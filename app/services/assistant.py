"""Assistant conversationnel : explique pourquoi un collaborateur (à l'associé, pour un dossier) ou un stagiaire
(au collaborateur responsable, pour une tâche) est bien placé ou non.

Les réponses s'appuient uniquement sur le classement calculé par scoring.py (critères du document
« Critères d'attribution », dans leur ordre hiérarchique). Mistral rédige la réponse ; sans Mistral,
une réponse déterministe est construite à partir des mêmes données.
"""
import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app import clock
from app.labels import COMPLEMENTARY_CRITERIA, ELIMINATORY, MAIN_CRITERIA, ROLE, TASK_TYPES
from app.models import Matter, Role, Task, User
from app.services import ai
from app.services.scoring import Candidate, compare, rank_candidates

MAX_HISTORY = 8
MAX_MESSAGE_LENGTH = 1000


class AssistantError(Exception):
    pass


def pool_for(user: User, item: Matter | Task) -> str | None:
    """Associé/partner sur un dossier -> collaborateurs ; collaborateur responsable d'une tâche -> stagiaires."""
    if isinstance(item, Matter):
        return Role.ASSOCIATE if user.is_assigner else None
    return Role.INTERN if user.id == item.assignee_id else None


def suggestions(candidates: list[Candidate], pool: str) -> list[str]:
    noun = "collaborateur" if pool == Role.ASSOCIATE else "stagiaire"
    eligible = [c for c in candidates if not c.eliminated]
    out = [f"Quel {noun} est le meilleur choix ?"]
    if len(eligible) >= 2:
        out.append(f"Pourquoi {eligible[0].user.name} plutôt que {eligible[1].user.name} ?")
    excluded = next((c for c in candidates if c.eliminated), None)
    if excluded:
        out.append(f"Pourquoi {excluded.user.name} est exclu ?")
    out.append("Comment les critères sont-ils hiérarchisés ?")
    return out


