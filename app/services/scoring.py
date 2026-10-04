"""Classement des personnes pour un DOSSIER (collaborateurs, choix de l'associé) ou une TÂCHE (stagiaires, choix du
collaborateur), selon le document « Critères d'attribution ».

Deux étapes :
1. Critères ÉLIMINATOIRES (règles fixes, vérifiées par le code, dans l'ordre du document) : conflit d'intérêts,
   domaine du droit non maîtrisé, sous-spécialité non maîtrisée, niveau hiérarchique insuffisant, disponibilité
   minimale, puis langue et juridiction obligatoires.
2. CLASSEMENT des personnes éligibles par Mistral : savoir qui est le plus susceptible de mener le travail à bien
   n'est pas une question déterministe. Mistral reçoit TOUTES les informations (profil complet, dossiers en cours et
   passés, disponibilités, relation client…) ainsi que la hiérarchie des critères, et rend un classement motivé.
   Ce classement est mémorisé (RankingCache) pour rester stable tant que les informations ne changent pas.
   Sans Mistral, un calcul local pondéré par le rang des critères prend le relais.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, config
from app.labels import COMPLEMENTARY_CRITERIA, ELIMINATORY, MAIN_CRITERIA, ROLE, SPECIALTY_LEVEL, TASK_TYPES
from app.models import (CalendarEvent, Client, Conflict, Matter, MatterMember, MatterStatus, RankingCache, Role, Task,
                        TaskStatus, TeamRole, TimeEntry, User)
from app.services import ai

Item = Matter | Task
DEFAULT_LEGAL_SYSTEM = "Droit français"
CACHE_HOURS = 24
CEFR = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
MASTERED = CEFR["C1"]   # niveau à partir duquel une langue obligatoire est considérée comme maîtrisée


class Ranking(list):
    """Liste de candidats (éligibles d'abord, dans l'ordre du classement) + origine du classement."""
    source: str = "local"   # "mistral" ou "local"


@dataclass
class Criterion:
    key: str
    label: str
    tier: str            # "main" | "complementary"
    rank: int            # position dans la hiérarchie du groupe (1 = le plus important)
    value: float | None  # 0..1, None = sans objet
    detail: str


@dataclass
class Candidate:
    user: User
    score: float = 0.0
    eliminations: list[tuple[str, str]] = field(default_factory=list)  # [(clé, motif)] dans l'ordre hiérarchique
    criteria: list[Criterion] = field(default_factory=list)
    free_hours: float = 0.0
    active_tasks: int = 0
    estimated_cost: float = 0.0
    estimated_finish: datetime | None = None
    explanation: str = ""

    @property
    def eliminated(self) -> str | None:
        return self.eliminations[0][1] if self.eliminations else None

    @property
    def facts(self) -> list[str]:
        return [("⚠ " if c.value is not None and c.value < 0.34 else "") + c.detail
                for c in self.criteria if c.value is not None]

    def criterion(self, key: str) -> Criterion | None:
        return next((c for c in self.criteria if c.key == key), None)


# --------------------------------------------------------------------------- utilitaires

def split_list(text: str | None) -> set[str]:
    return {ai.normalize(part).strip() for part in (text or "").split(",") if part.strip()}


def _in(value: str | None, text: str | None) -> bool:
    return bool(value) and ai.normalize(value).strip() in split_list(text)


def language_levels(user: User) -> dict[str, int]:
    """« Français C2, Anglais B2 » -> {"francais": 6, "anglais": 4}. Sans niveau indiqué : C2."""
    out = {}
    for part in (user.languages or "").split(","):
        match = re.match(r"\s*(.+?)\s*\b([ABC][12])?\s*$", part.strip())
        if match and match.group(1):
            out[ai.normalize(match.group(1))] = CEFR.get(match.group(2) or "C2", 6)
    return out


def language_level(user: User, language: str) -> int:
    return language_levels(user).get(ai.normalize(language), 0)


def pool_for(item: Item) -> tuple[str, ...]:
    """Dossier -> collaborateurs (senior, collaborateur, junior) ; tâche -> stagiaires."""
    return Role.LAWYERS if isinstance(item, Matter) else (Role.INTERN,)


# --------------------------------------------------------------------------- disponibilités

def _work_intervals(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
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


def _merge(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _overlap_hours(work, busy) -> float:
    total = 0.0
    for a, b in work:
        for c, d in busy:
            lo, hi = max(a, c), min(b, d)
            if lo < hi:
                total += (hi - lo).total_seconds() / 3600
    return total


def _free_intervals(work, busy):
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


def workload(db: Session, user: User, exclude: Item | None = None) -> list[tuple[Item, float]]:
    """Charge en cours : [(élément, heures restantes pour cette personne)].
    Collaborateur : sa part du reste à faire de chaque dossier en cours de son équipe.
    Stagiaire : le reste à faire des tâches qui lui sont déléguées."""
    if user.role == Role.INTERN:
        tasks = db.scalars(select(Task).where(Task.status.in_(TaskStatus.ACTIVE_WORK), Task.delegate_id == user.id))
        return [(t, remaining_hours(db, t)) for t in tasks if t is not exclude]
    matters = db.scalars(select(Matter).join(MatterMember).where(
        Matter.status == MatterStatus.ACTIVE, MatterMember.user_id == user.id,
        MatterMember.team_role == TeamRole.LAWYER))
    return [(m, remaining_hours(db, m) / max(1, len(m.lawyers))) for m in matters if m is not exclude]


def _busy(db: Session, user: User, start: datetime, end: datetime, load):
    # Les blocs « travail » liés à une tâche déjà comptée dans la charge ne sont pas comptés deux fois
    counted = {i.id for i, _ in load if isinstance(i, Task)} | {
        t.id for m, _ in load if isinstance(m, Matter) for t in m.tasks}
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
    for item, hours in load:
        if item.deadline <= end:
            available -= hours
        else:  # se termine après : on n'en compte qu'une part proportionnelle
            available -= hours * min(1.0, window / max((item.deadline - start).total_seconds(), 1))
    return round(max(0.0, available), 1)


def estimated_finish(db: Session, user: User, item: Item, start: datetime, horizon_days: int = 60) -> datetime | None:
    """Date de fin estimée (délai de traitement proposé) : créneaux libres de l'agenda, après le reste à faire
    de la charge dont l'échéance est antérieure."""
    load = workload(db, user, item)
    needed = item.estimated_hours + sum(h for x, h in load if x.deadline <= item.deadline)
    end = start + timedelta(days=horizon_days)
    for a, b in _free_intervals(_work_intervals(start, end), _busy(db, user, start, end, load)):
        hours = (b - a).total_seconds() / 3600
        if hours >= needed:
            return a + timedelta(hours=needed)
        needed -= hours
    return None


