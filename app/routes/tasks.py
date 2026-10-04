"""Tâches (créées par le collaborateur dans ses dossiers) : faire soi-même ou déléguer, revue, clôture."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import get_db
from app.models import Delegation, Role, Task, TaskStatus, TimeEntry, User
from app.routes.common import Forbidden, current_user, redirect, render
from app.routes.matters import AssistantQuestion, act as _act, assistant_response
from app.services import assistant, billing, workflow
from app.services.scoring import rank_candidates

router = APIRouter()


def _get_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise Forbidden()
    return task


def _can_view(task: Task, user: User) -> bool:
    involved = ({task.assignee_id, task.delegate_id} | {m.user_id for m in task.matter.members}
                | {d.to_user_id for d in task.delegations})
    return user.is_assigner or user.id in involved


# --------------------------------------------------------------------------- détail

@router.get("/tasks/{task_id}")
def task_detail(task_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    if not _can_view(task, user):
        raise Forbidden()
    delegation = workflow.pending_delegation(db, task)
    show_time = user.is_assigner or user.id in (task.assignee_id, task.delegate_id)
    entries = []
    if show_time:
        entries = db.scalars(select(TimeEntry).where(TimeEntry.task_id == task.id)
                             .order_by(TimeEntry.work_date.desc())).all()
    return render(request, "task_detail.html", db, user, task=task, delegation=delegation,
                  budget=billing.task_budget(db, task),
                  entries=entries, show_time=show_time, can_log=billing.can_log_time(task, user),
                  assistant_pool=assistant.pool_for(user, task),
                  is_late=task.status in TaskStatus.OPEN and task.deadline < clock.now(db))


# --------------------------------------------------------------------------- faire ou déléguer

@router.post("/tasks/{task_id}/work-myself")
def work_myself(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    return _act(db, lambda: workflow.work_myself(db, task, user), f"/tasks/{task_id}",
                "C'est parti : la tâche est en cours.", f"/tasks/{task_id}")


@router.get("/tasks/{task_id}/delegate")
def delegate_page(task_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    if task.assignee_id != user.id or task.status != TaskStatus.ACCEPTED:
        return redirect(f"/tasks/{task_id}", "Délégation impossible dans l'état actuel.", error=True)
    refresh = request.query_params.get("refresh") == "1"
    candidates = rank_candidates(db, task, clock.now(db), refresh=refresh)
    db.commit()
    refused = {d.to_user_id for d in db.scalars(select(Delegation).where(Delegation.task_id == task.id))}
    return render(request, "delegate.html", db, user, task=task, candidates=candidates, refused=refused,
                  source=candidates.source,
                  assistant_suggestions=assistant.suggestions(candidates, Role.INTERN))


@router.post("/tasks/{task_id}/delegate")
def delegate(task_id: int, intern_id: int = Form(...), db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    return _act(db, lambda: workflow.delegate(db, task, user, intern_id), f"/tasks/{task_id}",
                "Tâche déléguée : le stagiaire a été notifié.", f"/tasks/{task_id}/delegate")


@router.post("/delegations/{delegation_id}/accept")
def accept_delegation(delegation_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    delegation = db.get(Delegation, delegation_id)
    url = f"/tasks/{delegation.task_id}" if delegation else "/"
    return _act(db, lambda: workflow.accept_delegation(db, delegation_id, user), url, "Délégation acceptée.", "/")


@router.post("/delegations/{delegation_id}/refuse")
def refuse_delegation(delegation_id: int, reason: str = Form(...), db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    return _act(db, lambda: workflow.refuse_delegation(db, delegation_id, user, reason), "/",
                "Délégation refusée : la tâche est revenue au collaborateur.", "/")


# --------------------------------------------------------------------------- revue et clôture

@router.post("/tasks/{task_id}/submit-review")
def submit_review(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    return _act(db, lambda: workflow.submit_for_review(db, task, user), f"/tasks/{task_id}",
                "Travail envoyé en relecture.", f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/approve")
def approve(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    return _act(db, lambda: workflow.approve_review(db, task, user), f"/tasks/{task_id}",
                "Travail validé : la tâche est terminée.", f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/request-changes")
def request_changes(task_id: int, comment: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    return _act(db, lambda: workflow.request_changes(db, task, user, comment), f"/tasks/{task_id}",
                "Corrections demandées.", f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/complete")
def complete(task_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    return _act(db, lambda: workflow.complete(db, task, user), f"/tasks/{task_id}",
                "Tâche terminée. Pensez à valider vos temps.", f"/tasks/{task_id}")


# --------------------------------------------------------------------------- assistant (chatbot)

@router.post("/tasks/{task_id}/assistant")
def ask_assistant(task_id: int, body: AssistantQuestion, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    task = _get_task(db, task_id)
    if not _can_view(task, user):
        raise Forbidden()
    return assistant_response(db, user, task, body)
