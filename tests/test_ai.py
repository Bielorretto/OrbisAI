"""Le module IA : validation des sorties Mistral et bascule en mode simulé. Aucun appel réseau réel."""
import json

import httpx
import pytest

from app import config
from app.services import ai

SPECIALTIES = {
    "Droit social": ["Contentieux prud'homal", "Rupture du contrat de travail"],
    "Droit immobilier": ["Baux commerciaux", "Construction"],
    "Contentieux commercial": ["Rupture brutale et distribution"],
}
TYPES = {"conclusions": "Procédure / conclusions", "audit": "Audit / due diligence"}


@pytest.fixture(autouse=True)
def fresh_state():
    ai.reset_state()
    yield
    ai.reset_state()


def _fake_post(payload_content):
    def fake(url, headers, json, timeout):  # noqa: A002 - signature de httpx.post
        request = httpx.Request("POST", url)
        if url.endswith("/embeddings"):
            body = {"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(json["input"])]}
        else:
            body = {"choices": [{"message": {"content": payload_content}}]}
        return httpx.Response(200, json=body, request=request)
    return fake


def test_mock_analysis_detects_specialty():
    result = ai.analyze_task("Licenciement d'un salarié", "Préparer le dossier prud'hommes", SPECIALTIES, TYPES)
    assert result["specialty"] == "Droit social" and result["source"] == "simulé"
    assert result["sub_specialty"] == "Contentieux prud'homal"


def test_validation_clamps_and_rejects_unknown_values():
    result = ai._validate_analysis({"specialty": "Droit spatial", "complexity": 9, "estimated_hours": 5000,
                                    "keywords": ["a", 1, None], "task_type": "piratage"}, SPECIALTIES, TYPES)
    assert result["specialty"] is None and result["complexity"] == 3 and result["estimated_hours"] == 200
    assert result["task_type"] is None
    # Une sous-spécialité qui n'appartient pas au domaine est rejetée
    result = ai._validate_analysis({"specialty": "Droit social", "sub_specialty": "Baux commerciaux"},
                                   SPECIALTIES, TYPES)
    assert result["specialty"] == "Droit social" and result["sub_specialty"] is None


def test_live_call_is_parsed(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "auto")
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "test")
    content = json.dumps({"specialty": "Droit immobilier", "sub_specialty": "Baux commerciaux", "task_type": "audit",
                          "complexity": 2, "estimated_hours": 7.3, "keywords": ["bail"], "summary": "Revue de baux."})
    monkeypatch.setattr(httpx, "post", _fake_post(content))
    result = ai.analyze_task("Audit de baux", "", SPECIALTIES, TYPES)
    assert result == {"specialty": "Droit immobilier", "sub_specialty": "Baux commerciaux", "task_type": "audit",
                      "complexity": 2, "estimated_hours": 7.5, "keywords": ["bail"], "summary": "Revue de baux.",
                      "source": "mistral"}
    model, vectors = ai.embed(["a", "b"])
    assert model == config.MISTRAL_EMBED_MODEL and len(vectors) == 2


def test_auto_mode_falls_back_to_mock_on_api_error(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "auto")
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "fausse-cle")

    def unauthorized(url, headers, json, timeout):  # noqa: A002
        request = httpx.Request("POST", url)
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(httpx, "post", unauthorized)
    label = ai.billing_label("Audit de baux", "", "revue des clauses")
    assert "revue des clauses" in label.lower()
    status = ai.status()
    assert status["last_error"] == "Clé API Mistral refusée (401)" and status["using_mistral"] is False


def test_live_mode_raises_instead_of_simulating(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "live")
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "")
    with pytest.raises(ai.AIError):
        ai.billing_label("x", "", "y")


def test_invalid_json_from_model_falls_back(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "auto")
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(httpx, "post", _fake_post("pas du json"))
    result = ai.analyze_task("Licenciement d'un salarié", "", SPECIALTIES, TYPES)
    assert result["source"] == "simulé"


def test_chat_uses_mistral_then_falls_back(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "auto")
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(httpx, "post", _fake_post("Sarah est la meilleure."))
    assert ai.chat("système", [{"role": "user", "content": "?"}], lambda: "local") == ("Sarah est la meilleure.", "mistral")

    # Avec une clé, une erreur de Mistral est remontée : pas de fausse réponse simulée
    monkeypatch.setattr(httpx, "post", _fake_post(""))
    with pytest.raises(ai.AIError):
        ai.chat("système", [{"role": "user", "content": "?"}], lambda: "local")

    # Sans clé (ou en mode mock) : réponse simulée
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "")
    assert ai.chat("système", [{"role": "user", "content": "?"}], lambda: "local") == ("local", "simulé")


def test_chat_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "auto")
    monkeypatch.setattr(config, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(ai.time, "sleep", lambda _: None)
    calls = []

    def flaky(url, headers, json, timeout):  # noqa: A002
        calls.append(1)
        request = httpx.Request("POST", url)
        if len(calls) < 3:
            raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request)

    monkeypatch.setattr(httpx, "post", flaky)
    assert ai.chat("s", [{"role": "user", "content": "?"}], lambda: "local") == ("ok", "mistral")
    assert len(calls) == 3
