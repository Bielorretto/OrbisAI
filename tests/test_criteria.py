"""Critères éliminatoires (règles fixes), classement par Mistral (mémorisé) et assistant."""
from datetime import timedelta

import pytest

from app import clock, config
from app.models import CalendarEvent, Role, TeamRole
from app.services import ai, assistant, scoring, workflow
from app.services.scoring import language_level, rank_candidates
from app.services.workflow import WorkflowError
from tests.conftest import dossier, task_by_title

BAUX, NOVAPAY, IEF, BREVET, AUDIT_CONTRATS, CORPORATE_MA = 10, 1, 32, 13, 28, 25


def _rank(db, item):
    return {c.user.name: c for c in rank_candidates(db, item, use_ai=False)}


def _new_matter(db, people, **kwargs):
    m = dossier(db, BAUX)
    base = dict(name="Dossier de test", description="", client_id=m.client_id, deadline=clock.now(db) + timedelta(days=12),
                estimated_hours=8, specialty_id=m.specialty_id, sub_specialty_id=m.sub_specialty_id)
    return workflow.create_matter(db, people["Thomas Bernard"], **(base | kwargs))


# --------------------------------------------------------------------------- éliminatoires

def test_domain_is_eliminatory(db):
    assert _rank(db, dossier(db, BREVET))["Chloé Faure"].eliminations[0][0] == "domain"


def test_language_levels_parsed_from_profiles(db, people):
    assert language_level(people["Jean Moreau"], "Espagnol") == 4      # B2
    assert language_level(people["Léa Garnier"], "anglais") == 6        # C2
    assert language_level(people["Paul Marchand"], "Anglais") == 0


def test_required_language_needs_c1(db, people):
    ranking = _rank(db, _new_matter(db, people, required_language="Anglais"))
    assert any(k == "language" for k, _ in ranking["Chloé Faure"].eliminations)   # Anglais B2
    assert not ranking["Lucas Henry"].eliminated or "language" not in ranking["Lucas Henry"].eliminated


def test_minimum_hierarchy_excludes_juniors(db, people):
    ranking = _rank(db, _new_matter(db, people, min_level=Role.ASSOCIATE))
    assert ranking["Lucas Henry"].eliminations[0][0] == "hierarchy"   # junior
    assert not ranking["Chloé Faure"].eliminated


def test_insufficient_availability_is_eliminatory(db, people):
    now = clock.now(db)
    db.add(CalendarEvent(user_id=people["Hugo Blanc"].id, title="Congés", kind="conge", start=now,
                         end=now + timedelta(days=15)))
    db.flush()
    assert _rank(db, dossier(db, BREVET))["Hugo Blanc"].eliminations[0][0] == "availability"


def test_eliminations_follow_document_order(db, people):
    m = _new_matter(db, people, client_id=dossier(db, IEF).client_id, required_language="Japonais",
                    specialty_id=dossier(db, IEF).specialty_id, sub_specialty_id=dossier(db, IEF).sub_specialty_id)
    keys = [k for k, _ in _rank(db, m)["Victor Rey"].eliminations]
    assert keys[0] == "conflict" and keys[-1] == "language"


def test_team_members_are_not_candidates(db):
    assert "Léa Garnier" not in _rank(db, dossier(db, NOVAPAY))


def test_workload_is_shared_within_the_team(db, people):
    m = dossier(db, AUDIT_CONTRATS)  # équipe de plusieurs collaborateurs
    share = dict((x.id, h) for x, h in scoring.workload(db, people["Inès Roux"]))[m.id]
    assert len(m.lawyers) > 1
    assert share == pytest.approx(scoring.remaining_hours(db, m) / len(m.lawyers))


def test_server_rejects_an_excluded_choice(db, people):
    m = dossier(db, BREVET)
    with pytest.raises(WorkflowError, match="Domaine du droit"):
        workflow.start_proposals(db, m, m.created_by, [people["Hugo Blanc"].id, people["Chloé Faure"].id])


def test_criteria_are_in_document_order(db):
    best = rank_candidates(db, dossier(db, BAUX), use_ai=False)[0]
    assert [c.key for c in best.criteria if c.tier == "main"][:3] == ["domain_level", "sub_level", "similar_cases"]
    assert len(best.criteria) == 18 and best.estimated_finish


# --------------------------------------------------------------------------- classement par Mistral

def _fake_ai(calls, reverse=True):
    def fake(context, eligible):
        calls.append(context)
        order = list(reversed(eligible)) if reverse else list(eligible)
        return ai._validate_ranking({"classement": [{"id": uid, "score": 90 - 10 * i, "raison": f"raison {uid}"}
                                                     for i, uid in enumerate(order)]}, eligible)
    return fake


