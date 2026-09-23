import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.coach as coach_api
from app import create_app
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_knowledge import library
from test_training_plans import plans, request, accept
from test_coach import BASE, conversation, send
from test_coach_reviews import ReviewModel, understand, confirm


@pytest.fixture
def training_reviews(plans, monkeypatch):
    client, app, plan_model, _ = plans
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    model = ReviewModel()
    monkeypatch.setattr(coach_api, "create_coach_model", lambda _: model)
    return client, app, model, plan_model


def bound_request(data, **changes):
    return request(coach_id=data["id"], coach_version=data["version"], **changes)


def test_local_changes_preserve_time_and_do_not_call_model(plans):
    client, app, model, _ = plans
    data = send(client, conversation(client), "有氧建议").json()
    assert data["training_context"] == {"kind": "aerobic"}
    data = send(client, data, "接下来还有20分钟").json()
    assert data["training_context"]["time_basis"] == "session"
    key = str(uuid4())
    changed = send(client, data, "不骑车，改成走路", client_id=key).json()
    assert send(client, data, "不骑车，改成走路", client_id=key).json() == changed
    assert changed["training_context"] == {"kind": "aerobic", "minutes": 20, "time_basis": "session", "activity": "walk",
        "profile_basis": {"minutes_per_session": client.get('/api/profile').json()['minutes_per_session']}}
    assert not model.messages
    assert client.get(f"/api/workouts?day={DAY}").json() == []
    with app.state.database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0


def test_form_selection_becomes_next_dialogue_baseline(plans):
    client, _, _, _ = plans
    data = send(client, conversation(client), "有氧建议").json()
    body = bound_request(data, daily_minutes=25, time_basis="session", activity="cycle", bicycle_available=True)
    plan = client.post("/api/training-plans", json=body).json()
    changed = send(client, data, "改成走路").json()
    assert changed["training_context"] == {"kind": "aerobic", "minutes": 25, "time_basis": "session", "activity": "walk"}
    assert changed["training_plans"][0]["id"] == plan["id"]
    assert changed["training_plans"][0]["stale"]
    assert accept(client, plan).status_code == 409
    changed = send(client, changed, "今天只有20分钟").json()
    assert changed["training_context"]["time_basis"] == "unspecified"
    assert "时间口径" in changed["training_question"]


def test_model_activity_only_update_preserves_confirmed_time(training_reviews):
    client, _, model, _ = training_reviews
    data = send(client, conversation(client), "有氧建议").json()
    data = send(client, data, "接下来还有20分钟").json()
    text = "我想把刚才的有氧项目改为平地骑行"
    model.edit = lambda _, out: out.update(scope="aerobic", command="有氧建议", training={"source_text": text, "activity": "cycle"})
    data = send(client, data, text).json()
    ready = understand(client, data).json()
    result = confirm(client, ready).json()
    assert result["training_context"] == {"kind": "aerobic", "minutes": 20, "time_basis": "session", "activity": "cycle",
        "profile_basis": {"minutes_per_session": client.get('/api/profile').json()['minutes_per_session']}}


@pytest.mark.parametrize("basis,minutes,expected", [("session", 20, 20), ("daily", 45, 15)])
def test_session_time_does_not_subtract_completed_twice(plans, basis, minutes, expected):
    client, _, model, _ = plans
    client.post("/api/workouts", json=workout(minutes=30, status="completed"))
    result = client.post("/api/training-plans", json=request(daily_minutes=minutes, time_basis=basis))
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan["total_minutes"] == expected
    assert plan["remaining_minutes"] == expected
    assert model.messages[-1]["time_basis"] == basis
    assert accept(client, plan).status_code == 200


@pytest.mark.parametrize("basis", ["unspecified", "", None, True, "remaining"])
def test_plan_requires_valid_time_basis(plans, basis):
    client, _, model, _ = plans
    assert client.post("/api/training-plans", json=request(time_basis=basis)).status_code == 422
    assert not model.messages


def test_legacy_request_replay_default_time_basis(plans):
    client, app, model, _ = plans
    body = request()
    plan = client.post("/api/training-plans", json=body).json()
    with app.state.database.connect() as con:
        old = json.loads(con.execute("SELECT input_payload FROM training_plans WHERE id=?", (plan["id"],)).fetchone()[0])
        old.pop("time_basis")
        con.execute("UPDATE training_plans SET input_payload=? WHERE id=?", (json.dumps(old), plan["id"]))
    assert client.post("/api/training-plans", json=body).json()["id"] == plan["id"]
    assert len(model.messages) == 1