# --------------------------------------------------------------------------- historique

def history(db: Session, user: User, exclude: Item | None = None) -> list[Item]:
    """Dossiers de ses équipes (collaborateurs, associés) ou tâches déléguées (stagiaires)."""
    if user.role == Role.INTERN:
        items = db.scalars(select(Task).where(Task.delegate_id == user.id))
    else:
        items = db.scalars(select(Matter).join(MatterMember).where(MatterMember.user_id == user.id))
    return [i for i in items if i is not exclude]


def _client_hours(db: Session, user: User, client_id: int) -> float:
    minutes = db.scalar(select(func.coalesce(func.sum(TimeEntry.minutes), 0)).join(Task).join(Matter).where(
        TimeEntry.user_id == user.id, Matter.client_id == client_id))
    return (minutes or 0) / 60


# --------------------------------------------------------------------------- 1. critères éliminatoires

def _eliminations(user: User, item: Item, conflicts: dict[int, str], free: float) -> list[tuple[str, str]]:
    out = []
    if user.id in conflicts:
        out.append(("conflict", ELIMINATORY["conflict"] + (f" : {conflicts[user.id]}" if conflicts[user.id] else "")))
    if item.specialty_id and not any(us.specialty_id == item.specialty_id for us in user.specialties):
        out.append(("domain", f"{ELIMINATORY['domain']} ({item.specialty.name})"))
    if item.sub_specialty_id and not any(us.sub_specialty_id == item.sub_specialty_id for us in user.sub_specialties):
        out.append(("sub_specialty", f"{ELIMINATORY['sub_specialty']} ({item.sub_specialty.name})"))
    if user.level < Role.LEVEL.get(item.min_level, 1):
        out.append(("hierarchy", f"{ELIMINATORY['hierarchy']} (minimum : {ROLE[item.min_level].lower()})"))
    needed = item.estimated_hours * config.MIN_AVAILABILITY_RATIO
    if isinstance(item, Matter):  # rejoindre une équipe : pouvoir avancer avant la prochaine échéance
        needed = min(needed, 0.25 * item.estimated_hours, config.MATTER_MIN_FREE_HOURS)
    if free < needed:
        out.append(("availability", f"{ELIMINATORY['availability']} ({free:g} h libres pour {needed:g} h nécessaires)"))
    if item.required_language and language_level(user, item.required_language) < MASTERED:
        out.append(("language", f"{ELIMINATORY['language']} ({item.required_language}, niveau C1 minimum)"))
    if item.required_jurisdiction and not _in(item.required_jurisdiction, user.jurisdictions):
        out.append(("jurisdiction", f"{ELIMINATORY['jurisdiction']} ({item.required_jurisdiction})"))
    return out


