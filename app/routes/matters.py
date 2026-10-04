"""Dossiers : ouverture par l'associé, attribution (cascade), réponse du collaborateur, création des tâches."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, config
from app.db import get_db
from app.labels import TASK_TYPES
from app.models import (Client, Matter, MatterStatus, Proposal, Role, Specialty, SubSpecialty, Task, TeamRole, TimeEntry,
                        User)
from app.routes.common import Forbidden, current_user, redirect, render, require_role
from app.services import ai, assistant, scheduler, workflow
from app.services.scoring import logged_hours, rank_candidates
from app.services.workflow import WorkflowError

router = APIRouter()


def act(db: Session, action, success_url: str, success: str, error_url: str):
    """Exécute une action métier : commit + message, ou rollback + message d'erreur."""
    try:
        action()
        db.commit()
        return redirect(success_url, success)
    except WorkflowError as exc:
        db.rollback()
        return redirect(error_url, str(exc), error=True)


def _get_matter(db: Session, matter_id: int) -> Matter:
    matter = db.get(Matter, matter_id)
    if not matter:
        raise Forbidden()
    return matter


def can_view_matter(matter: Matter, user: User) -> bool:
    involved = {matter.created_by_id} | {m.user_id for m in matter.members} | {p.user_id for p in matter.proposals}
    return user.is_assigner or user.id in involved


def specialty_tree(db: Session) -> tuple[list[Specialty], list[SubSpecialty], dict[str, list[str]]]:
    specialties = db.scalars(select(Specialty).order_by(Specialty.name)).all()
    subs = db.scalars(select(SubSpecialty).order_by(SubSpecialty.name)).all()
    tree = {s.name: [x.name for x in subs if x.specialty_id == s.id] for s in specialties}
    return specialties, subs, tree


def analyze_payload(db: Session, title: str, description: str):
    """Analyse Mistral d'un dossier ou d'une tâche : domaine, sous-spécialité, type, complexité, effort."""
    specialties, subs, tree = specialty_tree(db)
    try:
        result = ai.analyze_task(title, description, tree, {k: v[0] for k, v in TASK_TYPES.items()})
    except ai.AIError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    result["specialty_id"] = next((s.id for s in specialties if s.name == result["specialty"]), None)
    result["sub_specialty_id"] = next((x.id for x in subs if x.name == result["sub_specialty"]
                                       and x.specialty_id == result["specialty_id"]), None)
    return result


def parse_specialties(db: Session, specialty_id: str, sub_specialty_id: str) -> tuple[int | None, int | None]:
    sub = db.get(SubSpecialty, int(sub_specialty_id)) if sub_specialty_id else None
    if sub and (not specialty_id or sub.specialty_id != int(specialty_id)):
        raise WorkflowError("La sous-spécialité ne correspond pas au domaine choisi.")
    return (int(specialty_id) if specialty_id else None), (sub.id if sub else None)


# --------------------------------------------------------------------------- ouverture d'un dossier (associé)

