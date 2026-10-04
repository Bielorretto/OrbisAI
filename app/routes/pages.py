"""Connexion de démo, tableaux de bord, notifications, fiches des personnes, outils de démo."""
import unicodedata
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import get_db
from app.labels import ROLE
from app.models import (Conflict, Delegation, DelegationStatus, EmailOutbox, Matter, MatterStatus, Notification,
                        Proposal, ProposalStatus, Role, Task, TaskStatus, User)
from app.routes.common import Forbidden, agenda_days, current_user, redirect, render
from app.services import scheduler
from app.services.scoring import logged_hours, workload
from app.services.workflow import current_proposal

router = APIRouter()


# Trois entrées à la connexion ; les partners passent par « Associé »
LOGIN_ROLES = {Role.PARTNER: Role.ASSIGNERS, Role.ASSOCIATE: (Role.ASSOCIATE,), Role.INTERN: (Role.INTERN,)}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.strip().lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def find_user_by_name(db: Session, role: str, name: str) -> User | None:
    """Nom complet, ou prénom / nom seul s'il est unique. Insensible à la casse et aux accents."""
    wanted = _normalize(name)
    if not wanted:
        return None
    users = db.scalars(select(User).where(User.role.in_(LOGIN_ROLES.get(role, ())))).all()
    exact = [u for u in users if _normalize(u.name) == wanted]
    if exact:
        return exact[0]
    partial = [u for u in users if wanted in _normalize(u.name).split()]
    return partial[0] if len(partial) == 1 else None


@router.get("/login")
def login_page(request: Request, next: str = "/", role: str | None = None, db: Session = Depends(get_db)):
    """Étape 1 : « Qui êtes-vous ? » (associé / collaborateur). Étape 2 : saisie de son nom."""
    return render(request, "login.html", db, None, role=role if role in LOGIN_ROLES else None, next=next)


@router.post("/login")
def login(user_id: int | None = Form(None), role: str = Form(""), name: str = Form(""), next: str = Form("/"),
          db: Session = Depends(get_db)):
    if user_id is not None:  # sélecteur d'utilisateur de la barre du haut
        user = db.get(User, user_id)
    else:
        user = find_user_by_name(db, role, name)
    if not user:
        return redirect(f"/login?role={role}&next={quote(next)}",
                        f"Aucun {ROLE.get(role, 'utilisateur').lower()} trouvé pour « {name} ».",
                        error=True)
    response = redirect(next if next.startswith("/") else "/", f"Connecté en tant que {user.name}")
    response.set_cookie("uid", str(user.id), httponly=True, samesite="lax")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("uid")
    return response


# --------------------------------------------------------------------------- tableaux de bord

@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    now = clock.now(db)
    if user.is_assigner:
        matters = db.scalars(select(Matter).where(Matter.created_by_id == user.id).order_by(Matter.deadline)).all()
        to_assign = [m for m in matters if m.status in (MatterStatus.TO_ASSIGN, MatterStatus.CASCADE_FAILED)]
        proposing = [(m, current_proposal(m)) for m in matters if m.status == MatterStatus.PROPOSING]
        active = [(m, _matter_progress(db, m)) for m in matters if m.status == MatterStatus.ACTIVE]
        closed = sorted([m for m in matters if m.status == MatterStatus.CLOSED],
                        key=lambda m: m.closed_at or m.deadline, reverse=True)[:6]
        late = [m for m in matters if m.status in MatterStatus.OPEN and m.deadline < now]
        return render(request, "dashboard_partner.html", db, user, to_assign=to_assign, proposing=proposing,
                      active=active, closed=closed, late=late)

    if user.role == Role.ASSOCIATE:
        proposals = db.scalars(select(Proposal).where(Proposal.user_id == user.id,
                                                      Proposal.status == ProposalStatus.PENDING)).all()
        matters = db.scalars(select(Matter).where(Matter.assignee_id == user.id, Matter.status == MatterStatus.ACTIVE)
                             .order_by(Matter.deadline)).all()
        mine = db.scalars(select(Task).where(Task.assignee_id == user.id, Task.status.in_(TaskStatus.OPEN))
                          .order_by(Task.deadline)).all()
        # Une seule liste, les tâches qui demandent une action d'abord
        order = {TaskStatus.ACCEPTED: 0, TaskStatus.IN_REVIEW: 1, TaskStatus.IN_PROGRESS: 2,
                 TaskStatus.DELEGATION_PENDING: 3}
        tasks = sorted(mine, key=lambda t: (order.get(t.status, 9), t.delegate_id is not None, t.deadline))
        return render(request, "dashboard_associate.html", db, user, proposals=proposals,
                      matters=[(m, _matter_progress(db, m)) for m in matters], tasks=tasks)

    delegations = db.scalars(select(Delegation).where(Delegation.to_user_id == user.id,
                                                      Delegation.status == DelegationStatus.PENDING)).all()
    mine = db.scalars(select(Task).where(Task.delegate_id == user.id).order_by(Task.deadline)).all()
    return render(request, "dashboard_intern.html", db, user, delegations=delegations,
                  working=[t for t in mine if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)],
                  done=[t for t in mine if t.status == TaskStatus.DONE])


