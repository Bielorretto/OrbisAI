"""Cycle de vie des dossiers et des tâches.

DOSSIER (attribué par l'associé) :
    À ATTRIBUER -> EN PROPOSITION (collaborateur n°1 -> n°2 -> n°3)
        accepté   -> EN COURS (le collaborateur devient responsable du dossier) -> CLÔTURÉ
        3 refus   -> REFUSÉ PAR TOUS -> l'associé refait une sélection (round suivant)

TÂCHE (créée par le collaborateur responsable, dans un de ses dossiers) :
    À LANCER -> « je la fais »   -> EN COURS -> TERMINÉE
             -> « je délègue »   -> DÉLÉGATION EN ATTENTE -> EN COURS (stagiaire) -> EN REVUE -> TERMINÉE
                                    (refus du stagiaire : retour à À LANCER)
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, config
from app.labels import refusal
from app.models import (Client, Delegation, DelegationStatus, Matter, MatterStatus, Proposal, ProposalStatus, Role,
                        Task, TaskStatus, User)
from app.services.notifications import log_event, notify
from app.services.scoring import eligible_ids


class WorkflowError(Exception):
    """Action impossible dans l'état actuel (message affiché à l'utilisateur)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkflowError(message)


def _eligibility(db: Session, item: Matter | Task, role: str) -> dict[int, str | None]:
    """{user_id: motif d'élimination ou None}. Les critères éliminatoires sont revérifiés côté serveur."""
    return eligible_ids(db, item, role, clock.now(db))


def min_choices(eligible_count: int) -> int:
    """Au moins MIN_PROPOSALS, sauf s'il y a moins de candidats éligibles que ça."""
    return max(1, min(config.MIN_PROPOSALS, eligible_count))


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def _requirements(min_level: str, required_language, required_jurisdiction, country, legal_system) -> dict:
    _require(min_level in Role.LEVEL, "Niveau hiérarchique inconnu.")
    return dict(min_level=min_level, required_language=_clean(required_language),
                required_jurisdiction=_clean(required_jurisdiction), country=_clean(country),
                legal_system=_clean(legal_system))


# =========================================================================== DOSSIERS

def next_reference(db: Session) -> str:
    year = clock.now(db).year
    count = db.scalar(select(func.count()).select_from(Matter).where(Matter.reference.like(f"{year}-%"))) or 0
    while True:
        count += 1
        reference = f"{year}-{count:03d}"
        if not db.scalar(select(Matter.id).where(Matter.reference == reference)):
            return reference


def create_matter(db: Session, partner: User, *, name: str, description: str, client_id: int, deadline: datetime,
                  estimated_hours: float, specialty_id: int | None = None, sub_specialty_id: int | None = None,
                  task_type: str | None = None, complexity: int = 2,
                  notify_days_before: int = config.NOTIFY_DAYS_BEFORE_DEFAULT, ai_summary: str | None = None,
                  min_level: str = Role.ASSOCIATE, required_language: str | None = None,
                  required_jurisdiction: str | None = None, country: str | None = None,
                  legal_system: str | None = None) -> Matter:
    _require(partner.is_assigner, "Seul un associé ou un partner peut ouvrir un dossier.")
    _require(bool(name.strip()), "Le nom du dossier est obligatoire.")
    _require(db.get(Client, client_id) is not None, "Client inconnu.")
    _require(deadline > clock.now(db), "L'échéance doit être dans le futur.")
    _require(estimated_hours > 0, "La charge estimée doit être positive.")
    matter = Matter(reference=next_reference(db), name=name.strip(), description=description.strip(),
                    client_id=client_id, deadline=deadline, estimated_hours=estimated_hours,
                    specialty_id=specialty_id, sub_specialty_id=sub_specialty_id, task_type=task_type or None,
                    complexity=min(3, max(1, complexity)), notify_days_before=notify_days_before,
                    created_by_id=partner.id, status=MatterStatus.TO_ASSIGN, created_at=clock.now(db),
                    ai_summary=ai_summary,
                    **_requirements(min_level, required_language, required_jurisdiction, country, legal_system))
    db.add(matter)
    db.flush()
    log_event(db, matter, "created", f"Dossier ouvert par {partner.name}", partner)
    return matter


def current_round(matter: Matter) -> int:
    return max((p.round for p in matter.proposals), default=0)


def current_proposal(matter: Matter) -> Proposal | None:
    return next((p for p in matter.proposals if p.status == ProposalStatus.PENDING), None)