# --------------------------------------------------------------------------- 2. critères (informations pour le classement)

def _main_criteria(db: Session, c: Candidate, item: Item, client: Client, now: datetime,
                   max_free: float) -> list[Criterion]:
    user, past = c.user, history(db, c.user, item)
    values: dict[str, tuple[float | None, str]] = {}

    if item.specialty_id:
        us = next((s for s in user.specialties if s.specialty_id == item.specialty_id), None)
        level = us.level if us else 0
        values["domain_level"] = (level / 3, f"{SPECIALTY_LEVEL.get(level, 'Aucun niveau')} en {item.specialty.name}")
    else:
        values["domain_level"] = (None, "Pas de domaine imposé")

    if item.sub_specialty_id:
        uss = next((s for s in user.sub_specialties if s.sub_specialty_id == item.sub_specialty_id), None)
        level = uss.level if uss else 0
        values["sub_level"] = (level / 3, f"{SPECIALTY_LEVEL.get(level, 'Aucun niveau')} en {item.sub_specialty.name}")
    else:
        values["sub_level"] = (None, "Pas de sous-spécialité imposée")

    if item.sub_specialty_id:
        declared = next((s.cases_count for s in user.sub_specialties if s.sub_specialty_id == item.sub_specialty_id), 0)
        in_app = sum(1 for t in past if t.sub_specialty_id == item.sub_specialty_id)
    elif item.specialty_id:
        declared = next((s.cases_count for s in user.specialties if s.specialty_id == item.specialty_id), 0)
        in_app = sum(1 for t in past if t.specialty_id == item.specialty_id)
    else:
        declared, in_app = 0, 0
    count = declared + in_app
    values["similar_cases"] = (min(1.0, count / 15), f"{count} dossier{'s' if count > 1 else ''} similaire"
                                                     f"{'s' if count > 1 else ''} traité{'s' if count > 1 else ''}")

    if item.task_type:
        counts = json.loads(user.task_type_counts or "{}")
        n = counts.get(item.task_type, 0) + sum(1 for t in past if t.task_type == item.task_type)
        values["task_type"] = (min(1.0, n / 8), f"{n} dossier{'s' if n > 1 else ''} de type "
                                                f"« {TASK_TYPES[item.task_type][0].lower()} »")
    else:
        values["task_type"] = (None, "Type non précisé")

    values["workload"] = (c.free_hours / max_free if max_free else 0.0,
                          f"{c.free_hours:g} h libres avant l'échéance (charge estimée : {item.estimated_hours:g} h)")
    values["open_files"] = (max(0.0, 1 - c.active_tasks / 5),
                            f"{c.active_tasks} {'dossier' if user.role != Role.INTERN else 'tâche'}"
                            f"{'s' if c.active_tasks > 1 else ''} en cours")

    upcoming = [x for x, _ in workload(db, user, item) if x.deadline <= now + timedelta(days=14)]
    critical = sum(1 for x in upcoming if x.deadline <= now + timedelta(days=2))
    weight = sum(3 if x.deadline <= now + timedelta(days=2) else 2 if x.deadline <= now + timedelta(days=7) else 1
                 for x in upcoming)
    values["deadlines"] = (max(0.0, 1 - weight / 8),
                           f"{len(upcoming)} échéance{'s' if len(upcoming) > 1 else ''} dans les 14 jours"
                           + (f", dont {critical} sous 48 h" if critical else ""))

    if client.sector:
        ok = _in(client.sector, user.sectors)
        values["sector"] = (1.0 if ok else 0.0, f"{'Maîtrise' if ok else 'Ne connaît pas'} le secteur « {client.sector} »")
    else:
        values["sector"] = (None, "Secteur non renseigné")

    client_items = [t for t in past if t.client.id == client.id]
    values["client_knowledge"] = (min(1.0, len(client_items) / 2),
                                  f"A déjà travaillé {len(client_items)} fois pour ce client" if client_items
                                  else "N'a jamais travaillé pour ce client")
    hours = _client_hours(db, user, client.id)
    values["client_history"] = (min(1.0, hours / 20), f"{hours:g} h passées sur les dossiers de ce client")

    if client.language:
        level = language_level(user, client.language)
        inv = {v: k for k, v in CEFR.items()}
        values["client_language"] = (1.0 if level >= CEFR["B2"] else 0.5 if level >= CEFR["B1"] else 0.0,
                                     f"{client.language} : {inv.get(level, 'non parlé')}")
    else:
        values["client_language"] = (None, "Langue non renseignée")

    return [Criterion(k, label, "main", i, *values[k]) for i, (k, label) in enumerate(MAIN_CRITERIA.items(), 1)]