def _matter_progress(db: Session, matter: Matter) -> dict:
    """Avancement d'un dossier : tâches ouvertes / terminées, heures passées vs charge estimée."""
    hours = logged_hours(db, matter)
    return {
        "open_tasks": sum(1 for t in matter.tasks if t.status in TaskStatus.OPEN),
        "done_tasks": sum(1 for t in matter.tasks if t.status == TaskStatus.DONE),
        "hours": round(hours, 1), "estimated_hours": matter.estimated_hours,
        "pct": round(100 * hours / matter.estimated_hours) if matter.estimated_hours else 0,
    }


# --------------------------------------------------------------------------- notifications

@router.get("/notifications")
def notifications(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    items = db.scalars(select(Notification).where(Notification.user_id == user.id)
                       .order_by(Notification.id.desc()).limit(100)).all()
    return render(request, "notifications.html", db, user, items=items)


@router.get("/notifications/{notification_id}")
def open_notification(notification_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    notification = db.get(Notification, notification_id)
    if not notification or notification.user_id != user.id:
        return redirect("/notifications", "Notification introuvable", error=True)
    notification.read = True
    db.commit()
    return redirect(notification.link)


@router.post("/notifications/read-all")
def read_all(db: Session = Depends(get_db), user: User = Depends(current_user)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read.is_(False))):
        n.read = True
    db.commit()
    return redirect("/notifications")


# --------------------------------------------------------------------------- personnes

def _person_context(db: Session, person: User, task: Matter | Task | None) -> dict:
    now = clock.now(db)
    end = task.deadline if task else now + timedelta(days=13)
    conflicts = db.scalars(select(Conflict).where(Conflict.user_id == person.id)).all()
    return {"person": person, "task": task, "days": agenda_days(db, person, now, end),
            "active": workload(db, person), "conflicts": conflicts,
            "years": person.years_at_bar(now.date())}


@router.get("/people")
def people(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    persons = db.scalars(select(User).order_by(User.role, User.name)).all()
    load = {p.id: len(workload(db, p)) for p in persons}
    return render(request, "people.html", db, user, persons=persons, load=load)


@router.get("/people/{person_id}")
def person(person_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    person = db.get(User, person_id)
    if not person:
        return redirect("/people", "Personne introuvable", error=True)
    return render(request, "person.html", db, user, standalone=True, **_person_context(db, person, None))


@router.get("/people/{person_id}/panel")
def person_panel(person_id: int, request: Request, task_id: int | None = None, matter_id: int | None = None,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Fragment HTML affiché dans le panneau latéral des écrans d'attribution et de délégation."""
    person = db.get(User, person_id)
    task = db.get(Matter, matter_id) if matter_id else db.get(Task, task_id) if task_id else None
    if not person:
        raise Forbidden()
    return render(request, "_person_panel.html", db, user, **_person_context(db, person, task))


# --------------------------------------------------------------------------- démo

@router.get("/demo")
def demo(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    emails = db.scalars(select(EmailOutbox).order_by(EmailOutbox.id.desc()).limit(30)).all()
    return render(request, "demo.html", db, user, emails=emails, offset=clock.get_offset(db))


@router.post("/demo/advance")
def demo_advance(hours: float = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    clock.advance(db, timedelta(hours=hours))
    summary = scheduler.tick(db)
    db.commit()
    return redirect("/demo", f"Temps avancé de {hours:g} h. Planificateur : {_summary(summary)}")


@router.post("/demo/tick")
def demo_tick(db: Session = Depends(get_db), user: User = Depends(current_user)):
    summary = scheduler.tick(db)
    db.commit()
    return redirect("/demo", f"Planificateur lancé : {_summary(summary)}")


@router.post("/demo/reset")
def demo_reset(user: User = Depends(current_user)):
    from app.seed import reset_database
    reset_database()
    response = redirect("/login", "Démo réinitialisée")
    response.delete_cookie("uid")
    return response


def _summary(summary: dict) -> str:
    return (f"{summary['to_assign']} notification(s) J-x, {summary['expired']} proposition(s) expirée(s), "
            f"{summary['reminders']} rappel(s) de deadline")