def test_strength_context_persistent_and_legacy_aerobic_endpoint_rejects(training_reviews):
    client, app, model, plans_model = training_reviews
    text = "今天想练胸和三头，本次40分钟，有哑铃"
    model.edit = lambda _, out: out.update(scope="strength", command="", training={"source_text": text, "minutes": 40, "time_basis": "session", "focus": "胸和三头", "equipment": "哑铃"})
    data = send(client, conversation(client), text).json()
    ready = understand(client, data).json()
    assert ready["pending"] and ready["review"]["training"]["focus"] == "胸和三头"
    data = confirm(client, ready).json()
    assert data["action"] == "training" and data["training_context"]["kind"] == "strength"
    assert client.post("/api/training-plans", json=bound_request(data)).status_code == 409
    assert not plans_model.messages
    later = send(client, data, "怎么练？").json()
    assert later["training_context"] == data["training_context"] and later["action"] == "training"
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert restarted.get(f"{BASE}/{data['id']}").json()["training_context"] == data["training_context"]
    with TestClient(app) as other:
        register(other, "bob")
        assert other.get(f"{BASE}/{data['id']}").status_code == 404
        assert other.post("/api/training-plans", json=bound_request(data)).status_code == 409
    assert client.get(f"/api/workouts?day={DAY}").json() == []
    assert client.get("/api/profile").json()["equipment"] == ""


def test_training_review_minimal_history_and_stale_on_recent_change(training_reviews):
    client, _, model, _ = training_reviews
    client.put("/api/profile", json={"display_name": "PrivateName", "equipment": "哑铃", "experience": "experienced", "weight_kg": 88})
    for day, name, status in [("2026-09-14", "胸和三头", "completed"), ("2026-09-08", "OutsideWindow", "completed"), (DAY, "NotDone", "planned")]:
        assert client.post("/api/workouts", json=workout(day=day, name=name, status=status)).status_code == 201
    text = "本次步行20分钟"
    model.edit = lambda _, out: out.update(scope="aerobic", command="有氧建议", training={"source_text": text, "minutes": 20, "time_basis": "session", "activity": "walk"})
    data = send(client, conversation(client), text).json()
    ready = understand(client, data).json()
    facts = model.messages[-1]["training_facts"]
    assert len(facts["recent_workouts"]) == 1 and facts["recent_workouts"][0]["name"] == "胸和三头"
    assert facts["profile"]["equipment"] == "哑铃"
    assert all(word not in json.dumps(model.messages) for word in ("PrivateName", "weight_kg", "OutsideWindow", "NotDone", "user_id"))
    client.post("/api/workouts", json=workout(day="2026-09-13", name="肩", status="completed"))
    assert confirm(client, ready).status_code == 409
    assert client.get(f"{BASE}/{data['id']}").json()["pending"]


@pytest.mark.parametrize("when", ["during", "accept"])
def test_recent_training_invalidate_bound_plan(plans, when):
    client, _, model, _ = plans
    data = send(client, conversation(client), "有氧建议").json()
    def change():
        client.post("/api/workouts", json=workout(day="2026-09-14", name="胸", status="completed"))
    if when == "during":
        model.on_call = change
        assert client.post("/api/training-plans", json=bound_request(data)).status_code == 409
    else:
        plan = client.post("/api/training-plans", json=bound_request(data)).json()
        change()
        assert accept(client, plan).status_code == 409


@pytest.mark.parametrize("update", [{"minutes": True}, {"minutes": 301}, {"activity": "swim"}, {"activity": "walk"}, {"time_basis": "guess"}, {"source_text": "not in original"}, {"focus": "腿"}, {"equipment": "杠铃"}, {"healthy": True}])
def test_bad_training_extraction_keeps_pending(training_reviews, update):
    client, _, model, _ = training_reviews
    text = "我想练胸，本次20分钟"
    model.edit = lambda _, out: out.update(scope="strength", command="", training={"source_text": text, **update})
    data = send(client, conversation(client), text).json()
    assert understand(client, data).status_code == 502
    assert client.get(f"{BASE}/{data['id']}").json()["pending"]


def test_training_change_cannot_clear_pending_injury(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "有氧建议").json()
    data = send(client, data, "肩疼").json()
    data = send(client, data, "接下来还有20分钟").json()
    assert data["pending"] and "minutes" not in data["training_context"]
    assert client.post("/api/training-plans", json=bound_request(data)).status_code == 409
    assert not model.messages
