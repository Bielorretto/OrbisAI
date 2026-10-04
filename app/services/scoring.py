"""Matching : classe les collaborateurs pour un DOSSIER (choix de l'associé) ou les stagiaires pour une TÂCHE
(choix du collaborateur responsable), selon le document « Critères d'attribution » et dans son ordre hiérarchique.
Dossiers et tâches ont les mêmes caractéristiques (domaine, sous-spécialité, type, échéance, effort, exigences) :
on parle ci-dessous d'« élément » (item) pour l'un ou l'autre.

1. Critères éliminatoires, dans l'ordre : conflit d'intérêts, domaine du droit non maîtrisé, sous-spécialité
   non maîtrisée, niveau hiérarchique insuffisant, disponibilité minimale, puis langue et juridiction
   obligatoires (tableau d'exclusion automatique). Un seul critère suffit à écarter la personne.
2. Critères principaux (11) puis complémentaires (7) : chacun donne une valeur entre 0 et 1, pondérée selon
   son rang (le 1er critère d'un groupe pèse le plus). Un critère sans objet pour la tâche est ignoré.
3. En plus du score : coût estimé (effort x taux) et date de fin estimée d'après l'agenda.
4. Mistral rédige une phrase d'explication ; l'assistant (services/assistant.py) répond aux questions.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.labels import COMPLEMENTARY_CRITERIA, ELIMINATORY, MAIN_CRITERIA, ROLE, SPECIALTY_LEVEL, TASK_TYPES
from app.models import (CalendarEvent, Client, Conflict, Matter, MatterStatus, Role, Task, TaskStatus, TimeEntry,
                        User)

Item = Matter | Task
from app.services import ai

MAX_EXPLAINED = 10  # on ne demande une explication à Mistral que pour les premiers du classement
DEFAULT_LEGAL_SYSTEM = "Droit français"


@dataclass
class Criterion:
    key: str
    label: str
    tier: str            # "main" | "complementary"
    rank: int            # position dans la hiérarchie du groupe (1 = le plus important)
    value: float | None  # 0..1, None = sans objet pour cette tâche
    detail: str


@dataclass
class Candidate:
    user: User
    score: float = 0.0
    eliminations: list[tuple[str, str]] = field(default_factory=list)  # [(clé, motif)] dans l'ordre hiérarchique
    criteria: list[Criterion] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    free_hours: float = 0.0
    active_tasks: int = 0
    estimated_cost: float = 0.0
    estimated_finish: datetime | None = None
    explanation: str = ""

    @property
    def eliminated(self) -> str | None:
        """Premier motif d'élimination (le plus haut dans la hiérarchie)."""
        return self.eliminations[0][1] if self.eliminations else None

    @property
    def facts(self) -> list[str]:
        """Faits lisibles, dans l'ordre hiérarchique ; « ⚠ » marque un point faible."""
        return [("⚠ " if c.value is not None and c.value < 0.34 else "") + c.detail
                for c in self.criteria if c.value is not None]

    def criterion(self, key: str) -> Criterion | None:
        return next((c for c in self.criteria if c.key == key), None)


# --------------------------------------------------------------------------- utilitaires

def split_list(text: str | None) -> set[str]:
    return {ai.normalize(part).strip() for part in (text or "").split(",") if part.strip()}


def _in(value: str | None, text: str | None) -> bool:
    return bool(value) and ai.normalize(value).strip() in split_list(text)


# --------------------------------------------------------------------------- disponibilités

