"""Parcours de démo complet à travers l'interface HTTP."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Proposal, ProposalStatus, Task, User
from tests.conftest import matter


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def _login(client, name, role="collaborateur"):
    """Connexion comme un humain : rôle + nom tapé."""
    client.cookies.clear()
    response = client.post("/login", data={"role": role, "name": name}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/", f"connexion refusée : {name}"


def _ids(db, *names):
    return [db.scalars(select(User.id).where(User.name == n)).one() for n in names]


def test_pages_require_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_full_demo_path(client, db):
    m = matter(db, "2026-014")
    _login(client, "exemple", role="associe")
    assert "Nordis – litige fournisseur" in client.get("/").text   # l'associé voit des dossiers
    page = client.get(f"/matters/{m.id}/assign")
    assert page.status_code == 200 and "Sarah Benali" in page.text

    client.post(f"/matters/{m.id}/assign", data={"user_ids": _ids(db, "Sarah Benali", "Camille Bernard",
                                                                   "Maxime Fontaine")})
    db.expire_all()
    first = db.scalars(select(Proposal).where(Proposal.matter_id == m.id, Proposal.rank == 1)).one()
    assert first.status == ProposalStatus.PENDING

    _login(client, "Sarah Benali")
    client.post(f"/proposals/{first.id}/refuse", data={"reason": "overloaded"})
    _login(client, "Camille Bernard")
    assert "Nordis – litige fournisseur" in client.get("/").text
    second = db.scalars(select(Proposal).where(Proposal.matter_id == m.id, Proposal.rank == 2)).one()
    client.post(f"/proposals/{second.id}/accept")

    # Le collaborateur crée une tâche dans le dossier et la fait lui-même
    assert client.get(f"/matters/{m.id}/tasks/new").status_code == 200
    client.post(f"/matters/{m.id}/tasks", data={"title": "Rédiger les conclusions en réponse",
                                                "deadline": "2099-01-01T18:00", "estimated_hours": 6,
                                                "next_step": "myself"})
    db.expire_all()
    task = db.scalars(select(Task).where(Task.matter_id == m.id)).one()
    assert task.status == "in_progress" and task.assignee.name == "Camille Bernard"
    client.post("/time-entries", data={"task_id": task.id, "hours": 2, "note": "plan des conclusions"})
    assert "plan des conclusions" in client.get("/timesheet").text.lower()
    client.post(f"/tasks/{task.id}/complete")

    # Une deuxième tâche, confiée à un stagiaire
    response = client.post(f"/matters/{m.id}/tasks", data={"title": "Analyser les pièces adverses",
                                                           "deadline": "2099-01-01T18:00", "estimated_hours": 3,
                                                           "next_step": "delegate"}, follow_redirects=False)
    assert "/delegate" in response.headers["location"]
    delegate_page = client.get(response.headers["location"])
    assert delegate_page.status_code == 200 and "Emma Petit" in delegate_page.text

    _login(client, "exemple", role="associe")
    assert client.get(f"/matters/{m.id}").status_code == 200
    assert client.get("/billing").status_code == 200


def test_matter_creation_with_ai_analysis(client, db):
    _login(client, "Antoine Ferrand", role="associe")
    analysis = client.post("/matters/analyze", data={"title": "Contester un licenciement",
                                                     "description": "salarié, prud'hommes"}).json()
    assert analysis["specialty"] == "Droit social" and analysis["specialty_id"]
    response = client.post("/matters", data={"name": "Lemaire – licenciement d'un chef d'équipe",
                                             "client_id": matter(db, "2026-032").client_id,
                                             "deadline": "2099-01-01T18:00", "estimated_hours": 8,
                                             "specialty_id": analysis["specialty_id"]}, follow_redirects=False)
    assert response.headers["location"].startswith("/matters/")


def test_collaborator_cannot_open_billing_or_create_matters(client, db):
    _login(client, "Julien Moreau")
    assert client.get("/billing", follow_redirects=False).status_code == 303
    assert client.get("/matters/new", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("role, name, expected", [
    ("associe", "exemple", ("Exemple", "associe")),
    ("collaborateur", "  EXEMPLE ", ("Exemple", "collaborateur")),
    ("collaborateur", "helene marchal", None),        # mauvais rôle
    ("associe", "Hélène Marchal", ("Hélène Marchal", "partner")),    # les partners passent par « Associé »
    ("collaborateur", "benali", ("Sarah Benali", "collaborateur")),  # nom de famille seul, unique
    ("stagiaire", "chloe martin", ("Chloé Martin", "stagiaire")),    # entrée « Stagiaire »
    ("collaborateur", "chloe martin", None),                          # un stagiaire n'est pas collaborateur
    ("stagiaire", "exemple", ("Exemple", "stagiaire")),
    ("collaborateur", "inconnu", None),
])
def test_login_by_name(db, role, name, expected):
    from app.routes.pages import find_user_by_name
    user = find_user_by_name(db, role, name)
    assert (user.name, user.role) == expected if expected else user is None


def test_login_page_shows_no_names(client):
    page = client.get("/login?role=collaborateur").text
    assert 'name="name"' in page and "Sarah Benali" not in page


def test_example_accounts_have_work(client, db):
    _login(client, "exemple", role="associe")
    assert "Nordis – litige fournisseur" in client.get("/").text
    _login(client, "exemple")
    page = client.get("/").text
    assert "Hestia – audit des baux commerciaux" in page          # dossier proposé
    assert "Hestia – recouvrement de loyers impayés" in page      # dossier dont il est responsable


def test_assistant_endpoint(client, db):
    m = matter(db, "2026-014")
    _login(client, "exemple", role="associe")
    response = client.post(f"/matters/{m.id}/assistant",
                           json={"message": "Pourquoi Sarah plutôt que Camille ?", "history": []})
    data = response.json()
    assert response.status_code == 200 and data["source"] == "simulé"
    assert "Sarah Benali" in data["answer"] and "Camille Bernard" in data["answer"]

    _login(client, "Julien Moreau")  # ne voit pas ce dossier
    response = client.post(f"/matters/{m.id}/assistant", json={"message": "Qui ?"}, follow_redirects=False)
    assert response.status_code == 303


def test_assign_page_shows_criteria_and_assistant(client, db):
    _login(client, "exemple", role="associe")
    page = client.get(f"/matters/{matter(db, '2026-014').id}/assign").text
    assert "expertise dans la sous-spécialité" in page
    assert "Domaine du droit non maîtrisé" in page
    assert 'id="assistant-panel"' in page


def test_intern_page_lists_assigned_tasks(client, db):
    _login(client, "exemple", role="stagiaire")
    page = client.get("/").text
    assert "Préparer le bordereau de pièces" in page             # délégation à accepter
    assert "Mettre à jour le registre des mouvements de titres" in page  # tâche en cours
    assert "Mes tâches" in page and "/billing" not in page


def test_intern_accepts_then_finishes_a_task(client, db):
    from app.models import Delegation
    _login(client, "exemple", role="stagiaire")
    delegation = db.scalars(select(Delegation).where(Delegation.status == "pending")).first()
    client.post(f"/delegations/{delegation.id}/accept")
    client.post(f"/tasks/{delegation.task_id}/submit-review")
    db.expire_all()
    assert db.get(Task, delegation.task_id).status == "in_review"
