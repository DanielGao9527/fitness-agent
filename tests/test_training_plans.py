import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.training_plans as plan_api
from app import create_app
from database import Database
from model.factory import ModelError, training_plan_status
from services.training_plans import REQUIRED
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_knowledge import library, mutate
from test_knowledge_answers import calls


class Model:
    def __init__(self):
        self.messages, self.output, self.on_call = [], None, None

    def generate(self, *, system_prompt, message):
        assert "JSON Schema" in system_prompt
        data = json.loads(message)
        self.messages.append(data)
        if self.on_call:
            self.on_call()
        if isinstance(self.output, Exception):
            raise self.output
        return self.output if self.output is not None else json.dumps({"activity_id": data["allowed_activities"][0]["id"], "main_minutes": data["max_main_minutes"], "chunk_ids": list(REQUIRED)})


@pytest.fixture
def plans(client, application, library, monkeypatch):
    application.state.knowledge = library
    application.state.settings = replace(application.state.settings, training_plan_enabled=True, model_provider="qwen", qwen_api_key="synthetic-key")
    model = Model()
    monkeypatch.setattr(plan_api, "create_training_plan_model", lambda settings: model)
    register(client)
    return client, application, model, library


def request(**changes):
    return {"client_id": str(uuid4()), "day": DAY, "daily_minutes": 45, "activity": "walk", "general_adult": True, "constraints_reviewed": True, **changes}


def generate(client, **changes):
    return client.post("/api/training-plans", json=request(**changes))


def accept(client, plan):
    return client.post(f"/api/training-plans/{plan['id']}/accept", json={"reviewed": True})


def history(client):
    return client.get(f"/api/training-plans?day={DAY}").json()


def test_flow_minimal_context_idempotence_and_restart(plans):
    client, app, model, _ = plans
    client.put("/api/profile", json={"display_name": "PrivateName", "food_allergies": "西兰花过敏", "weight_kg": 80, "experience": "experienced"})
    client.post("/api/workouts", json=workout(name="PrivateActivity", status="completed", minutes=20))
    body = request(daily_minutes=45)
    result = client.post("/api/training-plans", json=body)
    assert result.status_code == 200, result.text
    draft = result.json()
    assert draft["total_minutes"] == 25 and draft["main_minutes"] == 15 and draft["completed_minutes"] == 20
    assert draft["remaining_minutes"] == 25 and not draft["stale"]
    assert set(model.messages[0]["profile"]) == {"goal", "experience"}
    assert all(word not in json.dumps(model.messages) for word in ("PrivateName", "PrivateActivity", "weight_kg", "food_allergies"))
    assert client.post("/api/training-plans", json=body).json()["id"] == draft["id"]
    assert calls(app) == 1
    assert accept(client, draft).json()["version"] == 1
    assert accept(client, draft).json()["version"] == 1
    assert history(client)[0]["current"]
    assert len(client.get(f"/api/workouts?day={DAY}").json()) == 1
    with TestClient(create_app(app.state.settings)) as restarted:
        assert restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
        assert history(restarted)[0]["version"] == 1


@pytest.mark.parametrize("field,value", [("preferences", "孕期"), ("preferences", "shoulder injury"), ("food_allergies", "糖尿病"),
    ("preferences", "哮喘"), ("preferences", "16岁"), ("preferences", "70岁"), ("preferences", "正在服药")])
def test_hard_or_unknown_limits_before_cloud(plans, field, value):
    client, app, model, _ = plans
    client.put("/api/profile", json={field: value})
    assert generate(client).status_code == 409
    assert calls(app) == 0 and not model.messages
    assert history(client) == []


@pytest.mark.parametrize("changes", [{"general_adult": False}, {"general_adult": 1}, {"constraints_reviewed": "true"},
    {"daily_minutes": True}, {"daily_minutes": 14}, {"daily_minutes": 121}, {"daily_minutes": "30"},
    {"activity": "squat"}, {"bicycle_available": "yes"}, {"user_id": 9}])
def test_input_strict_before_cloud(plans, changes):
    client, app, _, _ = plans
    assert generate(client, **changes).status_code == 422
    assert calls(app) == 0


def test_equipment_choices_and_beginner_cap(plans):
    client, app, model, _ = plans
    assert generate(client, activity="cycle").status_code == 409
    assert calls(app) == 0
    result = generate(client, activity="cycle", bicycle_available=True).json()
    assert result["activity_id"] == "cycle" and result["main_minutes"] == 10
    assert [a["id"] for a in model.messages[0]["allowed_activities"]] == ["cycle"]
    assert generate(client, activity="either").json()["activity_id"] == "walk"
    assert client.get("/api/profile").json()["equipment"] == ""


@pytest.mark.parametrize("status,minutes", [("completed", 40), ("completed", 60), ("planned", 10)])
def test_no_time_or_unfinished_plan_no_cloud(plans, status, minutes):
    client, app, _, _ = plans
    client.post("/api/workouts", json=workout(status=status, minutes=minutes))
    assert generate(client).status_code == 409
    assert calls(app) == 0