def _work_intervals(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Plages de travail (jours ouvrés, hors pause déjeuner) entre start et end."""
    intervals = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        if day.weekday() < 5:
            for h1, h2 in ((config.WORKDAY_START_HOUR, config.LUNCH_START_HOUR),
                           (config.LUNCH_END_HOUR, config.WORKDAY_END_HOUR)):
                a, b = max(start, day.replace(hour=h1)), min(end, day.replace(hour=h2))
                if a < b:
                    intervals.append((a, b))
        day += timedelta(days=1)
    return intervals


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _overlap_hours(work: list[tuple[datetime, datetime]], busy: list[tuple[datetime, datetime]]) -> float:
    total = 0.0
    for a, b in work:
        for c, d in busy:
            lo, hi = max(a, c), min(b, d)
            if lo < hi:
                total += (hi - lo).total_seconds() / 3600
    return total


def _free_intervals(work, busy):
    """Plages de travail moins les créneaux occupés."""
    for a, b in work:
        cursor = a
        for c, d in busy:
            if d <= cursor or c >= b:
                continue
            if c > cursor:
                yield cursor, c
            cursor = max(cursor, d)
        if cursor < b:
            yield cursor, b


def logged_hours(db: Session, item: Item) -> float:
    query = select(func.coalesce(func.sum(TimeEntry.minutes), 0))
    if isinstance(item, Matter):
        query = query.join(Task).where(Task.matter_id == item.id)
    else:
        query = query.where(TimeEntry.task_id == item.id)
    return (db.scalar(query) or 0) / 60


def remaining_hours(db: Session, item: Item) -> float:
    return max(0.0, item.estimated_hours - logged_hours(db, item))


def workload(db: Session, user: User, exclude: Item | None = None) -> list[Item]:
    """Ce qui occupe la personne : ses dossiers en cours (collaborateur) ou ses tâches déléguées (stagiaire).
    La charge estimée d'un dossier couvre les tâches que le collaborateur y réalise lui-même."""
    if user.role == Role.INTERN:
        items = db.scalars(select(Task).where(Task.status.in_(TaskStatus.ACTIVE_WORK), Task.delegate_id == user.id))
    else:
        items = db.scalars(select(Matter).where(Matter.status == MatterStatus.ACTIVE, Matter.assignee_id == user.id))
    return [i for i in items if i is not exclude]


def _busy(db: Session, user: User, start: datetime, end: datetime, load: list[Item]):
    # Les blocs « travail » liés à une tâche déjà comptée dans la charge ne sont pas comptés deux fois
    counted = {t.id for t in load if isinstance(t, Task)} | {
        t.id for m in load if isinstance(m, Matter) for t in m.tasks}
    events = db.scalars(select(CalendarEvent).where(
        CalendarEvent.user_id == user.id, CalendarEvent.end > start, CalendarEvent.start < end))
    return _merge([(e.start, e.end) for e in events if e.task_id not in counted])


def free_hours(db: Session, user: User, start: datetime, end: datetime, exclude: Item | None = None) -> float:
    """Heures libres = heures ouvrées - agenda - reste à faire sur la charge en cours."""
    if end <= start:
        return 0.0
    work = _work_intervals(start, end)
    capacity = sum((b - a).total_seconds() / 3600 for a, b in work)
    load = workload(db, user, exclude)
    available = capacity - _overlap_hours(work, _busy(db, user, start, end, load))

    window = (end - start).total_seconds()
    for item in load:
        remaining = remaining_hours(db, item)
        if item.deadline <= end:
            available -= remaining
        else:  # l'élément se termine après : on n'en compte qu'une part proportionnelle
            span = max((item.deadline - start).total_seconds(), 1)
            available -= remaining * min(1.0, window / span)
    return round(max(0.0, available), 1)


def estimated_finish(db: Session, user: User, item: Item, start: datetime, horizon_days: int = 45) -> datetime | None:
    """Date de fin estimée : on remplit les créneaux libres de l'agenda, après le reste à faire de la charge
    en cours dont l'échéance est antérieure. Sert de « délai de traitement proposé »."""
    load = workload(db, user, item)
    needed = item.estimated_hours + sum(remaining_hours(db, x) for x in load if x.deadline <= item.deadline)
    end = start + timedelta(days=horizon_days)
    work = _work_intervals(start, end)
    for a, b in _free_intervals(work, _busy(db, user, start, end, load)):
        hours = (b - a).total_seconds() / 3600
        if hours >= needed:
            return a + timedelta(hours=needed)
        needed -= hours
    return None


# --------------------------------------------------------------------------- embeddings (Mistral)

def _profile_text(db: Session, user: User) -> str:
    specialties = ", ".join(us.specialty.name for us in user.specialties)
    subs = ", ".join(us.sub_specialty.name for us in user.sub_specialties)
    past = [i.title for i in _history(db, user, None)][:20]
    return f"{user.bio}\nSpécialités : {specialties}\nSous-spécialités : {subs}\nDossiers traités : {'; '.join(past)}"


def _task_text(task: Item) -> str:
    parts = [task.title, task.description, task.specialty.name if task.specialty else "",
             task.sub_specialty.name if task.sub_specialty else ""]
    return "\n".join(p for p in parts if p)


def _load(raw: str | None) -> tuple[str | None, list[float]]:
    if not raw:
        return None, []
    data = json.loads(raw)
    return data.get("model"), data.get("v", [])


def ensure_embeddings(db: Session, task: Item, users: list[User]) -> None:
    """Calcule (et stocke) les embeddings manquants ou issus d'un autre modèle que celui de la tâche."""
    task_model, _ = _load(task.embedding)
    if task_model is None:
        task_model, (vector,) = ai.embed([_task_text(task)])
        task.embedding = json.dumps({"model": task_model, "v": vector})
    stale = [u for u in users if _load(u.profile_embedding)[0] != task_model]
    if stale:
        model, vectors = ai.embed([_profile_text(db, u) for u in stale])
        if model != task_model:  # l'API a changé d'état entre-temps : on réaligne la tâche
            task_model, (vector,) = ai.embed([_task_text(task)])
            task.embedding = json.dumps({"model": task_model, "v": vector})
            model, vectors = ai.embed([_profile_text(db, u) for u in users])
            stale = users
        for u, v in zip(stale, vectors):
            u.profile_embedding = json.dumps({"model": model, "v": v})
    db.flush()


def invalidate_task_embedding(task: Item) -> None:
    task.embedding = None


# --------------------------------------------------------------------------- critères

def _eliminations(db: Session, user: User, task: Item, client: Client, conflicts: dict[int, str],
                  free: float) -> list[tuple[str, str]]:
    """Critères éliminatoires, dans l'ordre hiérarchique du document."""
    out = []
    if user.id in conflicts:
        out.append(("conflict", ELIMINATORY["conflict"] + (f" : {conflicts[user.id]}" if conflicts[user.id] else "")))
    if task.specialty_id and not any(us.specialty_id == task.specialty_id for us in user.specialties):
        out.append(("domain", f"{ELIMINATORY['domain']} ({task.specialty.name})"))
    if task.sub_specialty_id and not any(us.sub_specialty_id == task.sub_specialty_id for us in user.sub_specialties):
        out.append(("sub_specialty", f"{ELIMINATORY['sub_specialty']} ({task.sub_specialty.name})"))
    if user.level < Role.LEVEL.get(task.min_level, 1):
        out.append(("hierarchy", f"{ELIMINATORY['hierarchy']} (minimum : {ROLE[task.min_level].lower()})"))
    needed = task.estimated_hours * config.MIN_AVAILABILITY_RATIO
    if free < needed:
        out.append(("availability", f"{ELIMINATORY['availability']} ({free:g} h libres pour {needed:g} h nécessaires)"))
    if task.required_language and not _in(task.required_language, user.languages):
        out.append(("language", f"{ELIMINATORY['language']} ({task.required_language})"))
    if task.required_jurisdiction and not _in(task.required_jurisdiction, user.jurisdictions):
        out.append(("jurisdiction", f"{ELIMINATORY['jurisdiction']} ({task.required_jurisdiction})"))
    return out


def _history(db: Session, user: User, exclude: Item | None) -> list[Item]:
    """Expérience passée et en cours : dossiers dont la personne a été responsable (collaborateur) ou tâches
    qu'on lui a déléguées (stagiaire)."""
    if user.role == Role.INTERN:
        items = db.scalars(select(Task).where(Task.delegate_id == user.id))
    else:
        items = db.scalars(select(Matter).where(Matter.assignee_id == user.id))
    return [i for i in items if i is not exclude]


def _client_hours(db: Session, user: User, client_id: int) -> float:
    minutes = db.scalar(select(func.coalesce(func.sum(TimeEntry.minutes), 0)).join(Task).join(Matter).where(
        TimeEntry.user_id == user.id, Matter.client_id == client_id))
    return (minutes or 0) / 60


def _main_criteria(db: Session, c: Candidate, task: Item, client: Client, now: datetime, sim: float,
                   max_free: float) -> list[Criterion]:
    user, history = c.user, _history(db, c.user, task)
    values: dict[str, tuple[float | None, str]] = {}

    # 1. Niveau d'expertise dans le domaine
    if task.specialty_id:
        us = next((s for s in user.specialties if s.specialty_id == task.specialty_id), None)
        level = us.level if us else 0
        values["domain_level"] = (level / 3, f"{SPECIALTY_LEVEL.get(level, 'Aucun niveau')} en "
                                             f"{task.specialty.name.lower()}")
    else:
        values["domain_level"] = (None, "Pas de domaine imposé")

    # 2. Niveau d'expertise dans la sous-spécialité
    if task.sub_specialty_id:
        uss = next((s for s in user.sub_specialties if s.sub_specialty_id == task.sub_specialty_id), None)
        level = uss.level if uss else 0
        values["sub_level"] = (level / 3, f"{SPECIALTY_LEVEL.get(level, 'Aucun niveau')} en "
                                          f"{task.sub_specialty.name.lower()}")
    else:
        values["sub_level"] = (None, "Pas de sous-spécialité imposée")

    # 3. Nombre de dossiers similaires traités (historique déclaré + tâches de l'app + proximité Mistral)
    if task.sub_specialty_id:
        declared = next((s.cases_count for s in user.sub_specialties if s.sub_specialty_id == task.sub_specialty_id), 0)
        in_app = sum(1 for t in history if t.sub_specialty_id == task.sub_specialty_id)
    elif task.specialty_id:
        declared = next((s.cases_count for s in user.specialties if s.specialty_id == task.specialty_id), 0)
        in_app = sum(1 for t in history if t.specialty_id == task.specialty_id)
    else:
        declared, in_app = 0, 0
    count = declared + in_app
    values["similar_cases"] = (0.75 * min(1.0, count / 15) + 0.25 * sim,
                               f"{count} dossier{'s' if count > 1 else ''} similaire{'s' if count > 1 else ''} traité"
                               f"{'s' if count > 1 else ''}" + (" (profil très proche selon l'IA)" if sim >= 0.8 else ""))

    # 4. Expérience du type de dossier
    if task.task_type:
        counts = json.loads(user.task_type_counts or "{}")
        n = counts.get(task.task_type, 0) + sum(1 for t in history if t.task_type == task.task_type)
        values["task_type"] = (min(1.0, n / 8), f"{n} dossier{'s' if n > 1 else ''} de type « "
                                                f"{TASK_TYPES[task.task_type][0].lower()} »")
    else:
        values["task_type"] = (None, "Type de dossier non précisé")

    # 5. Charge de travail actuelle (comparée aux autres candidats : pénalisation forte)
    values["workload"] = (c.free_hours / max_free if max_free else 0.0,
                          f"{c.free_hours:g} h libres avant la deadline (besoin estimé : {task.estimated_hours:g} h)")

    # 6. Nombre de dossiers en cours
    values["open_files"] = (max(0.0, 1 - c.active_tasks / 5),
                            f"{c.active_tasks} dossier{'s' if c.active_tasks > 1 else ''} en cours")

    # 7. Nombre et criticité des échéances à venir (14 jours)
    upcoming = [t for t in workload(db, user, task) if t.deadline <= now + timedelta(days=14)]
    critical = sum(1 for t in upcoming if t.deadline <= now + timedelta(days=2))
    weight = sum(3 if t.deadline <= now + timedelta(days=2) else 2 if t.deadline <= now + timedelta(days=7) else 1
                 for t in upcoming)
    values["deadlines"] = (max(0.0, 1 - weight / 8),
                           f"{len(upcoming)} échéance{'s' if len(upcoming) > 1 else ''} dans les 14 jours"
                           + (f", dont {critical} sous 48 h" if critical else ""))

    # 8. Secteur d'activité du client
    if client.sector:
        ok = _in(client.sector, user.sectors)
        values["sector"] = (1.0 if ok else 0.0,
                            f"{'Maîtrise' if ok else 'Ne connaît pas'} le secteur « {client.sector.lower()} »")
    else:
        values["sector"] = (None, "Secteur du client non renseigné")

    # 9. Connaissance préalable du client
    client_tasks = [t for t in history if t.client.id == client.id]
    values["client_knowledge"] = (min(1.0, len(client_tasks) / 2),
                                  f"A déjà travaillé {len(client_tasks)} fois pour ce client" if client_tasks
                                  else "N'a jamais travaillé pour ce client")

    # 10. Historique de relation avec le client
    hours = _client_hours(db, user, client.id)
    values["client_history"] = (min(1.0, hours / 20), f"{hours:g} h passées sur les dossiers de ce client")

    # 11. Langue maternelle du client
    if client.language:
        ok = _in(client.language, user.languages)
        values["client_language"] = (1.0 if ok else 0.0,
                                     f"{'Parle' if ok else 'Ne parle pas'} {client.language.lower()}, "
                                     "la langue du client")
    else:
        values["client_language"] = (None, "Langue du client non renseignée")

    return [Criterion(k, label, "main", i, *values[k]) for i, (k, label) in enumerate(MAIN_CRITERIA.items(), 1)]


def _complementary_criteria(c: Candidate, task: Item, client: Client, now: datetime) -> list[Criterion]:
    user = c.user
    country = task.country or client.country
    legal_system = task.legal_system or DEFAULT_LEGAL_SYSTEM
    task_type = TASK_TYPES.get(task.task_type or "")
    nature = task_type[2] if task_type else None
    years = user.years_at_firm(now.date())
    prefs = split_list(user.preferences)
    matching = sorted(p for p in (task_type[1] if task_type else set()) if ai.normalize(p) in prefs)
    values = {
        "international": (user.international_level / 2,
                          ["Pas d'expérience internationale", "Quelques dossiers internationaux",
                           "Solide expérience internationale"][min(2, user.international_level)]),
        "country": ((1.0 if _in(country, user.countries) else 0.0) if country else None,
                    f"{'Connaît' if _in(country, user.countries) else 'Ne connaît pas'} le pays ({country})"),
        "legal_system": (1.0 if _in(legal_system, user.legal_systems) else 0.0,
                         f"{'Maîtrise' if _in(legal_system, user.legal_systems) else 'Ne maîtrise pas'} : {legal_system}"),
        "litigation": (user.litigation_level / 3, f"Expérience contentieuse {user.litigation_level}/3"
                       + (" (dossier contentieux)" if nature == "contentieux" else "")),
        "transactional": (user.transactional_level / 3, f"Expérience transactionnelle {user.transactional_level}/3"
                          + (" (dossier transactionnel)" if nature == "transactionnel" else "")),
        "firm_seniority": (min(1.0, years / 10), f"{years} an{'s' if years > 1 else ''} au cabinet"),
        "preferences": ((1.0 if matching else 0.0) if task_type else None,
                        f"Aime : {', '.join(matching)}" if matching else "Préférences non alignées avec ce dossier"),
    }
    return [Criterion(k, label, "complementary", i, *values[k])
            for i, (k, label) in enumerate(COMPLEMENTARY_CRITERIA.items(), 1)]


def _tier_score(criteria: list[Criterion], tier: str) -> float | None:
    """Moyenne pondérée par le rang : dans un groupe de n critères, le 1er pèse n, le dernier 1."""
    group = [c for c in criteria if c.tier == tier]
    n = len(group)
    applicable = [(n + 1 - c.rank, c.value) for c in group if c.value is not None]
    total = sum(w for w, _ in applicable)
    return sum(w * v for w, v in applicable) / total if total else None


def _final_score(criteria: list[Criterion]) -> float:
    main, comp = _tier_score(criteria, "main"), _tier_score(criteria, "complementary")
    share = config.MAIN_CRITERIA_SHARE
    if main is None or comp is None:
        return round(100 * (main if comp is None else comp if main is None else 0), 1)
    return round(100 * (share * main + (1 - share) * comp), 1)


def _sort_key(c: Candidate):
    """Score, puis départage par les critères dans l'ordre hiérarchique."""
    return (-c.score, *[-(cr.value or 0) for cr in c.criteria])


# --------------------------------------------------------------------------- classement

def rank_candidates(db: Session, task: Item, role: str, now: datetime, explain: bool = True) -> list[Candidate]:
    """Classe les personnes du rôle `role` pour un dossier (collaborateurs) ou une tâche (stagiaires)."""
    users = list(db.scalars(select(User).where(User.role == role).order_by(User.name)))
    client = task.client
    conflicts = {c.user_id: c.reason for c in db.scalars(select(Conflict).where(Conflict.client_id == client.id))}
    ensure_embeddings(db, task, users)
    _, task_vec = _load(task.embedding)

    candidates = []
    for user in users:
        c = Candidate(user=user)
        c.free_hours = free_hours(db, user, now, task.deadline, exclude=task)
        c.active_tasks = len(workload(db, user, task))
        c.estimated_cost = round(task.estimated_hours * user.hourly_rate, 2)
        c.eliminations = _eliminations(db, user, task, client, conflicts, c.free_hours)
        candidates.append(c)

    eligible = [c for c in candidates if not c.eliminations]
    sims = {c.user.id: max(0.0, ai.cosine(task_vec, _load(c.user.profile_embedding)[1])) for c in eligible}
    best_sim = max(sims.values(), default=0) or 1.0
    max_free = max((c.free_hours for c in eligible), default=0)

    for c in eligible:
        c.criteria = (_main_criteria(db, c, task, client, now, sims[c.user.id] / best_sim, max_free)
                      + _complementary_criteria(c, task, client, now))
        c.score = _final_score(c.criteria)
        c.estimated_finish = estimated_finish(db, c.user, task, now)
        _badges(c, task)

    eligible.sort(key=_sort_key)
    eliminated = sorted((c for c in candidates if c.eliminations),
                        key=lambda c: list(ELIMINATORY).index(c.eliminations[0][0]))

    if explain and eligible:
        task_info = {
            "titre": task.title,
            "specialite": task.specialty.name if task.specialty else None,
            "sous_specialite": task.sub_specialty.name if task.sub_specialty else None,
            "effort_estime_heures": task.estimated_hours,
            "jours_avant_deadline": max(0, (task.deadline - now).days),
        }
        payload = [{"id": c.user.id, "name": c.user.name, "score": c.score, "facts": c.facts[:8]}
                   for c in eligible[:MAX_EXPLAINED]]
        explanations = ai.explain_ranking(task_info, payload)
        fallback = ai.explain_locally([{"id": c.user.id, "facts": c.facts} for c in eligible[MAX_EXPLAINED:]])
        for c in eligible:
            c.explanation = explanations.get(c.user.id) or fallback.get(c.user.id, "")
    return eligible + eliminated


def _badges(c: Candidate, task: Item) -> None:
    sub = c.criterion("sub_level")
    dom = c.criterion("domain_level")
    if sub and sub.value:
        c.badges.append(f"{task.sub_specialty.name} · {SPECIALTY_LEVEL[round(sub.value * 3)]}")
    elif dom and dom.value:
        c.badges.append(f"{task.specialty.name} · {SPECIALTY_LEVEL[round(dom.value * 3)]}")
    if c.free_hours >= 1.5 * task.estimated_hours:
        c.badges.append("Disponible")
    if (k := c.criterion("client_knowledge")) and k.value:
        c.badges.append("Connaît le client")
    if (lang := c.criterion("client_language")) and lang.value:
        c.badges.append("Parle la langue du client")


def compare(a: Candidate, b: Candidate, threshold: float = 0.15) -> list[tuple[Criterion, Criterion]]:
    """Critères où a et b diffèrent nettement, dans l'ordre hiérarchique (le premier est décisif)."""
    out = []
    for ca in a.criteria:
        cb = b.criterion(ca.key)
        if cb and ca.value is not None and cb.value is not None and abs(ca.value - cb.value) >= threshold:
            out.append((ca, cb))
    return out


def eligible_ids(db: Session, task: Item, role: str, now: datetime) -> dict[int, str | None]:
    """{user_id: motif d'élimination ou None} — utilisé pour valider côté serveur les choix de l'utilisateur."""
    return {c.user.id: c.eliminated for c in rank_candidates(db, task, role, now, explain=False)}
