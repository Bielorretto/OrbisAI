"""Parcours complet à travers l'interface HTTP, avec les vrais profils et dossiers."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Delegation, Proposal, ProposalStatus, Task, User
from tests.conftest import dossier

IEF, NOVAPAY = 32, 1


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def _login(client, name, role="collaborateur"):
    client.cookies.clear()
    response = client.post("/login", data={"role": role, "name": name}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/?as="), f"refusée : {name}"
    return response.headers["location"].split("as=")[1].split("&")[0]


def _id(db, name):
    return db.scalars(select(User.id).where(User.name == name)).one()


def test_pages_require_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_full_path(client, db):
    m = dossier(db, IEF)
    _login(client, "exemple", role="associe")                    # Jean Moreau
    page = client.get("/").text
    assert "Energie Stratégique" in page
    assign = client.get(f"/matters/{m.id}/assign").text
    assert "Léa Garnier" in assign and "classement calculé localement" in assign.lower()

    from app.services.scoring import rank_candidates
    others = [c.user.id for c in rank_candidates(db, m, use_ai=False)
              if not c.eliminated and c.user.name != "Léa Garnier"][:2]
    client.post(f"/matters/{m.id}/assign", data={"user_ids": [_id(db, "Léa Garnier"), *others]})
    db.expire_all()
    first = db.scalars(select(Proposal).where(Proposal.matter_id == m.id, Proposal.rank == 1)).one()
    assert first.status == ProposalStatus.PENDING

    _login(client, "exemple")                                   # Léa Garnier
    client.post(f"/proposals/{first.id}/accept")
    db.expire_all()
    assert "Léa Garnier" in [u.name for u in dossier(db, IEF).lawyers]

    client.post(f"/matters/{m.id}/tasks", data={"title": "Analyse de l'éligibilité au contrôle IEF",
                                                "deadline": "2099-01-01T18:00", "estimated_hours": 6,
                                                "next_step": "myself"})
    db.expire_all()
    task = db.scalars(select(Task).where(Task.matter_id == m.id)).one()
    assert task.status == "in_progress"
    client.post("/time-entries", data={"task_id": task.id, "hours": 2, "note": "analyse du secteur"})
    client.post(f"/tasks/{task.id}/complete")

    response = client.post(f"/matters/{m.id}/tasks", data={"title": "Préparer la data room", "estimated_hours": 3,
                                                           "deadline": "2099-01-01T18:00", "next_step": "delegate"},
                           follow_redirects=False)
    assert "/delegate" in response.headers["location"]
    assert client.get(response.headers["location"]).status_code == 200


def test_assigned_dossier_is_not_listed_as_to_assign(client, db):
    _login(client, "Pierre Richard", role="associe")
    page = client.get("/").text
    to_assign = page.split("En cours")[0]
    assert "NovaPay" not in to_assign and "NovaPay" in page      # dossier partagé avec Jean Moreau


def test_dossier_shared_by_several_partners(client, db):
    for name in ("Jean Moreau", "Pierre Richard"):
        _login(client, name, role="associe")
        assert "NovaPay" in client.get("/").text


def test_lawyer_outside_team_cannot_open_dossier(client, db):
    _login(client, "Maxime Laurent")
    response = client.get(f"/matters/{dossier(db, NOVAPAY).id}", follow_redirects=False)
    assert response.status_code == 303


def test_intern_page(client, db):
    _login(client, "exemple", role="stagiaire")                  # Hugo Lambert
    page = client.get("/").text
    assert "Préparer la liste de questions pour la data room" in page   # à accepter
    assert "Due diligence réglementaire" in page                        # en cours
    delegation = db.scalars(select(Delegation).where(Delegation.status == "pending",
                                                     Delegation.to_user_id == _id(db, "Hugo Lambert"))).one()
    client.post(f"/delegations/{delegation.id}/accept")
    client.post(f"/tasks/{delegation.task_id}/submit-review")
    db.expire_all()
    assert db.get(Task, delegation.task_id).status == "in_review"


@pytest.mark.parametrize("role, name, expected", [
    ("associe", "exemple", ("Jean Moreau", "associe")),
    ("collaborateur", "exemple", ("Léa Garnier", "collab_senior")),
    ("stagiaire", "exemple", ("Hugo Lambert", "stagiaire")),
    ("collaborateur", "tom laurent", ("Tom Laurent", "junior")),
    ("associe", "Sarah Cohen", ("Sarah Cohen", "associe")),
    ("collaborateur", "clara dupont", None),
    ("collaborateur", "inconnu", None),
])
def test_login_by_name(db, role, name, expected):
    from app.routes.pages import find_user_by_name
    user = find_user_by_name(db, role, name)
    assert (user.name, user.role) == expected if expected else user is None


def test_assistant_endpoint(client, db):
    m = dossier(db, IEF)
    _login(client, "exemple", role="associe")
    data = client.post(f"/matters/{m.id}/assistant",
                       json={"message": "Pourquoi Léa plutôt qu'Emma ?", "history": []}).json()
    assert data["source"] == "simulé" and "Léa Garnier" in data["answer"] and "Emma Rolland" in data["answer"]


def test_matter_creation(client, db):
    _login(client, "Thomas Bernard", role="associe")
    analysis = client.post("/matters/analyze", data={"title": "Renouvellement de baux commerciaux",
                                                     "description": "bail, loyer, bailleur"}).json()
    assert analysis["specialty"] == "Droit immobilier"
    response = client.post("/matters", data={"name": "Baux de la Boutique Lune", "client_id": dossier(db, 10).client_id,
                                             "deadline": "2099-01-01T18:00", "estimated_hours": 8,
                                             "specialty_id": analysis["specialty_id"]}, follow_redirects=False)
    assert response.headers["location"].startswith("/matters/")


# --------------------------------------------------------------------------- un onglet = une identité

def test_each_tab_keeps_its_own_identity(client, db):
    associe = _login(client, "Jean Moreau", role="associe")
    stagiaire = _login(client, "Hugo Lambert", role="stagiaire")   # le cookie est maintenant celui du stagiaire
    # …mais l'onglet de l'associé, qui transporte son jeton, reste l'associé
    page = client.get(f"/?as={associe}").text
    assert "Jean Moreau" in page and "Nouveau dossier" in page
    page = client.get("/", headers={"X-As": stagiaire}).text
    assert "Hugo Lambert" in page and "Nouveau dossier" not in page


def test_redirects_keep_the_tab_identity(client, db):
    associe = _login(client, "Jean Moreau", role="associe")
    _login(client, "Léa Garnier")                                   # autre onglet, autre personne
    response = client.post(f"/notifications/read-all?as={associe}", follow_redirects=False)
    assert f"as={associe}" in response.headers["location"]


def test_forged_identity_is_rejected(client, db):
    _login(client, "Léa Garnier")
    response = client.get(f"/?as={_id(db, 'Jean Moreau')}.0000000000000000", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_flash_message_travels_with_the_page(client, db):
    client.cookies.clear()
    page = client.post("/login", data={"role": "associe", "name": "Jean Moreau"})  # suit la redirection
    assert "Connecté en tant que Jean Moreau" in page.text                         # message dans l'adresse
    assert "flash" not in client.cookies                                           # aucun cookie partagé
