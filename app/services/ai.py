"""Module IA : le SEUL endroit du code qui parle à Mistral.

Chaque fonction publique a deux implémentations :
- `_live_*` : appel à l'API Mistral (chat completions en JSON, ou embeddings) ;
- `_mock_*` : réponse simulée réaliste, utilisée en mode `mock`, ou en mode `auto`
  quand l'API échoue (clé invalide, réseau…). L'application fonctionne donc toujours.

L'IA assiste, elle ne décide pas : les sorties sont validées avant d'être utilisées,
et la logique critique (cascade, scores, montants) reste du code classique.
On n'envoie pas les noms des clients à Mistral.
"""
import json
import logging
import re
import time
import unicodedata
from typing import Callable, TypeVar

import httpx

from app import config

log = logging.getLogger("loickaton.ai")
T = TypeVar("T")


class AIError(Exception):
    pass


_state = {"disabled_until": 0.0, "last_error": None, "last_success": None, "calls": 0}


def status() -> dict:
    """État de l'IA, affiché dans l'interface."""
    return {
        "mode": config.AI_MODE,
        "has_key": bool(config.MISTRAL_API_KEY),
        "using_mistral": _live_available(),
        "last_error": _state["last_error"],
        "last_success": _state["last_success"],
        "calls": _state["calls"],
        "models": {
            "large": config.MISTRAL_MODEL_LARGE,
            "small": config.MISTRAL_MODEL_SMALL,
            "embed": config.MISTRAL_EMBED_MODEL,
        },
    }


def reset_state() -> None:
    _state.update(disabled_until=0.0, last_error=None)


def _live_available() -> bool:
    if config.AI_MODE == "mock" or not config.MISTRAL_API_KEY:
        return False
    if config.AI_MODE == "auto" and time.time() < _state["disabled_until"]:
        return False
    return True


def _run(live: Callable[[], T], mock: Callable[[], T]) -> T:
    if not _live_available():
        if config.AI_MODE == "live":
            raise AIError("Mode live demandé mais aucune clé MISTRAL_API_KEY n'est configurée.")
        return mock()
    try:
        result = live()
        _state["last_success"] = time.strftime("%H:%M:%S")
        _state["last_error"] = None
        return result
    except Exception as exc:  # noqa: BLE001 - toute erreur API bascule en simulé
        _state["last_error"] = _describe_error(exc)
        log.warning("Mistral indisponible : %s", _state["last_error"])
        if config.AI_MODE == "live":
            raise AIError(_state["last_error"]) from exc
        # Limite de débit (429) : passagère, on réessaie vite. Autres erreurs : pause plus longue.
        pause = 20 if _status_code(exc) == 429 else config.AI_RETRY_AFTER_SECONDS
        _state["disabled_until"] = time.time() + pause
        return mock()


def _status_code(exc: Exception) -> int | None:
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))
    except ValueError:
        return ""


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return "Clé API Mistral refusée (401)"
        if code == 403:
            detail = _error_message(exc.response)
            if "tier" in detail.lower():
                return "Modèle Mistral non inclus dans votre abonnement (403) : changez de modèle dans .env"
            return "Accès refusé par l'API Mistral (403) : clé invalide ou sans droits"
        if code == 429:
            return "Quota Mistral dépassé (429)"
        return f"Erreur API Mistral ({code})"
    if isinstance(exc, httpx.HTTPError):
        return f"API Mistral injoignable ({exc.__class__.__name__})"
    return f"Réponse Mistral invalide ({exc.__class__.__name__})"


# --------------------------------------------------------------------------- appels HTTP