def start_proposals(db: Session, matter: Matter, partner: User, user_ids: list[int],
                    scores: dict[int, float] | None = None) -> None:
    """L'associé valide sa sélection ordonnée : on crée les propositions et on envoie le dossier au n°1."""
    _require(partner.id == matter.created_by_id, "Seul l'associé responsable du dossier peut l'attribuer.")
    _require(matter.status in (MatterStatus.TO_ASSIGN, MatterStatus.CASCADE_FAILED), "Ce dossier est déjà attribué.")
    ordered = list(dict.fromkeys(user_ids))  # dédoublonne en gardant l'ordre
    users = [db.get(User, uid) for uid in ordered]
    for user in users:
        _require(user is not None and user.role == Role.ASSOCIATE, "Seuls des collaborateurs peuvent être choisis.")
    eligibility = _eligibility(db, matter, Role.ASSOCIATE)
    for user in users:
        _require(eligibility.get(user.id) is None, f"{user.name} est exclu : {eligibility.get(user.id)}.")
    needed = min_choices(sum(1 for reason in eligibility.values() if reason is None))
    _require(len(ordered) >= needed, f"Choisissez au moins {needed} collaborateur{'s' if needed > 1 else ''}.")

    round_no = current_round(matter) + 1
    for rank, user in enumerate(users, start=1):
        matter.proposals.append(Proposal(matter_id=matter.id, user_id=user.id, round=round_no, rank=rank,
                                         status=ProposalStatus.QUEUED, score=(scores or {}).get(user.id)))
    matter.status = MatterStatus.PROPOSING
    db.flush()
    names = " → ".join(u.name for u in users)
    log_event(db, matter, "selection", f"Sélection validée par {partner.name} : {names}", partner)
    _activate_next(db, matter)


def _activate_next(db: Session, matter: Matter) -> None:
    round_no = current_round(matter)
    queued = [p for p in matter.proposals if p.round == round_no and p.status == ProposalStatus.QUEUED]
    if not queued:
        matter.status = MatterStatus.CASCADE_FAILED
        log_event(db, matter, "cascade_failed", "Aucun des collaborateurs choisis n'a accepté : retour à l'associé")
        notify(db, matter.created_by, "cascade_failed",
               f"Personne n'a accepté le dossier « {matter.name} ». Choisissez de nouveaux collaborateurs.",
               matter, link=f"/matters/{matter.id}/assign", email_subject=f"Dossier non attribué : {matter.name}")
        db.flush()
        return
    proposal = min(queued, key=lambda p: p.rank)
    now = clock.now(db)
    proposal.status = ProposalStatus.PENDING
    proposal.sent_at = now
    proposal.expires_at = now + timedelta(hours=config.RESPONSE_DELAY_HOURS)
    db.flush()
    log_event(db, matter, "proposed", f"Proposé à {proposal.user.name} (choix n°{proposal.rank})")
    # Le collaborateur ne voit pas son rang : il ne sait pas s'il est le premier choix.
    notify(db, proposal.user, "proposal",
           f"{matter.created_by.name} vous propose le dossier « {matter.name} » (échéance "
           f"{matter.deadline:%d/%m à %Hh%M}). Réponse attendue avant le {proposal.expires_at:%d/%m à %Hh%M}.",
           matter, email_subject=f"Nouveau dossier proposé : {matter.name}")


def _pending_for(db: Session, proposal_id: int, user: User) -> Proposal:
    proposal = db.get(Proposal, proposal_id)
    _require(proposal is not None, "Proposition introuvable.")
    _require(proposal.user_id == user.id, "Cette proposition ne vous est pas adressée.")
    _require(proposal.status == ProposalStatus.PENDING, "Cette proposition n'est plus en attente.")
    return proposal


def accept_proposal(db: Session, proposal_id: int, user: User) -> Matter:
    proposal = _pending_for(db, proposal_id, user)
    matter = proposal.matter
    proposal.status = ProposalStatus.ACCEPTED
    proposal.responded_at = clock.now(db)
    for p in matter.proposals:
        if p.round == proposal.round and p.status == ProposalStatus.QUEUED:
            p.status = ProposalStatus.CANCELLED
    matter.status = MatterStatus.ACTIVE
    matter.assignee_id = user.id
    matter.budget_amount = round(matter.estimated_hours * user.hourly_rate, 2)
    db.flush()
    log_event(db, matter, "accepted", f"Dossier accepté par {user.name}", user)
    notify(db, matter.created_by, "accepted", f"{user.name} a accepté le dossier « {matter.name} ».", matter,
           email_subject=f"Dossier accepté : {matter.name}")
    return matter