def _complementary_criteria(c: Candidate, item: Item, client: Client, now: datetime) -> list[Criterion]:
    user = c.user
    country = item.country or client.country
    legal_system = item.legal_system or DEFAULT_LEGAL_SYSTEM
    task_type = TASK_TYPES.get(item.task_type or "")
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
                        f"Aime : {', '.join(matching)}" if matching else "Préférences non alignées"),
    }
    return [Criterion(k, label, "complementary", i, *values[k])
            for i, (k, label) in enumerate(COMPLEMENTARY_CRITERIA.items(), 1)]


def _tier_score(criteria: list[Criterion], tier: str) -> float | None:
    group = [c for c in criteria if c.tier == tier]
    n = len(group)
    applicable = [(n + 1 - c.rank, c.value) for c in group if c.value is not None]
    total = sum(w for w, _ in applicable)
    return sum(w * v for w, v in applicable) / total if total else None


def local_score(criteria: list[Criterion]) -> float:
    """Calcul de repli (sans Mistral) : critères pondérés selon leur rang dans la hiérarchie."""
    main, comp = _tier_score(criteria, "main"), _tier_score(criteria, "complementary")
    share = config.MAIN_CRITERIA_SHARE
    if main is None or comp is None:
        return round(100 * (main if comp is None else comp if main is None else 0), 1)
    return round(100 * (share * main + (1 - share) * comp), 1)


# --------------------------------------------------------------------------- informations transmises à Mistral

def item_info(item: Item, now: datetime) -> dict:
    client = item.client
    info = {
        "nature": "dossier" if isinstance(item, Matter) else "tâche",
        "titre": item.title if isinstance(item, Task) else item.name,
        "description": item.description,
        "domaine": item.specialty.name if item.specialty else None,
        "sous_specialite": item.sub_specialty.name if item.sub_specialty else None,
        "type": TASK_TYPES[item.task_type][0] if item.task_type else None,
        "complexite": item.complexity,
        "charge_estimee_heures": item.estimated_hours,
        "echeance": item.deadline.strftime("%d/%m/%Y"),
        "jours_avant_echeance": max(0, (item.deadline - now).days),
        "niveau_hierarchique_minimum": ROLE.get(item.min_level),
        "langue_obligatoire": item.required_language,
        "juridiction_obligatoire": item.required_jurisdiction,
        "pays": item.country or client.country,
        "systeme_juridique": item.legal_system or DEFAULT_LEGAL_SYSTEM,
        # Le nom du client n'est pas transmis (pseudonymisation)
        "client": {"secteur": client.sector, "langue": client.language, "pays": client.country},
    }
    if isinstance(item, Matter):
        info["priorite_cabinet"] = item.priority
        info["equipe_actuelle"] = [f"{m.user.name} ({ROLE[m.user.role]})" for m in item.members]
    else:
        info["dossier"] = item.matter.name
    return info