def answer(db: Session, user: User, task: Matter | Task, question: str, history: list[dict]) -> dict:
    pool = pool_for(user, task)
    if pool is None:
        raise AssistantError("L'assistant explique les classements à l'associé (choix du collaborateur pour un "
                             "dossier) et au collaborateur responsable d'une tâche (choix du stagiaire).")
    question = question.strip()[:MAX_MESSAGE_LENGTH]
    if not question:
        raise AssistantError("Posez une question.")
    now = clock.now(db)
    candidates = rank_candidates(db, task, pool, now, explain=False)
    messages = [{"role": m["role"], "content": str(m["content"])[:MAX_MESSAGE_LENGTH]}
                for m in history[-MAX_HISTORY:] if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": question})
    system = _system_prompt(task, candidates, pool, user, now)
    fallback = lambda: local_answer(question, task, candidates, pool)  # noqa: E731
    text, source = ai.chat(system, messages, fallback)
    errors = factual_errors(text, candidates)
    if errors and source == "mistral":
        # Garde-fou : on demande à Mistral de corriger avant d'afficher quoi que ce soit
        correction = ("Ta réponse contient une erreur factuelle : " + " ; ".join(errors) + ". Réécris ta réponse à "
                      "ma question précédente en corrigeant cela, sans mentionner cette correction.")
        text, source = ai.chat(system, [*messages, {"role": "assistant", "content": text},
                                        {"role": "user", "content": correction}], fallback)
        if factual_errors(text, candidates):  # toujours faux : on préfère la réponse construite par le code
            text, source = fallback(), "vérifié"
    return {"answer": text, "source": source}


_EXCLUSION_WORDS = ("exclu", "eliminatoire", "elimine", "ecarte")
_NEGATIONS = ("pas exclu", "non exclu", "pas eliminatoire", "non eliminatoire", "pas elimine", "pas ecarte",
              "n'est pas", "ne sont pas", "aucun critere eliminatoire", "jamais eliminatoire")


def factual_errors(text: str, candidates: list[Candidate]) -> list[str]:
    """Repère une affirmation fausse fréquente : une personne éligible présentée comme exclue."""
    errors = []
    sentences = re.split(r"(?<=[.!?])\s+|\n+", ai.normalize(text))
    eligible = [c for c in candidates if not c.eliminated]
    for c in eligible:
        names = {ai.normalize(c.user.name), ai.normalize(c.user.name).split()[0]}
        for sentence in sentences:
            if not any(re.search(rf"\b{re.escape(n)}\b", sentence) for n in names):
                continue
            others = [o for o in candidates if o is not c and ai.normalize(o.user.name).split()[0] in sentence]
            if others:  # phrase qui parle de plusieurs personnes : trop ambigu pour conclure
                continue
            if any(w in sentence for w in _EXCLUSION_WORDS) and not any(n in sentence for n in _NEGATIONS):
                rank = eligible.index(c) + 1
                errors.append(f"{c.user.name} n'est PAS exclu·e : cette personne est éligible (n°{rank}, "
                              f"score {c.score:g}/100)")
                break
    return errors


# --------------------------------------------------------------------------- contexte pour Mistral

def _system_prompt(task: Matter | Task, candidates: list[Candidate], pool: str, user: User, now: datetime) -> str:
    noun = "collaborateurs" if pool == Role.ASSOCIATE else "stagiaires"
    is_matter = isinstance(task, Matter)
    item = "le dossier" if is_matter else "la tâche"
    client = task.client
    homonym = any(c.user.name == user.name and c.user.id != user.id for c in candidates)
    who = f"l'{ROLE[user.role].lower()} responsable du dossier" if is_matter else \
        f"le {ROLE[user.role].lower()} responsable de la tâche"
    speaker = who if homonym else f"{user.name} ({who})"
    data = {
        "nature": "dossier" if is_matter else "tâche",
        "element": {
            "titre": task.title,
            "dossier": None if is_matter else task.matter.title,
            "domaine": task.specialty.name if task.specialty else None,
            "sous_specialite": task.sub_specialty.name if task.sub_specialty else None,
            "type_de_dossier": TASK_TYPES[task.task_type][0] if task.task_type else None,
            "effort_estime_heures": task.estimated_hours,
            "echeance": task.deadline.strftime("%d/%m/%Y %Hh%M"),
            "niveau_minimum": ROLE.get(task.min_level),
            "langue_obligatoire": task.required_language,
            "juridiction_obligatoire": task.required_jurisdiction,
            # Le nom du client n'est pas transmis (pseudonymisation)
            "client": {"secteur": client.sector, "langue": client.language, "pays": client.country},
        },
        "eligibles_par_ordre": [_display_name(c, user) for c in candidates if not c.eliminated],
        "exclus": {_display_name(c, user): c.eliminated for c in candidates if c.eliminated},
        "classement": [_candidate_data(i, c, user) for i, c in enumerate(candidates, 1)],
    }
    hierarchy = {
        "1_eliminatoires_dans_l_ordre": list(ELIMINATORY.values()),
        "2_principaux_dans_l_ordre": list(MAIN_CRITERIA.values()),
        "3_complementaires_dans_l_ordre": list(COMPLEMENTARY_CRITERIA.values()),
    }
    return (
        f"Tu es l'assistant d'attribution des tâches d'un cabinet d'avocats, intégré à l'application. "
        f"Tu discutes avec {speaker}. Nous sommes le {now:%d/%m/%Y à %Hh%M}.\n"
        f"Le sujet de la conversation : {item} ci-dessous et le choix des {noun} pour {'le prendre en charge' if is_matter else 'la réaliser'}.\n\n"
        "Comment te comporter :\n"
        "- L'utilisateur n'est PAS un candidat : ne lui dis jamais « toi » ou « vous » en parlant d'un candidat, "
        "désigne toujours les candidats par leur nom tel qu'il figure dans les données. Adopte le tutoiement ou le "
        "vouvoiement de l'utilisateur.\n"
        "- Converse naturellement, comme un collègue : réponds aux salutations, aux questions de suivi, aux "
        "« et lui ? », aux demandes de reformulation. Tiens compte de l'historique de la conversation.\n"
        "- PRIORITÉ À LA CONCISION : réponds en français en 2 à 4 phrases, sans liste, en allant droit au but "
        "(la réponse d'abord, puis la raison principale). Ne récapitule pas le classement complet et ne termine pas "
        "par une question de relance systématique.\n"
        "- Ne donne une réponse détaillée (listes, plusieurs critères, comparaison complète) QUE si l'utilisateur le "
        "demande explicitement (« détaille », « explique en détail », « liste », « compare tous les critères »…). "
        "Mise en forme légère : **gras** et listes à puces « - » uniquement, pas de titres ni de séparateurs.\n"
        "- Pour tout fait (niveau, disponibilité, nombre de dossiers, score, coût, date…), appuie-toi UNIQUEMENT sur "
        "les données JSON ci-dessous et recopie les valeurs telles quelles. Ne calcule pas de nouveaux montants ou "
        "délais. Si une information manque, dis-le ; n'invente jamais.\n"
        "- Les SEULES personnes exclues sont celles de la liste « exclus », pour le motif indiqué. Toute personne de "
        "« eligibles_par_ordre » est éligible : ne dis jamais qu'elle est exclue. Les critères principaux et "
        "complémentaires (dont la langue du client, le secteur ou la relation client) ne sont jamais éliminatoires : "
        "ils font seulement monter ou baisser le score.\n"
        "- Respecte la hiérarchie des critères : les critères principaux priment sur les complémentaires ; dans chaque "
        "groupe, un critère mieux classé pèse plus. Pour comparer deux personnes, commence par le critère le plus "
        "haut dans la hiérarchie qui les départage.\n"
        "- Le fonctionnement de l'application : un dossier est confié à UN collaborateur responsable ; l'associé "
        "choisit 3 collaborateurs dans l'ordre, et si le premier refuse ou ne répond pas, le dossier passe au suivant. "
        "Le collaborateur responsable crée ensuite les tâches du dossier et confie éventuellement chacune à UN "
        "stagiaire. Ne propose pas d'autres organisations (co-rédaction, binômes…).\n"
        "- Tu peux donner ton avis et proposer un ordre pour les 3 choix, en rappelant que la décision revient à "
        "l'utilisateur. Le coût estimé et la date de fin estimée aident à décider mais ne comptent pas dans le score.\n"
        "- Tu ne peux faire AUCUNE action dans l'application (attribuer, envoyer, déléguer, exclure quelqu'un) : "
        "c'est l'utilisateur qui le fait avec les boutons de la page. Ne propose donc pas de le faire à sa place.\n"
        f"- Si on te parle d'autre chose, réponds brièvement et ramène la conversation à {item}.\n\n"
        f"Hiérarchie des critères : {json.dumps(hierarchy, ensure_ascii=False)}\n\n"
        f"Données (classement actuel, recalculé à chaque message) : {json.dumps(data, ensure_ascii=False)}"
    )


def _display_name(c: Candidate, user: User) -> str:
    """Un candidat homonyme de l'utilisateur (ex. les deux comptes « Exemple ») est désigné sans ambiguïté."""
    return f"{c.user.name} ({ROLE[c.user.role].lower()})" if c.user.name == user.name else c.user.name


def _candidate_data(position: int, c: Candidate, user: User) -> dict:
    if c.eliminations:
        return {"nom": _display_name(c, user), "statut": "exclu",
                "motifs_d_exclusion": [m for _, m in c.eliminations]}
    return {
        "rang": position,
        "nom": _display_name(c, user),
        "statut": "éligible (n'est PAS exclu)",
        "score": c.score,
        "cout_estime_euros_ht": c.estimated_cost,
        "fin_estimee": c.estimated_finish.strftime("%d/%m %Hh%M") if c.estimated_finish else None,
        "criteres": [{"critere": cr.label, "groupe": "principal" if cr.tier == "main" else "complémentaire",
                      "rang": cr.rank, "evaluation": _grade(cr.value), "detail": cr.detail}
                     for cr in c.criteria if cr.value is not None],
    }


def _grade(value: float) -> str:
    return "fort" if value >= 0.67 else "moyen" if value >= 0.34 else "faible"


# --------------------------------------------------------------------------- réponse sans Mistral

def _mentions(question: str, candidates: list[Candidate]) -> list[Candidate]:
    """Candidats cités dans la question, dans l'ordre où ils apparaissent."""
    text = f" {re.sub(r'[^a-z0-9 ]', ' ', ai.normalize(question))} "
    text = text.replace(" par exemple ", " ")  # « par exemple » ne désigne pas le compte Exemple
    found = []
    for c in candidates:
        tokens = [ai.normalize(c.user.name)] + ai.normalize(c.user.name).split()
        positions = [text.find(f" {t} ") for t in tokens if len(t) >= 3]
        positions = [p for p in positions if p >= 0]
        if positions:
            found.append((min(positions), c))
    found.sort(key=lambda x: x[0])
    unique = []
    for _, c in found:
        if c not in unique:
            unique.append(c)
    return unique


def _position(c: Candidate, candidates: list[Candidate]) -> str:
    if c.eliminated:
        return "exclu"
    rank = [x for x in candidates if not x.eliminated].index(c) + 1
    return f"n°{rank}, score {c.score:g}/100"


def _strengths(c: Candidate, limit: int = 4) -> list[str]:
    return [f"{cr.label} : {cr.detail}" for cr in c.criteria if cr.value is not None and cr.value >= 0.67][:limit]


def _explain_exclusion(c: Candidate) -> str:
    lines = [f"{c.user.name} est exclu·e par un critère éliminatoire."]
    lines += [f"• {reason}" for _, reason in c.eliminations]
    if len(c.eliminations) > 1:
        lines.append(f"Le premier dans la hiérarchie est : {c.eliminations[0][1].split(' (')[0].split(' :')[0]}.")
    return "\n".join(lines)


def _explain_comparison(a: Candidate, b: Candidate, candidates: list[Candidate]) -> str:
    if b.eliminated and a.eliminated:
        return _explain_exclusion(a) + "\n\n" + _explain_exclusion(b)
    if a.eliminated or b.eliminated:
        ok, ko = (b, a) if a.eliminated else (a, b)
        return (f"{ok.user.name} ({_position(ok, candidates)}) est éligible, contrairement à {ko.user.name}.\n"
                + _explain_exclusion(ko))
    first, second = (a, b) if a.score >= b.score else (b, a)
    lines = [f"{first.user.name} ({_position(first, candidates)}) passe devant {second.user.name} "
             f"({_position(second, candidates)})."]
    diffs = compare(first, second)
    if not diffs:
        lines.append("Leurs profils sont très proches : l'écart vient de petites différences cumulées.")
        return "\n".join(lines)
    lines.append("Ce qui les départage, dans l'ordre hiérarchique des critères :")
    for ca, cb in diffs[:4]:
        tier = "principal" if ca.tier == "main" else "complémentaire"
        lines.append(f"• {ca.label} (critère {tier} n°{ca.rank}) — {first.user.name.split()[0]} : {ca.detail} ; "
                     f"{second.user.name.split()[0]} : {cb.detail}")
    favor_second = [ca.label for ca, cb in diffs if cb.value > ca.value]
    if favor_second:
        lines.append(f"À l'avantage de {second.user.name} : {', '.join(favor_second[:3]).lower()}, "
                     "mais ces critères sont moins prioritaires.")
    return "\n".join(lines)


def _explain_best(candidates: list[Candidate], pool: str) -> str:
    eligible = [c for c in candidates if not c.eliminated]
    noun = "collaborateur" if pool == Role.ASSOCIATE else "stagiaire"
    if not eligible:
        return f"Aucun {noun} n'est éligible : tous sont écartés par un critère éliminatoire."
    best = eligible[0]
    lines = [f"Le meilleur choix est {best.user.name} (score {best.score:g}/100)."]
    lines += [f"• {s}" for s in _strengths(best)]
    if len(eligible) > 1:
        runner = eligible[1]
        diffs = compare(best, runner)
        if diffs:
            ca, cb = diffs[0]
            lines.append(f"Devant {runner.user.name} ({runner.score:g}) grâce au critère « {ca.label} » : "
                         f"{ca.detail} contre {cb.detail}.")
    lines.append(f"Coût estimé : {best.estimated_cost:,.0f} € HT".replace(",", " ")
                 + (f", fin estimée le {best.estimated_finish:%d/%m à %Hh}." if best.estimated_finish else "."))
    if len(eligible) > 2:
        lines.append("Ensuite : " + ", ".join(f"{c.user.name} ({c.score:g})" for c in eligible[1:4]) + ".")
    return "\n".join(lines)


def _explain_single(c: Candidate, candidates: list[Candidate], pool: str) -> str:
    if c.eliminated:
        return _explain_exclusion(c)
    eligible = [x for x in candidates if not x.eliminated]
    if c is eligible[0]:
        return _explain_best(candidates, pool)
    weak = [f"{cr.label} : {cr.detail}" for cr in c.criteria if cr.value is not None and cr.value < 0.34][:3]
    text = _explain_comparison(eligible[0], c, candidates)
    if weak:
        text += "\nPoints faibles de " + c.user.name + " : " + " ; ".join(weak) + "."
    return text


def _explain_method() -> str:
    return ("Le classement suit le document « Critères d'attribution », dans son ordre hiérarchique :\n"
            "1. Critères éliminatoires (un seul suffit à exclure) : "
            + ", ".join(v.lower() for v in ELIMINATORY.values()) + ".\n"
            "2. Critères principaux (80 % du score), du plus au moins important : "
            + ", ".join(v.lower() for v in MAIN_CRITERIA.values()) + ".\n"
            "3. Critères complémentaires (20 %) : " + ", ".join(v.lower() for v in COMPLEMENTARY_CRITERIA.values())
            + ".\nDans chaque groupe, un critère mieux classé pèse davantage. Les égalités sont départagées "
              "critère par critère, dans le même ordre.")


def local_answer(question: str, task: Matter | Task, candidates: list[Candidate], pool: str) -> str:
    q = ai.normalize(question)
    mentioned = _mentions(question, candidates)
    if len(mentioned) >= 2:
        return _explain_comparison(mentioned[0], mentioned[1], candidates)
    if len(mentioned) == 1:
        return _explain_single(mentioned[0], candidates, pool)
    if any(w in q for w in ("exclu", "elimin", "ecart", "pas eligible")):
        excluded = [c for c in candidates if c.eliminated]
        if not excluded:
            return "Personne n'est exclu pour cette tâche."
        return "Personnes exclues :\n" + "\n".join(f"• {c.user.name} : {c.eliminated}" for c in excluded)
    if any(w in q for w in ("critere", "comment", "calcul", "hierarch", "score", "methode", "pondera")):
        return _explain_method()
    return _explain_best(candidates, pool)
