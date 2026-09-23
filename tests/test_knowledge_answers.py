import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import api.knowledge as knowledge_api
from model.factory import ModelError, create_knowledge_model, knowledge_qa_status
from schemas import Profile
from services.records import RecordService
from test_foundation import DAY, application, client, meal, register
from test_knowledge import library, mutate


class AnswerModel:
    def __init__(self):
        self.messages = []
        self.output = None
        self.on_call = None

    def generate(self, *, system_prompt, message):
        assert "JSON Schema" in system_prompt
        self.messages.append(json.loads(message))
        if self.on_call:
            self.on_call()
        if isinstance(self.output, Exception):
            raise self.output
        if self.output is not None:
            return self.output
        ids = [entry["chunk_id"] for entry in self.messages[-1]["evidence"]]
        return json.dumps({"status": "answered", "chunk_ids": ids[:1]})


@pytest.fixture
def qa(client, application, library, monkeypatch):
    model = AnswerModel()
    application.state.knowledge = library
    application.state.settings = replace(application.state.settings, knowledge_qa_enabled=True,
        model_provider="qwen", qwen_api_key="synthetic-test-key")
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda settings: model)
    user = register(client)
    return client, application, model, user


def ask(client, question="力量训练频率"):
    return client.post("/api/knowledge/ask", json={"question": question})


def calls(application):
    with application.state.database.connect() as connection:
        return connection.execute("SELECT COALESCE(SUM(calls),0) FROM ai_usage").fetchone()[0]


def test_answer_bound_to_real_evidence_and_no_business_writes(qa):
    client, app, model, _ = qa
    client.put("/api/profile", json={"display_name": "PrivateProfile", "weight_kg": 72})
    client.post("/api/meals", json=meal(name="PrivateMeal"))
    response = ask(client)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "answered" and data["model_called"] and not data["saved"]
    assert data["answer_mode"] == "reviewed_extracts"
    assert data["sources"][0]["chunk_id"] == "cdc-adults:strength"
    assert data["sources"][0]["url"].startswith("https://www.cdc.gov/")
    assert calls(app) == 1 and len(model.messages) == 1
    outgoing = json.dumps(model.messages)
    assert "PrivateProfile" not in outgoing and "PrivateMeal" not in outgoing and '"weight_kg"' not in outgoing
    assert set(model.messages[0]) == {"question", "evidence"}
    assert len(client.get(f"/api/meals?day={DAY}").json()) == 1
    assert client.get(f"/api/workouts?day={DAY}").json() == []


@pytest.mark.parametrize("question", [
    "我对西兰花过敏，晚餐吃什么", "肩受伤了，安排一个训练计划", "三分化计划怎么安排",
    "今晚吃多少千卡", "糖尿病怎么吃", "I have an injury, recommend a workout",
])
def test_explicit_personal_and_health_questions_do_not_call_model(qa, question):
    client, app, model, _ = qa
    result = ask(client, question).json()
    assert result["status"] == "personal_scope"
    assert not result["model_called"] and not result["sources"]
    assert not model.messages and calls(app) == 0


@pytest.mark.parametrize("profile,question", [
    ({"food_allergies": "西兰花"}, "饮食搭配"),
    ({"preferences": "16岁，刚开始运动"}, "力量训练频率"),
    ({"preferences": "近期肩部疼痛"}, "力量训练频率"),
])
def test_latest_profile_restrictions_stop_relevant_domain(qa, profile, question):
    client, app, model, _ = qa
    assert client.put("/api/profile", json=profile).status_code == 200
    assert ask(client, question).json()["status"] == "personal_scope"
    assert calls(app) == 0 and not model.messages


def test_unrelated_food_restriction_does_not_block_training_knowledge(qa):
    client, app, _, _ = qa
    client.put("/api/profile", json={"food_allergies": "西兰花"})
    assert ask(client).json()["status"] == "answered"
    assert calls(app) == 1


def test_no_evidence_has_no_cost(qa):
    client, app, model, _ = qa
    result = ask(client, "火星飞船引擎").json()
    assert result["status"] == "insufficient" and not result["model_called"]
    assert not result["sources"] and not model.messages and calls(app) == 0