def profile_info(db: Session, c: Candidate, item: Item, now: datetime) -> dict:
    u = c.user
    load = workload(db, u, item)
    return {
        "id": u.id,
        "nom": u.name,
        "niveau": ROLE[u.role],
        "anciennete_cabinet_ans": u.years_at_firm(now.date()),
        "annees_de_barreau": u.years_at_bar(now.date()) if u.bar_year else None,
        "specialisation": u.bio,
        "domaines": {us.specialty.name: {"niveau": SPECIALTY_LEVEL[us.level], "dossiers_traites": us.cases_count}
                     for us in u.specialties},
        "sous_specialites": {us.sub_specialty.name: {"niveau": SPECIALTY_LEVEL[us.level],
                                                     "dossiers_traites": us.cases_count} for us in u.sub_specialties},
        "langues": u.languages,
        "secteurs_maitrises": u.sectors,
        "pays_maitrises": u.countries,
        "systemes_juridiques": u.legal_systems,
        "preferences": u.preferences,
        "experience": {"contentieuse": f"{u.litigation_level}/3", "transactionnelle": f"{u.transactional_level}/3",
                       "internationale": f"{u.international_level}/2"},
        "charge_en_cours": [{"titre": x.title if isinstance(x, Task) else x.name,
                             "echeance": x.deadline.strftime("%d/%m"), "heures_restantes": round(h, 1)}
                            for x, h in load],
        "dossiers_de_ses_equipes": [x.name if isinstance(x, Matter) else x.title for x in history(db, u, item)][:12],
        "heures_libres_avant_echeance": c.free_hours,
        "fin_estimee": c.estimated_finish.strftime("%d/%m %Hh") if c.estimated_finish else "au-delà de 60 jours",
        "cout_estime_euros_ht": c.estimated_cost,
        "criteres_calcules": [{"critere": cr.label, "groupe": "principal" if cr.tier == "main" else "complémentaire",
                               "rang": cr.rank, "detail": cr.detail} for cr in c.criteria if cr.value is not None],
    }


def hierarchy_info() -> dict:
    return {
        "1_eliminatoires_dans_l_ordre": list(ELIMINATORY.values()),
        "2_principaux_dans_l_ordre": list(MAIN_CRITERIA.values()),
        "3_complementaires_dans_l_ordre": list(COMPLEMENTARY_CRITERIA.values()),
        "regles": ["Les critères principaux priment sur les complémentaires.",
                   "Dans chaque groupe, un critère mieux classé pèse davantage.",
                   "Une charge de travail plus élevée qu'un autre candidat n'est pas éliminatoire mais pénalise "
                   "fortement."],
    }


# --------------------------------------------------------------------------- classement

def _cache_key(item: Item) -> str:
    return f"{'matter' if isinstance(item, Matter) else 'task'}:{item.id}"


