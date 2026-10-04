from datetime import timedelta

import pytest

from app import clock, config
from app.models import MatterStatus, ProposalStatus, Role, TaskStatus
from app.services import scheduler, workflow
from app.services.scoring import rank_candidates
from app.services.workflow import WorkflowError
from tests.conftest import matter


def _start(db, people, m, names):
    workflow.start_proposals(db, m, m.created_by, [people[n].id for n in names])
    return [p for p in m.proposals if p.round == workflow.current_round(m)]


def _new_task(db, m, user, **kwargs):
    base = dict(title="Préparer le bordereau de pièces", description="", deadline=clock.now(db) + timedelta(days=3),
                estimated_hours=3)
    return workflow.create_task(db, user, m, **(base | kwargs))


# --------------------------------------------------------------------------- dossier : cascade

def test_refusal_passes_to_next_then_acceptance_makes_collaborator_responsible(db, people):
    m = matter(db, "2026-014")
    p1, p2, p3 = _start(db, people, m, ["Sarah Benali", "Camille Bernard", "Maxime Fontaine"])
    assert m.status == MatterStatus.PROPOSING
    assert (p1.status, p2.status, p3.status) == (ProposalStatus.PENDING, ProposalStatus.QUEUED, ProposalStatus.QUEUED)

    workflow.refuse_proposal(db, p1.id, people["Sarah Benali"], "overloaded")
    assert p1.status == ProposalStatus.REFUSED and p2.status == ProposalStatus.PENDING

    workflow.accept_proposal(db, p2.id, people["Camille Bernard"])
    assert m.status == MatterStatus.ACTIVE and m.assignee_id == people["Camille Bernard"].id
    assert p3.status == ProposalStatus.CANCELLED
    assert m.budget_amount == m.estimated_hours * people["Camille Bernard"].hourly_rate


def test_three_refusals_return_matter_to_partner_who_can_reselect(db, people):
    m = matter(db, "2026-014")
    names = ["Sarah Benali", "Camille Bernard", "Maxime Fontaine"]
    for proposal, name in zip(_start(db, people, m, names), names):
        workflow.refuse_proposal(db, proposal.id, people[name], "overloaded")
    assert m.status == MatterStatus.CASCADE_FAILED

    second = _start(db, people, m, ["Maxime Fontaine", "Sarah Benali", "Camille Bernard"])
    assert {p.round for p in second} == {2} and second[0].status == ProposalStatus.PENDING


def test_no_answer_expires_and_moves_to_next(db, people):
    m = matter(db, "2026-014")
    p1, p2, _ = _start(db, people, m, ["Sarah Benali", "Camille Bernard", "Maxime Fontaine"])
    clock.advance(db, timedelta(hours=config.RESPONSE_DELAY_HOURS, minutes=1))
    assert scheduler.tick(db)["expired"] >= 1
    assert p1.status == ProposalStatus.EXPIRED and p2.status == ProposalStatus.PENDING


def test_selection_rules(db, people):
    m = matter(db, "2026-014")
    with pytest.raises(WorkflowError, match="au moins"):
        workflow.start_proposals(db, m, m.created_by, [people["Sarah Benali"].id, people["Camille Bernard"].id])
    with pytest.raises(WorkflowError, match="collaborateurs"):
        workflow.start_proposals(db, m, m.created_by, [people[n].id for n in
                                                       ("Sarah Benali", "Camille Bernard", "Chloé Martin")])
    with pytest.raises(WorkflowError, match="associé responsable"):
        workflow.start_proposals(db, m, people["Antoine Ferrand"],
                                 [people[n].id for n in ("Sarah Benali", "Camille Bernard", "Maxime Fontaine")])