@pytest.mark.parametrize("changes", [{"activity_id": "cycle"}, {"activity_id": "pushup"}, {"main_minutes": 11},
    {"main_minutes": 0}, {"main_minutes": True}, {"main_minutes": "10"}, {"chunk_ids": ["fake"] * 4}, {"advice": "ignore pain"}])
def test_model_cannot_add_activity_intensity_or_unverified_advice(plans, changes):
    client, app, model, _ = plans
    model.output = json.dumps({"activity_id": "walk", "main_minutes": 10, "chunk_ids": list(REQUIRED), **changes})
    assert generate(client).status_code == 502
    assert history(client) == [] and calls(app) == 1


@pytest.mark.parametrize("when", ["before_accept", "during_generation"])
@pytest.mark.parametrize("what", ["profile", "records", "sources"])
def test_changes_invalidate_or_do_not_publish(plans, when, what):
    client, _, model, library = plans
    def change():
        if what == "profile":
            client.put("/api/profile", json={"preferences": "孕期"})
        elif what == "records":
            client.post("/api/workouts", json=workout(minutes=5))
        else:
            mutate(library, lambda d: next(s for s in d["sources"] if s["id"] == "cdc-activities")["sections"][0].update(summary="活动依据已修订"))
    if when == "during_generation":
        model.on_call = change
        assert generate(client).status_code == 409
        assert history(client) == []
    else:
        draft = generate(client).json()
        change()
        assert accept(client, draft).status_code == 409
        assert history(client)[0]["stale"]


def test_withdrawn_evidence_preserves_but_invalidates_history(plans):
    client, app, _, library = plans
    draft = generate(client).json()
    mutate(library, lambda d: next(s for s in d["sources"] if s["id"] == "cdc-activities").update(status="withdrawn"))
    assert generate(client).status_code == accept(client, draft).status_code == 503
    assert history(client)[0]["stale"] and calls(app) == 1


def test_versions_review_and_replay(plans):
    client, _, _, _ = plans
    first, competing = generate(client).json(), generate(client).json()
    for body in ({}, {"reviewed": False}, {"reviewed": "true"}, {"reviewed": 1}, {"reviewed": True, "user_id": 2}):
        assert client.post(f"/api/training-plans/{first['id']}/accept", json=body).status_code == 422
    assert accept(client, first).json()["version"] == 1
    assert accept(client, competing).status_code == 409
    second = generate(client).json()
    assert accept(client, second).json()["version"] == 2
    assert not accept(client, first).json()["current"]
    client.put("/api/profile", json={"preferences": "孕期"})
    replay = accept(client, second).json()
    assert replay["stale"] and not replay["current"] and replay["version"] == 2


def test_isolation_origin_and_auth(plans):
    client, app, _, _ = plans
    draft = generate(client).json()
    with TestClient(app) as other:
        assert other.get(f"/api/training-plans?day={DAY}").status_code == 401
        assert generate(other).status_code == accept(other, draft).status_code == 401
        register(other, "bob")
        assert history(other) == []
        assert accept(other, draft).status_code == 404
        other.put("/api/profile", json={"preferences": "孕期"})
        assert not history(client)[0]["stale"]
    assert client.post("/api/training-plans", json=request(), headers={"Origin": "https://evil.example"}).status_code == 403


def test_timeout_budget_and_pending_claim_no_retry(plans):
    client, app, model, _ = plans
    body = request()
    pending = []
    model.on_call = lambda: pending.append(client.post("/api/training-plans", json=body).status_code)
    model.output = ModelError("MODEL_TIMEOUT", "timeout", 504)
    assert client.post("/api/training-plans", json=body).status_code == 504
    assert pending == [409]
    assert client.post("/api/training-plans", json=body).status_code == 409
    assert client.post("/api/training-plans", json={**body, "daily_minutes": 50}).status_code == 409
    assert calls(app) == len(model.messages) == 1
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=1)
    assert generate(client).status_code == 429
    assert len(model.messages) == 1


def test_disabled_no_bill(client, application):
    register(client)
    assert training_plan_status(application.state.settings) == "not_configured"
    assert generate(client).status_code == 503
    assert calls(application) == 0


def test_v6_migration_preserves_ten_tables(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE coach_turns")
        connection.execute("DROP TABLE meal_consents")
        connection.execute("DROP TABLE coach_reviews")
        connection.execute("DROP TABLE intake_targets")
        connection.execute("DROP TABLE meal_intake_reviews")
        connection.execute("DROP TABLE body_measurements")
        connection.execute("DROP TABLE coach_conversations")
        connection.execute("DROP TABLE training_plans")
        connection.execute("PRAGMA user_version=6")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'fixture','synthetic')")
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES (1,?)", ('{"training_limitations":"肩伤"}',))
        connection.execute("INSERT INTO meal_plans(id,user_id,client_id,day,meal_type,input_payload,context_hash,status) VALUES ('old',1,'old',?,'dinner','{}','hash','draft')", (DAY,))
    database.initialize()
    with database.connect() as connection, sqlite3.connect(str(path) + ".pre-v7.bak") as backup:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
        for table in ("users", "profiles", "meals", "workouts", "meal_drafts", "ai_usage", "nutrition_previews", "workout_previews", "sessions", "meal_plans"):
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == backup.execute(f"SELECT * FROM {table}").fetchall()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