@router.get("/matters/new")
def new_matter(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    specialties, subs, _ = specialty_tree(db)
    clients = db.scalars(select(Client).order_by(Client.name)).all()
    return render(request, "matter_new.html", db, user, clients=clients, specialties=specialties, subs=subs,
                  task_types=TASK_TYPES, default_notify=config.NOTIFY_DAYS_BEFORE_DEFAULT)


@router.post("/matters/analyze")
def analyze_matter(title: str = Form(""), description: str = Form(""), db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    return analyze_payload(db, title, description)


@router.post("/matters")
def create_matter(name: str = Form(...), description: str = Form(""), client_id: str = Form(""),
                  new_client_name: str = Form(""), new_client_sector: str = Form(""),
                  new_client_language: str = Form("Français"), new_client_country: str = Form("France"),
                  deadline: str = Form(...), specialty_id: str = Form(""), sub_specialty_id: str = Form(""),
                  task_type: str = Form(""), complexity: int = Form(2), estimated_hours: float = Form(...),
                  notify_days_before: int = Form(config.NOTIFY_DAYS_BEFORE_DEFAULT), ai_summary: str = Form(""),
                  required_language: str = Form(""), required_jurisdiction: str = Form(""), country: str = Form(""),
                  legal_system: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    try:
        if new_client_name.strip():
            client = Client(name=new_client_name.strip(), sector=new_client_sector.strip(),
                            language=new_client_language.strip() or "Français",
                            country=new_client_country.strip() or "France")
            db.add(client)
            db.flush()
        elif client_id:
            client = db.get(Client, int(client_id))
        else:
            raise WorkflowError("Choisissez un client ou créez-en un.")
        spec, sub = parse_specialties(db, specialty_id, sub_specialty_id)
        matter = workflow.create_matter(
            db, user, name=name, description=description, client_id=client.id,
            deadline=datetime.fromisoformat(deadline), estimated_hours=estimated_hours, specialty_id=spec,
            sub_specialty_id=sub, task_type=task_type if task_type in TASK_TYPES else None, complexity=complexity,
            notify_days_before=notify_days_before, ai_summary=ai_summary or None,
            required_language=required_language, required_jurisdiction=required_jurisdiction, country=country,
            legal_system=legal_system)
        scheduler.tick(db)  # si l'échéance est déjà dans la fenêtre J-x, l'associé est notifié tout de suite
        db.commit()
    except (WorkflowError, ValueError) as exc:
        db.rollback()
        return redirect("/matters/new", str(exc), error=True)
    if matter.notified_at:
        return redirect(f"/matters/{matter.id}/assign",
                        "Dossier ouvert. L'échéance est proche : choisissez à qui le confier.")
    return redirect(f"/matters/{matter.id}", f"Dossier ouvert. Vous serez notifié à J-{matter.notify_days_before}.")


# --------------------------------------------------------------------------- détail

@router.get("/matters/{matter_id}")
def matter_detail(matter_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    matter = _get_matter(db, matter_id)
    if not can_view_matter(matter, user):
        raise Forbidden()
    my_proposal = next((p for p in matter.proposals if p.user_id == user.id and p.status == "pending"), None)
    hours = logged_hours(db, matter)
    amount = sum(e.amount for e in db.scalars(select(TimeEntry).join(Task).where(Task.matter_id == matter.id)))
    return render(request, "matter_detail.html", db, user, matter=matter, my_proposal=my_proposal,
                  current=workflow.current_proposal(matter), hours=round(hours, 1), amount=amount,
                  pct=round(100 * hours / matter.estimated_hours) if matter.estimated_hours else 0,
                  is_late=matter.status in MatterStatus.OPEN and matter.deadline < clock.now(db),
                  assistant_pool=assistant.pool_for(user, matter))


# --------------------------------------------------------------------------- attribution (associé)

@router.get("/matters/{matter_id}/assign")
def assign_page(matter_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    matter = _get_matter(db, matter_id)
    if not workflow.is_partner_of(matter, user):
        return redirect(f"/matters/{matter.id}", "Seuls les associés du dossier peuvent l'attribuer.", error=True)
    if matter.status == MatterStatus.CLOSED or workflow.current_proposal(matter):
        return redirect(f"/matters/{matter.id}", "Une proposition est déjà en attente de réponse sur ce dossier.")
    refresh = request.query_params.get("refresh") == "1"
    candidates = rank_candidates(db, matter, clock.now(db), refresh=refresh)
    db.commit()  # classement Mistral mémorisé
    refused = {p.user_id for p in matter.proposals}
    eligible = sum(1 for c in candidates if not c.eliminated)
    return render(request, "assign.html", db, user, matter=matter, candidates=candidates, refused=refused,
                  min_choices=workflow.min_choices(eligible), eligible_count=eligible, source=candidates.source,
                  response_delay=config.RESPONSE_DELAY_HOURS,
                  assistant_suggestions=assistant.suggestions(candidates, Role.ASSOCIATE))


@router.post("/matters/{matter_id}/assign")
async def assign(matter_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    user_ids = [int(v) for v in form.getlist("user_ids")]
    scores = {int(k.removeprefix("score_")): float(v) for k, v in form.items() if k.startswith("score_")}
    matter = _get_matter(db, matter_id)
    return act(db, lambda: workflow.start_proposals(db, matter, user, user_ids, scores),
               f"/matters/{matter.id}", "Sélection validée : le dossier est envoyé au premier collaborateur.",
               f"/matters/{matter.id}/assign")


@router.post("/matters/{matter_id}/close")
def close(matter_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    matter = _get_matter(db, matter_id)
    return act(db, lambda: workflow.close_matter(db, matter, user), f"/matters/{matter_id}", "Dossier clôturé.",
               f"/matters/{matter_id}")


# --------------------------------------------------------------------------- réponse du collaborateur

@router.post("/proposals/{proposal_id}/accept")
def accept(proposal_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    proposal = db.get(Proposal, proposal_id)
    url = f"/matters/{proposal.matter_id}" if proposal else "/"
    return act(db, lambda: workflow.accept_proposal(db, proposal_id, user), url,
               "Dossier accepté : vous pouvez maintenant y créer des tâches.", url)


@router.post("/proposals/{proposal_id}/refuse")
def refuse(proposal_id: int, reason: str = Form(...), comment: str = Form(""), db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    return act(db, lambda: workflow.refuse_proposal(db, proposal_id, user, reason, comment), "/",
               "Dossier refusé. Il a été transmis automatiquement.", "/")


# --------------------------------------------------------------------------- création d'une tâche (collaborateur)

@router.get("/matters/{matter_id}/tasks/new")
def new_task(matter_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    matter = _get_matter(db, matter_id)
    if not matter.has_member(user, TeamRole.LAWYER) or matter.status != MatterStatus.ACTIVE:
        return redirect(f"/matters/{matter_id}", "Seuls les collaborateurs de l'équipe peuvent créer des tâches.",
                        error=True)
    specialties, subs, _ = specialty_tree(db)
    return render(request, "task_new.html", db, user, matter=matter, specialties=specialties, subs=subs,
                  task_types=TASK_TYPES)


@router.post("/tasks/analyze")
def analyze_task(title: str = Form(""), description: str = Form(""), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    require_role(user, *Role.LAWYERS)
    return analyze_payload(db, title, description)


@router.post("/matters/{matter_id}/tasks")
def create_task(matter_id: int, title: str = Form(...), description: str = Form(""), deadline: str = Form(...),
                estimated_hours: float = Form(...), specialty_id: str = Form(""), sub_specialty_id: str = Form(""),
                task_type: str = Form(""), complexity: int = Form(2), ai_summary: str = Form(""),
                min_level: str = Form(Role.INTERN), required_language: str = Form(""),
                required_jurisdiction: str = Form(""), next_step: str = Form("open"),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    matter = _get_matter(db, matter_id)
    try:
        spec, sub = parse_specialties(db, specialty_id, sub_specialty_id)
        task = workflow.create_task(
            db, user, matter, title=title, description=description, deadline=datetime.fromisoformat(deadline),
            estimated_hours=estimated_hours, specialty_id=spec, sub_specialty_id=sub,
            task_type=task_type if task_type in TASK_TYPES else None, complexity=complexity,
            ai_summary=ai_summary or None, min_level=min_level, required_language=required_language,
            required_jurisdiction=required_jurisdiction)
        if next_step == "myself":
            workflow.work_myself(db, task, user)
        db.commit()
    except (WorkflowError, ValueError) as exc:
        db.rollback()
        return redirect(f"/matters/{matter_id}/tasks/new", str(exc), error=True)
    if next_step == "delegate":
        return redirect(f"/tasks/{task.id}/delegate", "Tâche créée : choisissez le stagiaire.")
    return redirect(f"/tasks/{task.id}", "Tâche créée." + (" Elle est en cours." if next_step == "myself" else ""))


# --------------------------------------------------------------------------- assistant (chatbot)

class AssistantQuestion(BaseModel):
    message: str
    history: list[dict] = []


def assistant_response(db: Session, user: User, item, body: AssistantQuestion):
    try:
        result = assistant.answer(db, user, item, body.message, body.history)
    except assistant.AssistantError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except ai.AIError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    db.commit()  # embeddings éventuellement calculés
    return result


@router.post("/matters/{matter_id}/assistant")
def ask_assistant(matter_id: int, body: AssistantQuestion, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    matter = _get_matter(db, matter_id)
    if not can_view_matter(matter, user):
        raise Forbidden()
    return assistant_response(db, user, matter, body)
