from datetime import timedelta

import pytest

from app import clock, config
from app.models import MatterStatus, ProposalStatus, TaskStatus
from app.services import scheduler, workflow
from app.services.scoring import eligible_ids, rank_candidates
from app.services.workflow import WorkflowError
from tests.conftest import dossier

BAUX, NOVAPAY, IEF, APPRO = 10, 1, 32, 2


def _start(db, people, m, names, partner=None):
    workflow.start_proposals(db, m, partner or m.created_by, [people[n].id for n in names])
    return [p for p in m.proposals if p.round == workflow.current_round(m)]


def _new_task(db, m, user, **kwargs):
    base = dict(title="Préparer le bordereau de pièces", description="", deadline=clock.now(db) + timedelta(days=3),
                estimated_hours=3)
    return workflow.create_task(db, user, m, **(base | kwargs))


# --------------------------------------------------------------------------- équipes et cascade

def test_refusal_passes_to_next_and_acceptance_adds_to_team(db, people):
    m = dossier(db, BAUX)
    p1, p2, p3 = _start(db, people, m, ["Chloé Faure", "Inès Roux", "Lucas Henry"])
    assert m.status == MatterStatus.PROPOSING
    workflow.refuse_proposal(db, p1.id, people["Chloé Faure"], "overloaded")
    assert p2.status == ProposalStatus.PENDING
    workflow.accept_proposal(db, p2.id, people["Inès Roux"])
    assert m.status == MatterStatus.ACTIVE and people["Inès Roux"] in m.lawyers
    assert p3.status == ProposalStatus.CANCELLED


def test_three_refusals_then_reselection(db, people):
    m = dossier(db, BAUX)
    names = ["Chloé Faure", "Inès Roux", "Lucas Henry"]
    for proposal, name in zip(_start(db, people, m, names), names):
        workflow.refuse_proposal(db, proposal.id, people[name], "overloaded")
    assert m.status == MatterStatus.CASCADE_FAILED
    second = _start(db, people, m, ["Lucas Henry", "Chloé Faure", "Inès Roux"])
    assert {p.round for p in second} == {2} and second[0].status == ProposalStatus.PENDING


def test_no_answer_expires_and_moves_to_next(db, people):
    m = dossier(db, BAUX)
    p1, p2, _ = _start(db, people, m, ["Chloé Faure", "Inès Roux", "Lucas Henry"])
    clock.advance(db, timedelta(hours=config.RESPONSE_DELAY_HOURS, minutes=1))
    assert scheduler.tick(db)["expired"] >= 1
    assert p1.status == ProposalStatus.EXPIRED and p2.status == ProposalStatus.PENDING


def test_selection_rules(db, people):
    m = dossier(db, BAUX)
    with pytest.raises(WorkflowError, match="au moins"):
        workflow.start_proposals(db, m, m.created_by, [people["Chloé Faure"].id])
    with pytest.raises(WorkflowError, match="collaborateurs"):
        workflow.start_proposals(db, m, m.created_by, [people[n].id for n in ("Chloé Faure", "Inès Roux", "Clara Dupont")])
    with pytest.raises(WorkflowError, match="associés du dossier"):
        _start(db, people, m, ["Chloé Faure", "Inès Roux", "Lucas Henry"], partner=people["Marie Lefèvre"])


def test_several_partners_share_a_dossier(db, people):
    m = dossier(db, NOVAPAY)
    assert {u.name for u in m.partners} == {"Jean Moreau", "Pierre Richard"}
    assert workflow.is_partner_of(m, people["Pierre Richard"]) and workflow.is_partner_of(m, people["Jean Moreau"])


def test_active_dossier_can_get_another_lawyer(db, people):
    m = dossier(db, NOVAPAY)  # Léa Garnier y travaille déjà
    eligible = [uid for uid, reason in eligible_ids(db, m).items() if reason is None]
    assert people["Léa Garnier"].id not in eligible  # déjà dans l'équipe : n'est pas candidate
    team_before = len(m.lawyers)
    workflow.start_proposals(db, m, people["Pierre Richard"], eligible)
    assert m.status == MatterStatus.ACTIVE  # le dossier reste en cours pendant la proposition
    workflow.accept_proposal(db, workflow.current_proposal(m).id, db.get(type(people["Léa Garnier"]), eligible[0]))
    assert len(m.lawyers) == team_before + 1


def test_member_cannot_be_proposed_again(db, people):
    with pytest.raises(WorkflowError):
        _start(db, people, dossier(db, NOVAPAY), ["Léa Garnier"])


def test_conflict_of_interest_is_excluded(db, people):
    victor = next(c for c in rank_candidates(db, dossier(db, IEF), use_ai=False) if c.user.name == "Victor Rey")
    assert victor.eliminations[0][0] == "conflict"


def test_minimum_choices_lowered_only_when_few_eligible():
    assert [workflow.min_choices(n) for n in (0, 1, 2, 3, 8)] == [1, 1, 2, 3, 3]


def test_partner_notified_at_j_minus_x(db):
    m = dossier(db, BAUX)
    assert m.notified_at is None
    clock.advance(db, timedelta(days=(m.deadline - clock.now(db)).days - m.notify_days_before + 1))
    assert scheduler.tick(db)["to_assign"] >= 1 and m.notified_at is not None


def test_close_matter_requires_tasks_to_be_finished(db, people):
    m = dossier(db, APPRO)
    maxime = people["Maxime Laurent"]
    with pytest.raises(WorkflowError, match="encore ouverte"):
        workflow.close_matter(db, m, maxime)
    for t in m.tasks:
        if t.status == TaskStatus.IN_PROGRESS:
            workflow.complete(db, t, maxime)
    workflow.close_matter(db, m, maxime)
    assert m.status == MatterStatus.CLOSED


# --------------------------------------------------------------------------- tâches

def test_team_lawyer_creates_task_and_intern_joins_team(db, people):
    m = dossier(db, NOVAPAY)
    lea = people["Léa Garnier"]
    task = _new_task(db, m, lea, specialty_id=m.specialty_id, sub_specialty_id=None)
    assert task.status == TaskStatus.ACCEPTED and task.assignee_id == lea.id
    d1 = workflow.delegate(db, task, lea, people["Mehdi Cohen"].id)
    workflow.refuse_delegation(db, d1.id, people["Mehdi Cohen"], "overloaded")
    assert task.status == TaskStatus.ACCEPTED
    d2 = workflow.delegate(db, task, lea, people["Eva Martin"].id)
    workflow.accept_delegation(db, d2.id, people["Eva Martin"])
    assert people["Eva Martin"] in m.interns  # le stagiaire rejoint l'équipe du dossier
    workflow.submit_for_review(db, task, people["Eva Martin"])
    workflow.approve_review(db, task, lea)
    assert task.status == TaskStatus.DONE


def test_only_team_lawyers_create_tasks(db, people):
    with pytest.raises(WorkflowError, match="collaborateurs de l'équipe"):
        _new_task(db, dossier(db, NOVAPAY), people["Emma Rolland"])
    with pytest.raises(WorkflowError, match="dossier en cours"):
        _new_task(db, dossier(db, BAUX), people["Chloé Faure"])


def test_cannot_delegate_to_a_lawyer(db, people):
    m = dossier(db, NOVAPAY)
    task = _new_task(db, m, people["Léa Garnier"])
    with pytest.raises(WorkflowError, match="stagiaire"):
        workflow.delegate(db, task, people["Léa Garnier"], people["Emma Rolland"].id)