def refuse_proposal(db: Session, proposal_id: int, user: User, reason: str, comment: str = "") -> Matter:
    proposal = _pending_for(db, proposal_id, user)
    proposal.status = ProposalStatus.REFUSED
    proposal.responded_at = clock.now(db)
    proposal.refusal_reason = reason
    proposal.refusal_comment = comment.strip() or None
    db.flush()
    log_event(db, proposal.matter, "refused", f"Refusé par {user.name} ({refusal(reason)})", user)
    _activate_next(db, proposal.matter)
    return proposal.matter


def expire_proposals(db: Session, now: datetime) -> int:
    """Propositions sans réponse dans le délai : on passe au suivant (appelé par le planificateur)."""
    expired = list(db.scalars(select(Proposal).where(
        Proposal.status == ProposalStatus.PENDING, Proposal.expires_at <= now)))
    for proposal in expired:
        proposal.status = ProposalStatus.EXPIRED
        proposal.responded_at = now
        db.flush()
        log_event(db, proposal.matter, "expired",
                  f"Pas de réponse de {proposal.user.name} dans les {config.RESPONSE_DELAY_HOURS} h")
        notify(db, proposal.user, "expired", f"La proposition du dossier « {proposal.matter.name} » a expiré.",
               proposal.matter)
        _activate_next(db, proposal.matter)
    return len(expired)


def close_matter(db: Session, matter: Matter, user: User) -> None:
    _require(matter.status == MatterStatus.ACTIVE, "Seul un dossier en cours peut être clôturé.")
    _require(user.id in (matter.assignee_id, matter.created_by_id),
             "Seuls le collaborateur responsable et l'associé peuvent clôturer le dossier.")
    open_tasks = [t for t in matter.tasks if t.status in TaskStatus.OPEN]
    _require(not open_tasks, f"{len(open_tasks)} tâche(s) encore ouverte(s) dans ce dossier.")
    matter.status = MatterStatus.CLOSED
    matter.closed_at = clock.now(db)
    db.flush()
    log_event(db, matter, "closed", f"Dossier clôturé par {user.name}", user)
    other = matter.created_by if user.id == matter.assignee_id else matter.assignee
    notify(db, other, "closed", f"Le dossier « {matter.name} » a été clôturé.", matter)


# =========================================================================== TÂCHES

def create_task(db: Session, user: User, matter: Matter, *, title: str, description: str, deadline: datetime,
                estimated_hours: float, specialty_id: int | None = None, sub_specialty_id: int | None = None,
                task_type: str | None = None, complexity: int = 2, ai_summary: str | None = None,
                min_level: str = Role.INTERN, required_language: str | None = None,
                required_jurisdiction: str | None = None, country: str | None = None,
                legal_system: str | None = None) -> Task:
    """Le collaborateur responsable crée une tâche dans son dossier. Domaine et sous-spécialité reprennent ceux
    du dossier s'ils ne sont pas précisés (ils servent au choix du stagiaire)."""
    _require(matter.status == MatterStatus.ACTIVE, "On ne peut créer des tâches que dans un dossier en cours.")
    _require(user.id == matter.assignee_id, "Seul le collaborateur responsable du dossier peut y créer des tâches.")
    _require(bool(title.strip()), "Le titre est obligatoire.")
    _require(deadline > clock.now(db), "La deadline doit être dans le futur.")
    _require(estimated_hours > 0, "L'effort estimé doit être positif.")
    if not specialty_id:
        specialty_id, sub_specialty_id = matter.specialty_id, sub_specialty_id or matter.sub_specialty_id
    task = Task(title=title.strip(), description=description.strip(), matter_id=matter.id, deadline=deadline,
                specialty_id=specialty_id, sub_specialty_id=sub_specialty_id, task_type=task_type or None,
                complexity=min(3, max(1, complexity)), estimated_hours=estimated_hours, created_by_id=user.id,
                assignee_id=user.id, status=TaskStatus.ACCEPTED, created_at=clock.now(db), ai_summary=ai_summary,
                budget_amount=round(estimated_hours * user.hourly_rate, 2),
                **_requirements(min_level, required_language, required_jurisdiction, country, legal_system))
    db.add(task)
    db.flush()
    log_event(db, task, "created", f"Tâche « {task.title} » créée par {user.name}", user)
    return task


def _require_assignee(task: Task, user: User, status: str) -> None:
    _require(task.assignee_id == user.id, "Vous n'êtes pas responsable de cette tâche.")
    _require(task.status == status, "Action impossible dans l'état actuel de la tâche.")


def work_myself(db: Session, task: Task, user: User) -> None:
    _require_assignee(task, user, TaskStatus.ACCEPTED)
    task.status = TaskStatus.IN_PROGRESS
    db.flush()
    log_event(db, task, "in_progress", f"{user.name} réalise « {task.title} » lui-même / elle-même", user)