def test_mistral_ranks_with_full_information_and_result_is_cached(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "rank_candidates", _fake_ai(calls))
    m = dossier(db, BAUX)
    local = [c.user.id for c in rank_candidates(db, m, use_ai=False) if not c.eliminated]
    ranking = rank_candidates(db, m)
    assert ranking.source == "mistral"
    assert [c.user.id for c in ranking if not c.eliminated] == list(reversed(local))
    assert ranking[0].explanation.startswith("raison") and ranking[0].score == 90
    profile = calls[0]["candidats_eligibles"][0]
    assert {"langues", "domaines", "sous_specialites", "charge_en_cours", "heures_libres_avant_echeance",
            "preferences", "experience"} <= set(profile)
    assert "hierarchie_des_criteres" in calls[0] and "description" in calls[0]["a_pourvoir"]

    rank_candidates(db, m)                      # informations inchangées : classement mémorisé
    assert len(calls) == 1
    rank_candidates(db, m, refresh=True)        # « Recalculer »
    assert len(calls) == 2


def test_ranking_is_recomputed_when_information_changes(db, people, monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "rank_candidates", _fake_ai(calls))
    m = dossier(db, BAUX)
    rank_candidates(db, m)
    workflow.add_member(db, m, people["Inès Roux"], TeamRole.LAWYER)  # l'équipe change
    rank_candidates(db, m)
    assert len(calls) == 2


def test_local_ranking_when_mistral_unavailable(db):
    ranking = rank_candidates(db, dossier(db, BAUX))  # mode simulé : pas d'appel
    assert ranking.source == "local" and ranking[0].explanation


def test_ai_ranking_validation():
    result = ai._validate_ranking({"classement": [{"id": 3, "score": 40, "raison": "x"}, {"id": 99, "score": 99},
                                                   {"id": 3, "score": 10}, {"id": 5, "score": 70}]}, [3, 5])
    assert result["order"] == [3, 5]
    assert result["scores"] == {"3": 40, "5": 40}  # inconnus et doublons ignorés, scores décroissants
    with pytest.raises(ValueError):
        ai._validate_ranking({"classement": []}, [3])


# --------------------------------------------------------------------------- assistant

def _ask(db, item, question, pool=Role.ASSOCIATE):
    candidates = rank_candidates(db, item, use_ai=False)
    return assistant.local_answer(question, item, candidates, pool)


def test_assistant_answers(db):
    m = dossier(db, BAUX)
    assert _ask(db, m, "Qui est le meilleur choix ?").startswith("Le meilleur choix est")
    assert "éliminatoires" in _ask(db, m, "Comment sont hiérarchisés les critères ?")
    text = _ask(db, m, "Pourquoi Inès plutôt que Chloé ?")
    assert "Inès Roux" in text and "Chloé Faure" in text


def test_assistant_access(db, people):
    m = dossier(db, NOVAPAY)
    task = task_by_title(db, "Négociation du SPA")
    assert assistant.pool_for(people["Jean Moreau"], m) == Role.ASSOCIATE
    assert assistant.pool_for(people["Léa Garnier"], m) is None
    assert assistant.pool_for(people["Léa Garnier"], task) == Role.INTERN
    assert assistant.pool_for(people["Emma Rolland"], task) is None
    with pytest.raises(assistant.AssistantError):
        assistant.answer(db, people["Emma Rolland"], task, "Qui ?", [])


def test_factual_guard(db):
    candidates = rank_candidates(db, dossier(db, BAUX), use_ai=False)
    eligible = [c for c in candidates if not c.eliminated]
    name = eligible[0].user.name
    assert assistant.factual_errors(f"{name} est exclu car il ne parle pas anglais.", candidates)
    assert not assistant.factual_errors(f"{name} n'est pas exclu.", candidates)


def test_config_threshold_used_for_dossiers():
    assert config.MATTER_MIN_FREE_HOURS == 10


def test_every_dossier_has_at_least_five_eligible_lawyers(db):
    from sqlalchemy import select
    from app.demo_data import MIN_ELIGIBLE
    from app.models import Matter
    for m in db.scalars(select(Matter)):
        eligible = [c for c in rank_candidates(db, m, use_ai=False) if not c.eliminated]
        assert len(eligible) >= MIN_ELIGIBLE, f"{m.name} : {len(eligible)} éligibles"


def test_every_profile_works_on_a_dossier(db, people):
    from app.models import MatterMember
    from sqlalchemy import select
    staffed = {m.user_id for m in db.scalars(select(MatterMember))}
    assert {u.id for u in people.values()} <= staffed
