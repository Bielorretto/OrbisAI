"""Création du cabinet de démonstration à partir de app/demo_data.py (profils, clients, dossiers, équipes, tâches).

Lancement manuel : `python -m app.seed` (réinitialise la base).
"""
import json
import random
import unicodedata
from datetime import date, datetime, timedelta

from sqlalchemy import MetaData
from sqlalchemy.orm import Session

from app import clock
from app.db import Base, SessionLocal, engine
from app.demo_data import CLIENTS, CONFLICTS, DOMAINS, MATTERS, PEOPLE, RATES
from app.models import (CalendarEvent, Client, Conflict, Delegation, DelegationStatus, Matter, MatterMember,
                        MatterStatus, Proposal, Role, Specialty, SubSpecialty, Task, TaskEvent,
                        TaskStatus, TeamRole, TimeEntry, TimeEntryStatus, User, UserSpecialty, UserSubSpecialty)
from app.services import ai


def reset_database() -> None:
    """Supprime TOUTES les tables présentes (y compris celles d'un ancien schéma), puis recrée la démo."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        sqlite = conn.dialect.name == "sqlite"
        if sqlite:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        existing = MetaData()
        existing.reflect(conn)
        existing.drop_all(conn)
        if sqlite:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    ai.reset_state()
    with SessionLocal() as db:
        seed(db)
        db.commit()


def _at(day: date, hour: float) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)


def _slug(name: str) -> str:
    return unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode().replace(" ", ".")


def seed(db: Session) -> None:
    clock.reset(db)
    now = clock.now(db)
    today = now.date()
    rng = random.Random(42)

    # ------------------------------------------------------------------ domaines
    specialties = {name: Specialty(name=name) for name in DOMAINS}
    subs = {sub: SubSpecialty(specialty=specialties[dom], name=sub) for dom, names in DOMAINS.items() for sub in names}
    db.add_all([*specialties.values(), *subs.values()])

    # ------------------------------------------------------------------ profils
    users: dict[str, User] = {}
    for p in PEOPLE:
        role = p["role"]
        bar_year = None if role == Role.INTERN else today.year - max(1, p["age"] - 26)
        user = User(name=p["name"], email=f"{_slug(p['name'])}@cabinet-demo.fr", role=role, bar_year=bar_year,
                    joined_year=today.year - p["years"], hourly_rate=RATES[role], languages=p["langs"],
                    bio=p["specialty"], weekly_capacity_hours=35 if role == Role.INTERN else 45,
                    sectors=p.get("sectors", ""), countries=p.get("countries", "France"),
                    legal_systems=p.get("legal", "Droit français"), jurisdictions=p.get("juris", "Paris"),
                    preferences=p.get("prefs", ""), litigation_level=p.get("lit", 0),
                    transactional_level=p.get("trans", 0), international_level=p.get("intl", 0),
                    task_type_counts=json.dumps(p.get("types", {})))
        user.specialties = [UserSpecialty(specialty=specialties[d], level=lvl, cases_count=n)
                            for d, (lvl, n) in p["domains"].items()]
        user.sub_specialties = [UserSubSpecialty(sub_specialty=subs[s], level=lvl, cases_count=n)
                                for s, (lvl, n) in p.get("subs", {}).items()]
        users[p["name"]] = user
    db.add_all(users.values())

    clients = {name: Client(name=name, sector=sector, language=lang, country=country)
               for name, (sector, lang, country) in CLIENTS.items()}
    db.add_all(clients.values())
    db.flush()
    for person, client, reason in CONFLICTS:
        db.add(Conflict(user_id=users[person].id, client_id=clients[client].id, reason=reason))

    # ------------------------------------------------------------------ agendas (6 semaines)
    templates = [("audience", "Audience", 9, 3), ("audience", "Audience – tribunal de commerce", 14, 3),
                 ("rdv", "Rendez-vous client", 10, 1.5), ("rdv", "Réunion d'équipe", 9, 1),
                 ("rdv", "Conférence téléphonique client", 16, 1), ("formation", "Formation continue", 14, 3)]
    for user in users.values():
        busy = {Role.PARTNER: 2, Role.SENIOR: 2}.get(user.role, 1)
        for offset in range(0, 43):
            day = today + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            taken: list[tuple[float, float]] = []
            for _ in range(rng.randint(0, busy)):
                kind, title, hour, length = rng.choice(templates)
                if user.role == Role.INTERN and kind == "audience":
                    title = "Assister à une audience"
                if any(not (hour + length <= a or hour >= b) for a, b in taken):
                    continue
                taken.append((hour, hour + length))
                db.add(CalendarEvent(user_id=user.id, title=title, kind=kind,
                                     start=_at(day, hour), end=_at(day, hour + length)))
    # Congés : démontre l'exclusion « disponibilité minimale »
    leave = today + timedelta(days=14 + (7 - today.weekday()) % 7)
    db.add(CalendarEvent(user_id=users["Maxime Laurent"].id, title="Congés", kind="conge",
                         start=_at(leave, 0), end=_at(leave + timedelta(days=5), 0)))
    db.flush()

    # ------------------------------------------------------------------ dossiers, équipes, tâches
    def event(m: Matter, kind: str, message: str, actor: User | None, when: datetime, t: Task | None = None):
        db.add(TaskEvent(matter_id=m.id, task_id=t.id if t else None, actor_id=actor.id if actor else None,
                         kind=kind, message=message, created_at=when))

    def member(m: Matter, name: str, team_role: str, when: datetime):
        m.members.append(MatterMember(user_id=users[name].id, team_role=team_role, joined_at=when))

    pending_notifications = []
    for d in MATTERS:
        created = now - timedelta(days=rng.randint(4, 25))
        complexity = d["complexity"]
        m = Matter(reference=f"{today.year}-{d['n']:03d}", name=d["name"],
                   description="\n".join(f"• {step}" for step in d["steps"]), client=clients[d["client"]],
                   specialty=specialties[d["domain"]], sub_specialty=subs[d["sub"]], task_type=d["type"],
                   complexity=complexity, estimated_hours=d["hours"], deadline=_at(today + timedelta(days=d["due"]), 18),
                   priority=d["priority"], min_level=Role.ASSOCIATE if complexity == 3 else Role.JUNIOR,
                   required_language=d.get("required_language"), country=d.get("country"),
                   legal_system=d.get("legal_system"), created_by=users[d["partners"][0]],
                   created_at=created, notify_days_before=7)
        db.add(m)
        db.flush()
        for name in d["partners"]:
            member(m, name, TeamRole.PARTNER, created)
        event(m, "created", f"Dossier ouvert par {users[d['partners'][0]].name}", users[d["partners"][0]], created)

        if d["status"] == "proposing":
            m.status = MatterStatus.PROPOSING
            m.notified_at = now - timedelta(days=1)
            sent = now - timedelta(hours=5)
            names = " → ".join(n for n, _ in d["proposals"])
            event(m, "selection", f"Sélection validée par {users[d['partners'][0]].name} : {names}",
                  users[d["partners"][0]], sent)
            for rank, (name, status) in enumerate(d["proposals"], 1):
                p = Proposal(matter_id=m.id, user_id=users[name].id, round=1, rank=rank, status=status)
                if status == "refused":
                    p.sent_at, p.expires_at, p.responded_at = sent, sent + timedelta(hours=4), sent + timedelta(hours=3)
                    p.refusal_reason = "overloaded"
                    event(m, "proposed", f"Proposé à {name} (choix n°{rank})", None, sent)
                    event(m, "refused", f"Refusé par {name} (Surcharge de travail)", users[name], p.responded_at)
                elif status == "pending":
                    p.sent_at = now - timedelta(hours=2)
                    p.expires_at = p.sent_at + timedelta(hours=4)
                    event(m, "proposed", f"Proposé à {name} (choix n°{rank})", None, p.sent_at)
                    pending_notifications.append((users[name], m, p))
                db.add(p)
            continue
        if d["status"] == "to_assign":
            continue

        # Dossier en cours : équipe en place
        m.status = MatterStatus.ACTIVE
        m.notified_at = created
        joined = created + timedelta(days=1)
        for name in d["lawyers"]:
            member(m, name, TeamRole.LAWYER, joined)
            event(m, "accepted", f"{name} rejoint l'équipe du dossier", users[name], joined)
        for name in d["interns"]:
            member(m, name, TeamRole.INTERN, joined)
        m.budget_amount = d["hours"] * users[d["lawyers"][0]].hourly_rate

        for title, owner, hours, due, status, intern, logged in d.get("tasks", []):
            lawyer = users[owner]
            task_created = joined + timedelta(hours=rng.randint(2, 48))
            deadline = _at(today + timedelta(days=due), 18)
            t = Task(matter=m, title=title, specialty=m.specialty, sub_specialty=m.sub_specialty, complexity=complexity,
                     estimated_hours=hours, deadline=deadline, created_by=lawyer, assignee=lawyer, status=status,
                     created_at=task_created, budget_amount=hours * lawyer.hourly_rate,
                     completed_at=now - timedelta(days=1) if status == TaskStatus.DONE else None)
            db.add(t)
            db.flush()
            event(m, "created", f"Tâche « {title} » créée par {lawyer.name}", lawyer, task_created, t)
            worker = lawyer
            if intern:
                stagiaire = users[intern]
                accepted = status != TaskStatus.DELEGATION_PENDING
                db.add(Delegation(task_id=t.id, from_user_id=lawyer.id, to_user_id=stagiaire.id,
                                  status=DelegationStatus.ACCEPTED if accepted else DelegationStatus.PENDING,
                                  created_at=task_created, responded_at=task_created if accepted else None))
                event(m, "delegated", f"{lawyer.name} délègue « {title} » à {stagiaire.name}", lawyer, task_created, t)
                if accepted:
                    t.delegate = stagiaire
                    worker = stagiaire
                else:
                    pending_notifications.append((stagiaire, t, None))
            if status == TaskStatus.DONE:
                event(m, "done", f"« {title} » terminée", worker, now - timedelta(days=1), t)
            if logged:
                # Temps saisis : validés (facturables) pour les tâches terminées, brouillon pour une partie du reste
                chunks = [logged] if logged <= 3 else [round(logged / 2, 1), logged - round(logged / 2, 1)]
                for i, h in enumerate(chunks):
                    draft = status != TaskStatus.DONE and i == len(chunks) - 1 and len(chunks) > 1
                    db.add(TimeEntry(user_id=worker.id, task_id=t.id, work_date=today - timedelta(days=2 + i * 2),
                                     minutes=int(h * 60), note=title.lower(), label=f"{title}.",
                                     rate=worker.hourly_rate, billable=worker.role != Role.INTERN,
                                     status=TimeEntryStatus.DRAFT if draft else TimeEntryStatus.VALIDATED,
                                     created_at=now - timedelta(days=2 + i * 2)))
            if status == TaskStatus.IN_PROGRESS:
                db.add(CalendarEvent(user_id=worker.id, title=title, kind="travail", task_id=t.id,
                                     start=_at(today, 14), end=_at(today, 16)))
    db.flush()

    from app.services.notifications import notify
    for user, item, proposal in pending_notifications:
        if proposal:
            notify(db, user, "proposal", f"{', '.join(p.name for p in item.partners)} vous propose(nt) le dossier "
                                         f"« {item.name} ». Réponse attendue avant le "
                                         f"{proposal.expires_at:%d/%m à %Hh%M}.", item)
        else:
            notify(db, user, "delegation", f"{item.assignee.name} vous confie « {item.title} » "
                                           f"(deadline {item.deadline:%d/%m}).", item)

    from app.services.scheduler import tick
    tick(db)


if __name__ == "__main__":
    reset_database()
    print("Base de démo réinitialisée.")