def delegate(db: Session, task: Task, user: User, intern_id: int) -> Delegation:
    _require_assignee(task, user, TaskStatus.ACCEPTED)
    intern = db.get(User, intern_id)
    _require(intern is not None and intern.role == Role.INTERN, "On ne peut déléguer qu'à un stagiaire.")
    reason = _eligibility(db, task, Role.INTERN).get(intern.id)
    _require(reason is None, f"{intern.name} est exclu·e : {reason}.")
    delegation = Delegation(task_id=task.id, from_user_id=user.id, to_user_id=intern.id,
                            status=DelegationStatus.PENDING, created_at=clock.now(db))
    db.add(delegation)
    task.status = TaskStatus.DELEGATION_PENDING
    db.flush()
    log_event(db, task, "delegated", f"{user.name} délègue « {task.title} » à {intern.name}", user)
    notify(db, intern, "delegation", f"{user.name} vous confie « {task.title} » "
                                     f"(deadline {task.deadline:%d/%m à %Hh%M}).",
           task, email_subject=f"Tâche déléguée : {task.title}")
    return delegation


def pending_delegation(db: Session, task: Task) -> Delegation | None:
    return db.scalar(select(Delegation).where(Delegation.task_id == task.id,
                                              Delegation.status == DelegationStatus.PENDING))


def _pending_delegation_for(db: Session, delegation_id: int, intern: User) -> Delegation:
    delegation = db.get(Delegation, delegation_id)
    _require(delegation is not None and delegation.to_user_id == intern.id, "Délégation introuvable.")
    _require(delegation.status == DelegationStatus.PENDING, "Cette délégation n'est plus en attente.")
    return delegation


def accept_delegation(db: Session, delegation_id: int, intern: User) -> Task:
    delegation = _pending_delegation_for(db, delegation_id, intern)
    delegation.status = DelegationStatus.ACCEPTED
    delegation.responded_at = clock.now(db)
    task = delegation.task
    task.delegate_id = intern.id
    task.status = TaskStatus.IN_PROGRESS
    db.flush()
    log_event(db, task, "delegation_accepted", f"{intern.name} a accepté « {task.title} »", intern)
    notify(db, delegation.from_user, "delegation_accepted", f"{intern.name} a accepté « {task.title} ».", task)
    return task


def refuse_delegation(db: Session, delegation_id: int, intern: User, reason: str) -> Task:
    """Le stagiaire refuse : la tâche revient au collaborateur, qui choisit à nouveau."""
    delegation = _pending_delegation_for(db, delegation_id, intern)
    delegation.status = DelegationStatus.REFUSED
    delegation.responded_at = clock.now(db)
    delegation.refusal_reason = reason
    task = delegation.task
    task.status = TaskStatus.ACCEPTED
    db.flush()
    log_event(db, task, "delegation_refused", f"{intern.name} a refusé « {task.title} » ({refusal(reason)})", intern)
    notify(db, delegation.from_user, "delegation_refused",
           f"{intern.name} a refusé « {task.title} ». Faites-la vous-même ou choisissez un autre stagiaire.", task)
    return task


def submit_for_review(db: Session, task: Task, intern: User) -> None:
    _require(task.delegate_id == intern.id and task.status == TaskStatus.IN_PROGRESS, "Action impossible.")
    task.status = TaskStatus.IN_REVIEW
    db.flush()
    log_event(db, task, "in_review", f"{intern.name} a soumis « {task.title} » pour relecture", intern)
    notify(db, task.assignee, "review", f"{intern.name} a terminé « {task.title} » : à relire.", task)


def approve_review(db: Session, task: Task, user: User) -> None:
    _require_assignee(task, user, TaskStatus.IN_REVIEW)
    _complete(db, task, user, f"« {task.title} » validée par {user.name}")


def request_changes(db: Session, task: Task, user: User, comment: str) -> None:
    _require_assignee(task, user, TaskStatus.IN_REVIEW)
    task.status = TaskStatus.IN_PROGRESS
    db.flush()
    log_event(db, task, "changes_requested", f"{user.name} demande des corrections : {comment or '—'}", user)
    notify(db, task.delegate, "changes_requested", f"Corrections demandées sur « {task.title} » : {comment}", task)


def complete(db: Session, task: Task, user: User) -> None:
    _require(task.delegate_id is None, "Tâche déléguée : elle doit passer par la relecture.")
    _require_assignee(task, user, TaskStatus.IN_PROGRESS)
    _complete(db, task, user, f"« {task.title} » terminée par {user.name}")


def _complete(db: Session, task: Task, user: User, message: str) -> None:
    task.status = TaskStatus.DONE
    task.completed_at = clock.now(db)
    db.flush()
    log_event(db, task, "done", message, user)