def _post(path: str, payload: dict) -> dict:
    _state["calls"] += 1
    response = httpx.post(
        f"{config.MISTRAL_API_URL}{path}",
        headers={"Authorization": f"Bearer {config.MISTRAL_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=config.MISTRAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _chat_json(model: str, system: str, user: str, temperature: float = 0.2) -> dict:
    data = _post("/chat/completions", {
        "model": model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    })
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("JSON attendu sous forme d'objet")
    return parsed


# --------------------------------------------------------------------------- 1. analyse de tâche

def analyze_task(title: str, description: str, specialties: dict[str, list[str]], task_types: dict[str, str]) -> dict:
    """specialties : {domaine: [sous-spécialités]} ; task_types : {code: libellé}.
    Retourne {specialty, sub_specialty, task_type, complexity (1-3), estimated_hours, keywords, summary, source}."""
    def live():
        system = (
            "Tu es l'assistant d'un cabinet d'avocats d'affaires français. On te donne une tâche juridique. "
            "Réponds uniquement en JSON avec les clés : specialty (un domaine EXACT de la liste, ou null), "
            "sub_specialty (une sous-spécialité EXACTE de ce domaine, ou null), task_type (un code EXACT de la liste "
            "des types, ou null), complexity (1 = simple, 2 = intermédiaire, 3 = complexe), estimated_hours (heures de "
            "travail réalistes pour un avocat), keywords (3 à 6 mots-clés), summary (une phrase)."
        )
        user = (f"Domaines et sous-spécialités : {json.dumps(specialties, ensure_ascii=False)}\n"
                f"Types de dossier : {json.dumps(task_types, ensure_ascii=False)}\n"
                f"Titre : {title}\nDescription : {description}")
        raw = _chat_json(config.MISTRAL_MODEL_LARGE, system, user)
        return _validate_analysis(raw, specialties, task_types) | {"source": "mistral"}

    return _run(live, lambda: _mock_analyze(title, description, specialties, task_types) | {"source": "simulé"})


def _validate_analysis(raw: dict, specialties: dict[str, list[str]], task_types: dict[str, str]) -> dict:
    specialty = raw.get("specialty")
    if specialty not in specialties:
        specialty = None
    sub = raw.get("sub_specialty")
    if not specialty or sub not in specialties[specialty]:
        sub = None
    task_type = raw.get("task_type") if raw.get("task_type") in task_types else None
    try:
        complexity = min(3, max(1, int(raw.get("complexity", 2))))
    except (TypeError, ValueError):
        complexity = 2
    try:
        hours = float(raw.get("estimated_hours", 4))
    except (TypeError, ValueError):
        hours = 4.0
    hours = round(min(200.0, max(0.5, hours)) * 2) / 2
    keywords = [str(k) for k in raw.get("keywords", []) if isinstance(k, (str, int))][:6]
    summary = str(raw.get("summary", ""))[:300]
    return {"specialty": specialty, "sub_specialty": sub, "task_type": task_type, "complexity": complexity,
            "estimated_hours": hours, "keywords": keywords, "summary": summary}


_SPECIALTY_KEYWORDS = {
    "M&A et private equity": ["acquisition", "cession", "spa", "due diligence", "fusion", "fonds", "fpci",
                              "investisseur", "closing", "private equity", "ief"],
    "Droit des sociétés": ["statuts", "joint-venture", "ohada", "filiale", "restructuration du groupe",
                           "implantation", "assemblee", "audit de contrats", "contrats fournisseurs"],
    "Restructuring et financement": ["redressement", "cessation des paiements", "conciliation", "dette",
                                     "financement", "suretes", "pool bancaire", "covenants", "procedure collective"],
    "Pénal des affaires": ["corruption", "penal", "enquete interne", "pnf", "fraude", "abus de biens sociaux"],
    "Compliance": ["sapin", "rgpd", "ai act", "conformite", "anticorruption", "commission europeenne", "entente",
                   "concurrence", "clemence"],
    "Contentieux des affaires": ["litige", "assignation", "contentieux", "recouvrement", "injonction de payer",
                                 "rupture brutale", "arbitrage", "assurance", "sinistre", "tribunal de commerce"],
    "Droit social": ["licenciement", "pse", "cse", "salarie", "prud", "directeur general salarie"],
    "Propriété intellectuelle et numérique": ["brevet", "marque", "contrefacon", "licence", "saas", "logiciel",
                                              "base de donnees"],
    "Droit fiscal": ["fiscal", "prix de transfert", "redressement fiscal", "verification de comptabilite"],
    "Droit public des affaires": ["marche public", "commande publique", "refere precontractuel"],
    "Droit immobilier": ["bail", "baux", "loyer", "bailleur"],
}
_SUB_KEYWORDS = {
    "Acquisitions de sociétés": ["acquisition", "spa", "cession"],
    "Opérations transfrontalières": ["americain", "etats-unis", "cfius", "transfrontal", "delaware"],
    "Contrôle des investissements étrangers": ["ief", "investissements etrangers", "direction generale du tresor"],
    "Fusions": ["fusion", "traite de fusion"],
    "Fonds d'investissement": ["fonds", "fpci", "societe de gestion"],
    "Due diligence": ["due diligence", "data room"],
    "Restructurations de groupe": ["restructuration du groupe", "apport partiel", "reorganisation"],
    "Joint-ventures et implantation internationale": ["joint-venture", "implantation", "filiale"],
    "Droit OHADA": ["ohada", "cote d'ivoire", "senegal"],
    "Audit contractuel": ["audit de", "contrats fournisseurs", "revue des contrats"],
    "Procédures collectives": ["redressement judiciaire", "cessation des paiements", "liquidation"],
    "Restructuration de dette": ["dette", "conciliation", "reechelonnement"],
    "Financement de projets et sûretés": ["financement", "project finance", "suretes", "pool bancaire"],
    "Réglementation bancaire": ["acpr", "agrement", "dsp2"],
    "Corruption et enquêtes internes": ["corruption", "enquete interne", "pnf", "afa"],
    "Fraude et abus de biens sociaux": ["fraude", "abus de biens sociaux", "escroquerie"],
    "Anticorruption (Sapin II)": ["sapin", "anticorruption", "red flags"],
    "RGPD et IA": ["rgpd", "ai act", "donnees personnelles", "intelligence artificielle"],
    "Droit de la concurrence": ["entente", "concurrence", "clemence", "griefs"],
    "Contentieux contractuel": ["inexecution", "contrat d'approvisionnement", "responsabilite contractuelle"],
    "Rupture brutale et distribution": ["rupture brutale", "distribution", "preavis"],
    "Recouvrement": ["recouvrement", "creance", "injonction de payer", "impaye"],
    "Contentieux international et arbitrage": ["arbitrage", "bruxelles i", "rome i", "international", "exequatur"],
    "Assurances": ["assurance", "assureur", "sinistre"],
    "Licenciements économiques (PSE)": ["pse", "licenciement economique", "plan de sauvegarde"],
    "Cadres dirigeants": ["directeur general", "cadre dirigeant", "mandataire"],
    "Brevets": ["brevet", "revendications"],
    "Marques": ["marque", "inpi", "euipo"],
    "Contrats IT et SaaS": ["saas", "licence logicielle", "sla", "logiciel"],
    "Bases de données": ["base de donnees", "sui generis"],
    "Prix de transfert et contrôle fiscal": ["prix de transfert", "controle fiscal", "rectification"],
    "Commande publique": ["marche public", "refere", "commande publique"],
    "Baux commerciaux": ["bail", "baux", "loyer"],
}
_TYPE_KEYWORDS = {
    "transactionnel": ["acquisition", "negociation", "spa", "fusion", "joint-venture", "financement", "creation"],
    "contentieux": ["litige", "assignation", "contentieux", "arbitrage", "tribunal", "contestation", "action en"],
    "conseil": ["audit", "mise en conformite", "enquete", "analyse", "conseil"],
    "conclusions": ["conclusions", "plaidoirie", "memoire", "requete"],
    "acte": ["rediger le contrat", "acte de", "convention", "traite", "pacte"],
    "audit": ["audit", "due diligence", "revue de"],
    "consultation": ["note", "consultation", "recherches"],
    "negociation": ["negoci"],
    "formalites": ["depot", "formalites", "greffe", "proces-verbal", "immatriculation", "registre"],
}
_COMPLEX_HINTS = ["complexe", "fusion", "acquisition", "cour d'appel", "cassation", "international",
                  "plan social", "instruction", "due diligence", "urgent", "plusieurs"]
_SIMPLE_HINTS = ["relecture", "relire", "simple", "courrier", "mise en demeure", "note courte", "verifier"]


def _best_match(text: str, options: list[str], keywords: dict[str, list[str]]) -> str | None:
    best, best_hits = None, 0
    for name in options:
        words = keywords.get(name) or _normalize(name).split()
        hits = sum(1 for k in words if k in text)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def _mock_analyze(title: str, description: str, specialties: dict[str, list[str]], task_types: dict[str, str]) -> dict:
    text = _normalize(f"{title} {description}")
    best = _best_match(text, list(specialties), _SPECIALTY_KEYWORDS)
    sub = _best_match(text, specialties[best], _SUB_KEYWORDS) if best else None
    task_type = _best_match(text, list(task_types), _TYPE_KEYWORDS)
    complexity = 2
    if sum(h in text for h in _COMPLEX_HINTS) >= 1 or len(text) > 500:
        complexity = 3
    elif sum(h in text for h in _SIMPLE_HINTS) >= 1 and len(text) < 250:
        complexity = 1
    hours = {1: 3.0, 2: 8.0, 3: 16.0}[complexity]
    match = re.search(r"(\d+)\s*pages", text)
    if match:
        hours = max(hours, round(int(match.group(1)) / 4 * 2) / 2)
    keywords = [k for k in (_SPECIALTY_KEYWORDS.get(best or "", [])) if k in text][:5]
    summary = f"Tâche {['', 'simple', 'intermédiaire', 'complexe'][complexity]}"
    summary += f" en {best.lower()}" if best else ""
    summary += f" ({sub.lower()})" if sub else ""
    return {"specialty": best, "sub_specialty": sub, "task_type": task_type, "complexity": complexity,
            "estimated_hours": min(hours, 200.0), "keywords": keywords, "summary": summary + "."}


# --------------------------------------------------------------------------- 2. classement des candidats

RANKING_SYSTEM = (
    "Tu es responsable du staffing d'un cabinet d'avocats d'affaires. On te donne un dossier (ou une tâche) à "
    "pourvoir, la hiérarchie des critères d'attribution du cabinet, et le profil COMPLET de chaque candidat "
    "éligible (les critères éliminatoires ont déjà été appliqués). Classe TOUS les candidats éligibles, du plus au "
    "moins susceptible de mener ce travail à bien.\n"
    "- Raisonne sur l'ensemble des informations : description du travail, domaine et sous-spécialité, dossiers "
    "similaires, type de dossier, charge et disponibilités, échéances, relation et langue du client, pays, système "
    "juridique, expérience, ancienneté, préférences.\n"
    "- Respecte la hiérarchie des critères : les critères principaux priment sur les complémentaires ; dans chaque "
    "groupe, un critère mieux classé pèse davantage ; une charge plus élevée pénalise fortement.\n"
    "- score : entier de 0 à 100 (probabilité de réussir ce travail), décroissant dans le classement.\n"
    "- raison : une phrase de 15 mots maximum, en français, qui donne l'argument décisif, sans inventer de faits.\n"
    'Réponds uniquement en JSON : {"classement": [{"id": <id>, "score": <0-100>, "raison": "..."}]}'
)


def rank_candidates(context: dict, eligible_ids: list[int]) -> dict | None:
    """Classement par Mistral. Retourne {"order": [ids], "scores": {id: score}, "reasons": {id: raison}},
    ou None si Mistral n'est pas disponible (l'appelant utilise alors le calcul local)."""
    if not _live_available():
        return None
    for attempt in range(3):
        try:
            raw = _chat_json(config.MISTRAL_MODEL_LARGE, RANKING_SYSTEM, json.dumps(context, ensure_ascii=False))
            result = _validate_ranking(raw, eligible_ids)
            _state.update(last_success=time.strftime("%H:%M:%S"), last_error=None)
            return result
        except Exception as exc:  # noqa: BLE001
            if _status_code(exc) == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            _state["last_error"] = _describe_error(exc)
            log.warning("Classement Mistral indisponible : %s", _state["last_error"])
            if config.AI_MODE == "auto":
                _state["disabled_until"] = time.time() + (20 if _status_code(exc) == 429 else config.AI_RETRY_AFTER_SECONDS)
            return None
    return None


def _validate_ranking(raw: dict, eligible_ids: list[int]) -> dict:
    allowed = set(eligible_ids)
    order, scores, reasons = [], {}, {}
    for entry in raw.get("classement", []):
        try:
            uid = int(entry["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if uid not in allowed or uid in order:
            continue
        order.append(uid)
        try:
            scores[str(uid)] = max(0, min(100, round(float(entry.get("score", 0)))))
        except (TypeError, ValueError):
            scores[str(uid)] = 0
        reasons[str(uid)] = str(entry.get("raison", ""))[:200]
    if not order:
        raise ValueError("Classement vide ou invalide")
    # Le score doit suivre le classement : on corrige les incohérences éventuelles
    previous = 100
    for uid in order:
        scores[str(uid)] = previous = min(scores[str(uid)], previous)
    return {"order": order, "scores": scores, "reasons": reasons}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# --------------------------------------------------------------------------- 4. libellés de facturation

def billing_label(task_title: str, task_description: str, note: str) -> str:
    def live():
        system = (
            "Tu rédiges des libellés de facturation pour un cabinet d'avocats français. À partir de la tâche et "
            "de la note rapide de l'avocat, écris un libellé professionnel, précis et compréhensible par le client "
            "(une phrase, 8 à 30 mots, sans montant ni durée, sans inventer de faits). "
            'Réponds en JSON : {"label": "..."}'
        )
        user = json.dumps({"tache": task_title, "description": task_description[:800], "note": note},
                          ensure_ascii=False)
        label = str(_chat_json(config.MISTRAL_MODEL_SMALL, system, user).get("label", "")).strip()
        if not label:
            raise ValueError("Libellé vide")
        return label[:400]

    return _run(live, lambda: _mock_label(task_title, note))


def _mock_label(task_title: str, note: str) -> str:
    note = note.strip().rstrip(".")
    if not note:
        return f"Travaux relatifs à : {task_title}."
    return f"{note[:1].upper()}{note[1:]} dans le cadre de la mission « {task_title} »."


# --------------------------------------------------------------------------- 5. conversation (assistant)

def chat(system: str, messages: list[dict], fallback: Callable[[], str]) -> tuple[str, str]:
    """Conversation libre avec Mistral. messages : [{"role": "user"|"assistant", "content": str}].
    Retourne (réponse, source).

    Contrairement aux autres fonctions, le chat ne retombe PAS silencieusement sur une réponse simulée quand une clé
    est configurée : chaque message est un vrai appel à Mistral, et une erreur est remontée telle quelle (AIError).
    `fallback` ne sert qu'en mode `mock` ou sans clé (tests, démo hors ligne)."""
    if config.AI_MODE == "mock" or not config.MISTRAL_API_KEY:
        return fallback(), "simulé"
    payload = {
        "model": config.MISTRAL_MODEL_CHAT,
        "temperature": 0.2,
        "max_tokens": 700,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    for attempt in range(3):
        try:
            data = _post("/chat/completions", payload)
            content = str(data["choices"][0]["message"]["content"]).strip()
            if not content:
                raise ValueError("Réponse vide")
            _state.update(last_success=time.strftime("%H:%M:%S"), last_error=None, disabled_until=0.0)
            return content, "mistral"
        except Exception as exc:  # noqa: BLE001
            if _status_code(exc) == 429 and attempt < 2:  # limite de débit : on patiente un peu
                time.sleep(1.5 * (attempt + 1))
                continue
            _state["last_error"] = _describe_error(exc)
            log.warning("Chat Mistral en échec : %s", _state["last_error"])
            raise AIError(_state["last_error"]) from exc
    raise AIError("Mistral n'a pas répondu")


def _mock_explain(candidates: list[dict]) -> dict[int, str]:
    out = {}
    for c in candidates:
        facts = c.get("facts", [])
        positives = [f for f in facts if not f.startswith("⚠")]
        warnings = [f.removeprefix("⚠ ") for f in facts if f.startswith("⚠")]
        text = ", ".join(positives[:2]) if positives else "Profil peu adapté"
        if warnings:
            text += f" ; attention : {warnings[0][:1].lower()}{warnings[0][1:]}"
        out[c["id"]] = text[:1].upper() + text[1:] + "."
    return out


def explain_locally(candidates: list[dict]) -> dict[int, str]:
    """Explication construite sans appel à Mistral (pour les candidats en bas du classement)."""
    return _mock_explain(candidates)


def normalize(text: str) -> str:
    """Minuscules sans accents (comparaisons de noms, langues, pays…)."""
    return _normalize(text)