def test_conflict_of_interest_is_excluded(db, people):
    celtis = matter(db, "2026-051").client
    m = workflow.create_matter(db, people["Antoine Ferrand"], name="Audition du dirigeant", description="",
                               client_id=celtis.id, deadline=clock.now(db) + timedelta(days=5), estimated_hours=6)
    sarah = next(c for c in rank_candidates(db, m, Role.ASSOCIATE, clock.now(db)) if c.user.name == "Sarah Benali")
    assert sarah.eliminated and "Conflit" in sarah.eliminated
    with pytest.raises(WorkflowError, match="Conflit d'intérêts"):
        workflow.start_proposals(db, m, m.created_by,
                                 [people[n].id for n in ("Sarah Benali", "Maxime Fontaine", "Camille Bernard")])


def test_specialist_ranks_first(db):
    ranking = rank_candidates(db, matter(db, "2026-032"), Role.ASSOCIATE, clock.now(db))
    assert ranking[0].user.name == "Julien Moreau" and ranking[0].explanation


def test_partner_notified_at_j_minus_x(db):
    m = matter(db, "2026-040")
    assert m.notified_at is None
    clock.advance(db, timedelta(days=(m.deadline - clock.now(db)).days - m.notify_days_before + 1))
    assert scheduler.tick(db)["to_assign"] == 1 and m.notified_at is not None


def test_close_matter_requires_tasks_to_be_finished(db, people):
    m = matter(db, "2026-033")  # Julien : 2 tâches en cours
    with pytest.raises(WorkflowError, match="encore ouverte"):
        workflow.close_matter(db, m, people["Julien Moreau"])
    m = matter(db, "2026-047")  # dossier du collaborateur Exemple, sans tâche
    workflow.close_matter(db, m, m.assignee)
    assert m.status == MatterStatus.CLOSED


# --------------------------------------------------------------------------- tâches créées par le collaborateur

def test_collaborator_creates_task_then_delegates_to_intern(db, people):
    m = matter(db, "2026-014")
    p1, *_ = _start(db, people, m, ["Sarah Benali", "Camille Bernard", "Maxime Fontaine"])
    sarah, emma, yanis = people["Sarah Benali"], people["Emma Petit"], people["Yanis Chevalier"]
    workflow.accept_proposal(db, p1.id, sarah)

    task = _new_task(db, m, sarah, title="Analyser les pièces adverses")
    assert task.status == TaskStatus.ACCEPTED and task.assignee_id == sarah.id
    assert task.specialty_id == m.specialty_id and task.sub_specialty_id == m.sub_specialty_id  # repris du dossier

    d1 = workflow.delegate(db, task, sarah, emma.id)
    workflow.refuse_delegation(db, d1.id, emma, "overloaded")
    assert task.status == TaskStatus.ACCEPTED  # revient au collaborateur

    d2 = workflow.delegate(db, task, sarah, yanis.id)
    workflow.accept_delegation(db, d2.id, yanis)
    assert task.status == TaskStatus.IN_PROGRESS and task.delegate_id == yanis.id
    workflow.submit_for_review(db, task, yanis)
    workflow.request_changes(db, task, sarah, "Compléter la partie sur les pénalités")
    workflow.submit_for_review(db, task, yanis)
    workflow.approve_review(db, task, sarah)
    assert task.status == TaskStatus.DONE and task.completed_at


def test_collaborator_does_the_task_himself(db):
    m = matter(db, "2026-047")
    task = _new_task(db, m, m.assignee)
    workflow.work_myself(db, task, m.assignee)
    workflow.complete(db, task, m.assignee)
    assert task.status == TaskStatus.DONE


def test_only_the_responsible_collaborator_creates_tasks(db, people):
    with pytest.raises(WorkflowError, match="responsable du dossier"):
        _new_task(db, matter(db, "2026-033"), people["Sarah Benali"])
    with pytest.raises(WorkflowError, match="dossier en cours"):
        _new_task(db, matter(db, "2026-014"), people["Sarah Benali"])  # pas encore attribué


def test_cannot_delegate_to_a_collaborator(db, people):
    m = matter(db, "2026-047")
    task = _new_task(db, m, m.assignee)
    with pytest.raises(WorkflowError, match="stagiaire"):
        workflow.delegate(db, task, m.assignee, people["Camille Bernard"].id)
