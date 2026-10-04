"""Module IA : le SEUL endroit du code qui parle à Mistral.

Chaque fonction publique a deux implémentations :
- `_live_*` : appel à l'API Mistral (chat completions en JSON, ou embeddings) ;
- `_mock_*` : réponse simulée réaliste, utilisée en mode `mock`, ou en mode `auto`
  quand l'API échoue (clé invalide, réseau…). L'application fonctionne donc toujours.

L'IA assiste, elle ne décide pas : les sorties sont validées avant d'être utilisées,
et la logique critique (cascade, scores, montants) reste du code classique.
On n'envoie pas les noms des clients à Mistral.
"""
import hashlib
import json
import logging
import math
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
_explanation_cache: dict[str, dict[int, str]] = {}


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
    _explanation_cache.clear()


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
    "Droit des sociétés": ["societe", "cession", "actions", "statuts", "fusion", "acquisition", "pacte",
                           "associes", "assemblee", "gouvernance", "due diligence", "capital", "holding"],
    "Droit social": ["licenciement", "salarie", "prud", "contrat de travail", "cse", "rupture conventionnelle",
                     "harcelement", "employeur", "convention collective", "plan social"],
    "Contentieux commercial": ["assignation", "contentieux", "tribunal de commerce", "litige", "creance",
                               "recouvrement", "refere", "conclusions", "appel", "mise en demeure", "rupture brutale"],
    "Droit immobilier": ["bail", "baux", "loyer", "immobilier", "construction", "copropriete", "vente immobiliere",
                         "promoteur", "permis", "locataire", "bailleur"],
    "Propriété intellectuelle": ["marque", "brevet", "contrefacon", "droit d'auteur", "licence", "logiciel",
                                 "inpi", "dessin", "propriete intellectuelle", "nom de domaine"],
    "Droit pénal des affaires": ["penal", "abus de biens sociaux", "corruption", "garde a vue", "plainte",
                                 "escroquerie", "fraude", "blanchiment", "instruction", "parquet"],
}
_SUB_KEYWORDS = {
    "Fusions-acquisitions": ["fusion", "acquisition", "cession", "due diligence", "garantie d'actif"],
    "Secrétariat juridique et gouvernance": ["assemblee", "statuts", "pv", "proces-verbal", "approbation des comptes"],
    "Pactes d'associés": ["pacte"],
    "Contentieux prud'homal": ["prud", "conseil de prud'hommes", "faute grave"],
    "Relations collectives": ["cse", "plan social", "pse", "accord collectif"],
    "Rupture du contrat de travail": ["rupture conventionnelle", "licenciement", "demission"],
    "Rupture brutale et distribution": ["rupture brutale", "distribution", "relations commerciales", "fournisseur"],
    "Recouvrement de créances": ["recouvrement", "creance", "impaye", "injonction de payer"],
    "Référés et urgence": ["refere", "urgence"],
    "Baux commerciaux": ["bail", "baux", "loyer", "deplafonnement", "indexation"],
    "Construction": ["construction", "chantier", "decennale"],
    "Transactions immobilières": ["vente immobiliere", "acquisition immobiliere", "promesse de vente"],
    "Marques": ["marque", "inpi", "euipo", "anteriorite"],
    "Brevets": ["brevet", "invention"],
    "Contrefaçon et logiciels": ["contrefacon", "logiciel", "licence"],
    "Abus de biens sociaux et fraude": ["abus de biens sociaux", "fraude", "escroquerie"],
    "Corruption et compliance": ["corruption", "compliance", "sapin"],
    "Enquêtes internes": ["enquete interne", "audition", "lanceur d'alerte"],
}
_TYPE_KEYWORDS = {
    "conclusions": ["conclusions", "assignation", "plaidoirie", "procedure", "refere", "audience"],
    "acte": ["rediger le contrat", "acte de", "convention", "contrat", "rupture conventionnelle"],
    "audit": ["audit", "due diligence", "revue de"],
    "consultation": ["note", "consultation", "analyser", "qualifier"],
    "negociation": ["negoci"],
    "formalites": ["depot", "formalites", "greffe", "proces-verbal", "immatriculation", "statuts"],
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


# --------------------------------------------------------------------------- 2. embeddings

MOCK_EMBED_MODEL = "mock-hash-256"


def embed(texts: list[str]) -> tuple[str, list[list[float]]]:
    """Retourne (nom du modèle, vecteurs). Le nom sert à ne comparer que des vecteurs compatibles."""
    def live():
        data = _post("/embeddings", {"model": config.MISTRAL_EMBED_MODEL, "input": texts})
        vectors = [item["embedding"] for item in sorted(data["data"], key=lambda d: d["index"])]
        if len(vectors) != len(texts):
            raise ValueError("Nombre d'embeddings inattendu")
        return config.MISTRAL_EMBED_MODEL, vectors

    return _run(live, lambda: (MOCK_EMBED_MODEL, [_mock_embed(t) for t in texts]))


_STOPWORDS = set("le la les de des du un une et en a au aux pour par sur dans avec l d que qui est ce ces son sa ses"
                 " il elle nous vous ils leur leurs ou ne pas plus se s y".split())


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _mock_embed(text: str, dim: int = 256) -> list[float]:
    vector = [0.0] * dim
    for word in re.findall(r"[a-z]{3,}", _normalize(text)):
        if word in _STOPWORDS:
            continue
        stem = word[:6]  # racinisation grossière : « licenciement » ~ « licencier »
        h = int(hashlib.md5(stem.encode()).hexdigest(), 16)
        vector[h % dim] += 1.0 if (h >> 8) % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------- 3. explication du classement

def explain_ranking(task_info: dict, candidates: list[dict]) -> dict[int, str]:
    """candidates : [{id, name, score, facts: [str]}] -> {id: phrase courte}. Résultat mis en cache."""
    if not candidates:
        return {}
    cache_key = hashlib.md5(json.dumps([task_info, candidates], sort_keys=True, default=str).encode()).hexdigest()
    if cache_key in _explanation_cache:
        return _explanation_cache[cache_key]

    def live():
        system = (
            "Tu aides un associé d'un cabinet d'avocats à choisir à qui confier une tâche. "
            "Pour chaque candidat, écris UNE phrase très courte (15 mots maximum), factuelle, en français, qui donne "
            "la raison principale de son classement (point fort ou point faible), "
            "en t'appuyant uniquement sur les faits fournis. "
            'Réponds en JSON : {"explanations": [{"id": <id>, "text": "..."}]}'
        )
        user = json.dumps({"tache": task_info, "candidats": candidates}, ensure_ascii=False)
        raw = _chat_json(config.MISTRAL_MODEL_SMALL, system, user, temperature=0.3)
        result = {}
        for item in raw.get("explanations", []):
            try:
                result[int(item["id"])] = str(item["text"])[:300]
            except (KeyError, TypeError, ValueError):
                continue
        # Candidat oublié par le modèle : on complète avec l'explication simulée
        mock = _mock_explain(candidates)
        return {c["id"]: result.get(c["id"]) or mock[c["id"]] for c in candidates}

    result = _run(live, lambda: _mock_explain(candidates))
    _explanation_cache[cache_key] = result
    return result


def _mock_explain(candidates: list[dict]) -> dict[int, str]:
    out = {}
    for c in candidates:
        facts = c.get("facts", [])
        positives = [f for f in facts if not f.startswith("⚠")]
        warnings = [f.removeprefix("⚠ ") for f in facts if f.startswith("⚠")]
        text = ", ".join(positives[:2]) if positives else "Profil peu adapté"
        if warnings:
            text += f" ; attention : {warnings[0].lower()}"
        out[c["id"]] = text[:1].upper() + text[1:] + "."
    return out


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


def explain_locally(candidates: list[dict]) -> dict[int, str]:
    """Explication construite sans appel à Mistral (pour les candidats en bas du classement)."""
    return _mock_explain(candidates)


def normalize(text: str) -> str:
    """Minuscules sans accents (comparaisons de noms, langues, pays…)."""
    return _normalize(text)