@pytest.mark.parametrize("status", ["personal_scope", "insufficient"])
def test_model_can_decline_keyword_matches(qa, status):
    client, app, model, _ = qa
    model.output = json.dumps({"status": status, "chunk_ids": []})
    result = ask(client).json()
    assert result["status"] == status and result["sources"] == []
    assert result["model_called"] and calls(app) == 1


@pytest.mark.parametrize("output", [
    "not JSON",
    '{"status":"answered","chunk_ids":["made-up"]}',
    '{"status":"answered","chunk_ids":[]}',
    '{"status":"insufficient","chunk_ids":["cdc-adults:strength"]}',
    '{"status":"answered","chunk_ids":["cdc-adults:strength","cdc-adults:strength"]}',
    '{"status":"answered","chunk_ids":["cdc-adults:strength"],"answer":"unsafe invented prescription"}',
    '{"status":"answered","chunk_ids":["fda-label:energy"]}',
])
def test_invalid_or_unoffered_citations_never_reach_user(qa, output):
    client, app, model, _ = qa
    model.output = output
    response = ask(client)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MODEL_INVALID_OUTPUT"
    assert "unsafe invented" not in response.text
    assert calls(app) == 1 and len(model.messages) == 1


def test_provider_error_is_not_retried(qa):
    client, app, model, _ = qa
    model.output = ModelError("MODEL_TIMEOUT", "测试超时", 504)
    assert ask(client).status_code == 504
    assert calls(app) == 1 and len(model.messages) == 1


def test_global_budget_is_shared_and_not_reserved_on_over_limit(qa):
    client, app, model, _ = qa
    app.state.settings = replace(app.state.settings, ai_global_daily_limit=1)
    assert ask(client).status_code == 200
    with TestClient(app) as other:
        register(other, "bob")
        assert ask(other).status_code == 429
    assert calls(app) == 1 and len(model.messages) == 1


def test_profile_is_rechecked_after_model_returns(qa):
    client, app, model, user = qa
    model.on_call = lambda: RecordService(app.state.database, user["id"]).save_profile(Profile(preferences="孕期"))
    result = ask(client).json()
    assert result["status"] == "personal_scope" and result["sources"] == []
    assert result["model_called"] and calls(app) == 1


def test_other_accounts_restriction_is_not_used(qa):
    client, app, model, _ = qa
    client.put("/api/profile", json={"preferences": "孕期"})
    with TestClient(app) as other:
        register(other, "bob")
        assert ask(other).json()["status"] == "answered"
    assert ask(client).json()["status"] == "personal_scope"
    assert len(model.messages) == 1


@pytest.mark.parametrize("action", ["withdraw", "edit", "expire"])
def test_changed_sources_while_waiting_are_not_presented(qa, action):
    client, app, model, _ = qa
    def change(data):
        source = next(s for s in data["sources"] if s["id"] == "cdc-adults")
        if action == "withdraw":
            source["status"] = "withdrawn"
        elif action == "expire":
            source.update(reviewed_on="2025-10-01", review_due="2026-03-01")
        else:
            source["sections"][1]["summary"] = "Updated source summary"
    model.on_call = lambda: mutate(app.state.knowledge, change)
    assert ask(client).status_code == 409
    assert calls(app) == 1


def test_auth_input_and_origin_checks(client):
    assert ask(client).status_code == 401
    register(client)
    for body in [{"question": " "}, {"question": "x" * 1001}, {"question": "MET", "user_id": 2}]:
        assert client.post("/api/knowledge/ask", json=body).status_code == 422
    assert client.post("/api/knowledge/ask", json={"question": "MET"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_disabled_model_keeps_free_search(client, application):
    register(client)
    assert client.get("/api/health").json()["knowledge_qa"] == "not_configured"
    assert ask(client).status_code == 503
    assert calls(application) == 0
    assert client.post("/api/knowledge/search", json={"query": "力量训练频率"}).status_code == 200
    with pytest.raises(ModelError):
        create_knowledge_model(application.state.settings)
    configured = replace(application.state.settings, knowledge_qa_enabled=True, model_provider="qwen", qwen_api_key="test")
    assert knowledge_qa_status(configured) == "configured_unverified"
