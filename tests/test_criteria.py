"""Critères d'attribution : éliminatoires dans l'ordre du document, puis hiérarchie des critères."""
from datetime import timedelta

import pytest

from app import clock
from app.models import Role
from app.services import assistant, workflow
from app.services.scoring import rank_candidates
from app.services.workflow import WorkflowError
from tests.conftest import matter, task_by_title


def _rank(db, item, role=Role.ASSOCIATE):
    return {c.user.name: c for c in rank_candidates(db, item, role, clock.now(db), explain=False)}


def _new_matter(db, people, **kwargs):
    base = dict(name="Dossier de test", description="", client_id=matter(db, "2026-014").client_id,
                deadline=clock.now(db) + timedelta(days=10), estimated_hours=4)
    return workflow.create_matter(db, people["Antoine Ferrand"], **(base | kwargs))


def test_domain_and_sub_specialty_are_eliminatory(db):
    ranking = _rank(db, matter(db, "2026-014"))
    assert ranking["Julien Moreau"].eliminations[0][0] == "domain"
    assert not ranking["Sarah Benali"].eliminated


def test_required_language_is_eliminatory(db):
    ranking = _rank(db, matter(db, "2026-040"))  # italien obligatoire
    assert ranking["Thomas Leroy"].eliminations[0][0] == "language"
    assert not ranking["Inès Garnier"].eliminated


def test_minimum_hierarchy_level_excludes_interns(db):
    m = matter(db, "2026-047")
    task = workflow.create_task(db, m.assignee, m, title="Plaider le référé", description="",
                                deadline=clock.now(db) + timedelta(days=4), estimated_hours=3,
                                min_level=Role.ASSOCIATE)
    ranking = _rank(db, task, Role.INTERN)
    assert all(any(key == "hierarchy" for key, _ in c.eliminations) for c in ranking.values())


def test_insufficient_availability_is_eliminatory(db):
    ranking = _rank(db, matter(db, "2026-014"))  # Hugo est en congé
    assert ranking["Hugo Lambert"].eliminations[0][0] == "availability"


def test_eliminations_follow_document_order(db, people):
    m = _new_matter(db, people, client_id=matter(db, "2026-051").client_id, required_language="Japonais")
    assert [key for key, _ in _rank(db, m)["Sarah Benali"].eliminations] == ["conflict", "language"]


def test_required_jurisdiction(db, people):
    ranking = _rank(db, _new_matter(db, people, required_jurisdiction="Nanterre"))
    assert not ranking["Sarah Benali"].eliminated          # Paris, Versailles, Nanterre
    assert ranking["Julien Moreau"].eliminations[0][0] == "jurisdiction"


def test_criteria_are_in_document_order(db):
    best = rank_candidates(db, matter(db, "2026-014"), Role.ASSOCIATE, clock.now(db), explain=False)[0]
    main = [c.key for c in best.criteria if c.tier == "main"]
    assert main[:3] == ["domain_level", "sub_level", "similar_cases"] and len(main) == 11
    assert len([c for c in best.criteria if c.tier == "complementary"]) == 7
    assert best.user.name == "Sarah Benali" and best.estimated_finish and best.estimated_cost == 14 * 380


def test_workload_counts_active_matters(db, people):
    # Julien est responsable d'un dossier en cours : sa charge réduit ses heures libres
    julien = _rank(db, matter(db, "2026-032"))["Julien Moreau"]
    assert julien.active_tasks == 1


def test_server_rejects_an_excluded_choice(db, people):
    m = matter(db, "2026-014")
    with pytest.raises(WorkflowError, match="Domaine du droit"):
        workflow.start_proposals(db, m, m.created_by,
                                 [people[n].id for n in ("Sarah Benali", "Camille Bernard", "Julien Moreau")])


def test_minimum_choices_lowered_when_few_eligible(db, people):
    m = matter(db, "2026-040")  # seule Inès est éligible
    workflow.start_proposals(db, m, m.created_by, [people["Inès Garnier"].id])
    assert m.status == "proposing"


# --------------------------------------------------------------------------- assistant (réponses sans Mistral)

def _ask(db, item, question, pool=Role.ASSOCIATE):
    candidates = rank_candidates(db, item, pool, clock.now(db), explain=False)
    return assistant.local_answer(question, item, candidates, pool)


def test_assistant_names_the_best_candidate(db):
    assert _ask(db, matter(db, "2026-014"), "Qui est le meilleur choix ?").startswith(
        "Le meilleur choix est Sarah Benali")


def test_assistant_compares_with_the_deciding_criterion_first(db):
    text = _ask(db, matter(db, "2026-014"), "Pourquoi Camille plutôt que Sarah ?")
    assert "Sarah Benali" in text.splitlines()[0]  # la mieux classée est citée en premier
    assert "Niveau d'expertise dans le domaine" in text.splitlines()[2]


def test_assistant_explains_exclusion(db):
    text = _ask(db, matter(db, "2026-014"), "Pourquoi Hugo est exclu ?")
    assert "Hugo Lambert" in text and "Disponibilité minimale requise" in text


def test_assistant_explains_method(db):
    text = _ask(db, matter(db, "2026-014"), "Comment sont hiérarchisés les critères ?")
    assert "éliminatoires" in text and "conflit d'intérêts" in text


def test_assistant_access(db, people):
    m = matter(db, "2026-014")
    task = task_by_title(db, "Rédiger les conclusions en défense")
    assert assistant.pool_for(m.created_by, m) == Role.ASSOCIATE          # associé -> collaborateurs (dossier)
    assert assistant.pool_for(people["Julien Moreau"], m) is None
    assert assistant.pool_for(people["Julien Moreau"], task) == Role.INTERN  # responsable -> stagiaires (tâche)
    assert assistant.pool_for(people["Sarah Benali"], task) is None
    with pytest.raises(assistant.AssistantError):
        assistant.answer(db, people["Sarah Benali"], task, "Qui ?", [])


def test_assistant_for_collaborator_explains_interns(db, people):
    task = task_by_title(db, "Rédiger les conclusions en défense")
    result = assistant.answer(db, people["Julien Moreau"], task, "Quel stagiaire est le meilleur ?", [])
    assert result["source"] == "simulé" and "Le meilleur choix est" in result["answer"]


def test_factual_guard_flags_eligible_person_called_excluded(db):
    candidates = rank_candidates(db, matter(db, "2026-014"), Role.ASSOCIATE, clock.now(db), explain=False)
    assert assistant.factual_errors("Maxime Fontaine est exclu car il ne parle pas anglais.", candidates)
    assert not assistant.factual_errors("Maxime n'est pas exclu, mais son score est plus bas.", candidates)
    assert not assistant.factual_errors("Julien Moreau est exclu : domaine non maîtrisé.", candidates)


def test_assistant_asks_mistral_to_correct_a_false_exclusion(db, monkeypatch):
    m = matter(db, "2026-014")
    replies = iter([("Maxime est exclu, c'est éliminatoire.", "mistral"),
                    ("Maxime est éligible (n°4) mais moins bien classé.", "mistral")])
    seen = []
    monkeypatch.setattr(assistant.ai, "chat", lambda system, messages, fallback: (seen.append(messages), next(replies))[1])
    result = assistant.answer(db, m.created_by, m, "Et Maxime ?", [])
    assert result["answer"].startswith("Maxime est éligible") and len(seen) == 2
    assert "n'est PAS exclu" in seen[1][-1]["content"]