def _fingerprint(db: Session, item: Item, eligible: list[Candidate]) -> str:
    """Empreinte des informations qui changent le classement : élément, équipe, candidats éligibles et leur charge."""
    data = {
        "item": [item.specialty_id, item.sub_specialty_id, item.task_type, item.estimated_hours,
                 item.deadline.isoformat(), item.min_level, item.required_language, item.description],
        "team": [m.user_id for m in item.members] if isinstance(item, Matter) else [],
        "candidates": {str(c.user.id): sorted(f"{'m' if isinstance(x, Matter) else 't'}{x.id}"
                                              for x, _ in workload(db, c.user, item)) for c in eligible},
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def forget_ranking(db: Session, item: Item) -> None:
    row = db.get(RankingCache, _cache_key(item))
    if row:
        db.delete(row)
        db.flush()


def _apply(eligible: list[Candidate], payload: dict) -> list[Candidate]:
    by_id = {c.user.id: c for c in eligible}
    ordered = []
    for uid in payload["order"]:
        c = by_id.pop(uid, None)
        if c:
            c.score = payload["scores"].get(str(uid), c.score)
            c.explanation = payload["reasons"].get(str(uid), "")
            ordered.append(c)
    return ordered + sorted(by_id.values(), key=lambda c: -c.score)  # oubliés par l'IA : à la fin


def _ai_ranking(db: Session, item: Item, eligible: list[Candidate], excluded: list[Candidate], now: datetime,
                refresh: bool) -> list[Candidate] | None:
    key, fingerprint = _cache_key(item), _fingerprint(db, item, eligible)
    cached = db.get(RankingCache, key)
    fresh = cached and cached.fingerprint == fingerprint and now - cached.created_at < timedelta(hours=CACHE_HOURS)
    if fresh and not refresh:
        return _apply(eligible, json.loads(cached.payload))
    context = {
        "a_pourvoir": item_info(item, now),
        "hierarchie_des_criteres": hierarchy_info(),
        "candidats_eligibles": [profile_info(db, c, item, now) for c in eligible],
        "exclus_par_un_critere_eliminatoire": {c.user.name: c.eliminated for c in excluded},
    }
    result = ai.rank_candidates(context, [c.user.id for c in eligible])
    if result is None:  # Mistral indisponible : dernier classement connu s'il correspond encore, sinon calcul local
        return _apply(eligible, json.loads(cached.payload)) if cached and cached.fingerprint == fingerprint else None
    payload = json.dumps(result, ensure_ascii=False)
    if cached:
        cached.fingerprint, cached.payload, cached.created_at = fingerprint, payload, now
    else:
        db.add(RankingCache(key=key, fingerprint=fingerprint, payload=payload, created_at=now))
    db.flush()
    return _apply(eligible, result)


def rank_candidates(db: Session, item: Item, now: datetime | None = None, use_ai: bool = True,
                    refresh: bool = False) -> Ranking:
    """Classe les collaborateurs (dossier) ou les stagiaires (tâche). Les membres de l'équipe sont écartés."""
    now = now or clock.now(db)
    users = list(db.scalars(select(User).where(User.role.in_(pool_for(item))).order_by(User.name)))
    if isinstance(item, Matter):
        users = [u for u in users if not item.has_member(u)]
    client = item.client
    conflicts = {c.user_id: c.reason for c in db.scalars(select(Conflict).where(Conflict.client_id == client.id))}

    candidates = []
    for user in users:
        c = Candidate(user=user)
        c.free_hours = free_hours(db, user, now, item.deadline, exclude=item)
        c.active_tasks = len(workload(db, user, item))
        c.estimated_cost = round(item.estimated_hours * user.hourly_rate, 2)
        c.eliminations = _eliminations(user, item, conflicts, c.free_hours)
        candidates.append(c)

    eligible = [c for c in candidates if not c.eliminations]
    excluded = sorted((c for c in candidates if c.eliminations),
                      key=lambda c: list(ELIMINATORY).index(c.eliminations[0][0]))
    max_free = max((c.free_hours for c in eligible), default=0)
    for c in eligible:
        c.criteria = (_main_criteria(db, c, item, client, now, max_free)
                      + _complementary_criteria(c, item, client, now))
        c.score = local_score(c.criteria)
        c.estimated_finish = estimated_finish(db, c.user, item, now)
    eligible.sort(key=lambda c: (-c.score, *[-(cr.value or 0) for cr in c.criteria]))

    result = Ranking()
    if use_ai and eligible:
        ranked = _ai_ranking(db, item, eligible, excluded, now, refresh)
        if ranked is not None:
            eligible, result.source = ranked, "mistral"
    if result.source == "local":
        local = ai.explain_locally([{"id": c.user.id, "facts": c.facts} for c in eligible])
        for c in eligible:
            c.explanation = local.get(c.user.id, "")
    result.extend(eligible + excluded)
    return result


def compare(a: Candidate, b: Candidate, threshold: float = 0.15) -> list[tuple[Criterion, Criterion]]:
    """Critères où a et b diffèrent nettement, dans l'ordre hiérarchique (le premier est décisif)."""
    out = []
    for ca in a.criteria:
        cb = b.criterion(ca.key)
        if cb and ca.value is not None and cb.value is not None and abs(ca.value - cb.value) >= threshold:
            out.append((ca, cb))
    return out


def eligible_ids(db: Session, item: Item, now: datetime | None = None) -> dict[int, str | None]:
    """{user_id: motif d'élimination ou None} — validation côté serveur des choix de l'utilisateur."""
    return {c.user.id: c.eliminated for c in rank_candidates(db, item, now, use_ai=False)}
