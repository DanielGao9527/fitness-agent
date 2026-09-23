from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import create_app
from schemas import TrainingExecutionCommit
from services.training_execution import TrainingExecutionService
from test_foundation import DAY, PASSWORD, application, client, register
from test_knowledge import library
from test_knowledge_answers import calls
from test_training_plans import plans, generate, accept
from test_workouts import model, preview


def open_draft(client, plan_id):
    response = client.post(f"/api/training-plans/{plan_id}/execution", json={"reviewed": True})
    assert response.status_code == 200, response.text
    return response.json()


def values(draft, **changes):
    return {key: draft[key] for key in ("version", "day", "weight_kg", "notes")} | {
        "items": [{key: value for key, value in item.items() if key != "calorie_estimate"} for item in draft["items"]]
    } | changes


def save(client, draft, **changes):
    return client.put(f"/api/training-plans/{draft['plan_id']}/execution", json=values(draft, **changes))


def commit(client, draft, **changes):
    return client.post(f"/api/training-plans/{draft['plan_id']}/execution/commit",
                       json={"version": draft["version"], "reviewed": True, **changes})


def records(client, day=DAY):
    return client.get(f"/api/workouts?day={day}").json()


@pytest.mark.parametrize("activity,count", [("walk", 1), ("cycle", 3)])
def test_names_only_open_idempotent_no_actual_records(plans, activity, count):
    client, app, _, _ = plans
    plan = generate(client, activity=activity, bicycle_available=True).json()
    draft = open_draft(client, plan["id"])
    assert len(draft["items"]) == count
    assert all(item["minutes"] is None and item["calorie_preview_id"] is None for item in draft["items"])
    assert draft["weight_kg"] is None
    assert open_draft(client, plan["id"]) == draft
    assert records(client) == [] and calls(app) == 1
    assert commit(client, draft).status_code == 422
    assert accept(client, plan).status_code == 200
    assert open_draft(client, plan["id"]) == draft
    assert records(client) == []


def test_save_resume_restart_commit_and_deleted_record_replay(plans):
    client, app, _, _ = plans
    plan = generate(client).json()
    draft = open_draft(client, plan["id"])
    body = values(draft, day="2026-09-16", items=[{"name": "实际步行", "minutes": 12}, {"name": "胸和三头", "minutes": 40}])
    url = f"/api/training-plans/{plan['id']}/execution"
    saved = client.put(url, json=body).json()
    assert saved["version"] == 2
    assert client.put(url, json=body).json() == saved
    assert records(client, "2026-09-16") == []
    assert client.get("/api/training-plans/executions").json() == [saved]
    with TestClient(create_app(replace(app.state.settings, qwen_api_key=None, training_plan_enabled=False))) as restarted:
        assert restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
        assert open_draft(restarted, plan["id"]) == saved
        result = commit(restarted, saved)
        assert result.status_code == 200, result.text
        actual = records(restarted, "2026-09-16")
        assert [row["minutes"] for row in actual] == [12, 40]
        assert all(row["status"] == "completed" and "calorie_estimate" not in row for row in actual)
        summary = restarted.get("/api/summary?day=2026-09-16").json()
        assert summary["completed_minutes"] == 52 and summary["estimated_workout_calories"]["unknown_count"] == 2
        assert records(restarted) == []
        assert commit(restarted, saved).json() == result.json()
        assert restarted.delete(f"/api/workouts/{actual[0]['id']}").status_code == 204
        assert commit(restarted, saved).json() == result.json()
        assert len(records(restarted, "2026-09-16")) == 1
        assert restarted.get("/api/training-plans/executions").json() == []
        assert save(restarted, saved).status_code == 409
    assert calls(app) == 1


def test_version_conflicts_and_partial_draft(plans):
    client, _, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    saved = save(client, draft, items=[{"name": "", "minutes": None}]).json()
    assert saved["version"] == 2
    assert save(client, draft, notes="different tab").status_code == 409
    assert commit(client, draft).status_code == 409
    assert commit(client, saved).status_code == 422
    assert records(client) == []


@pytest.mark.parametrize("changes", [{"version": True}, {"version": "1"}, {"day": "bad"}, {"user_id": 2},
    {"weight_kg": 0}, {"items": []}, {"items": [{"name": "Walk", "minutes": True}]},
    {"items": [{"name": "Walk", "minutes": 0}]}, {"items": [{"name": "Walk", "minutes": 601}]},
    {"items": [{"name": "Walk", "minutes": 10, "status": "planned"}]},
    {"items": [{"name": "Walk", "minutes": 10, "calorie_estimate": {"kcal": 200}}]}])
def test_invalid_or_forged_draft_input(plans, changes):
    client, _, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    assert save(client, draft, **changes).status_code == 422
    assert open_draft(client, draft["plan_id"]) == draft
    assert records(client) == []


@pytest.mark.parametrize("reviewed", [False, 1, "true", None])
def test_confirmation_is_strict(plans, reviewed):
    client, _, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    assert commit(client, draft, reviewed=reviewed).status_code == 422


def test_cross_user_auth_and_origin(plans):
    client, app, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    url = f"/api/training-plans/{draft['plan_id']}/execution"
    with TestClient(app) as other:
        assert other.get("/api/training-plans/executions").status_code == 401
        assert other.post(url, json={"reviewed": True}).status_code == 401
        register(other, "bob")
        assert other.get("/api/training-plans/executions").json() == []
        assert other.post(url, json={"reviewed": True}).status_code == 404
        assert save(other, draft).status_code == commit(other, draft).status_code == 404
        assert other.request("DELETE", url, json={"version": 1, "reviewed": True}).status_code == 404
    assert client.post(url, json={"reviewed": True}, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    assert open_draft(client, draft["plan_id"]) == draft


def test_expired_or_changed_preview_rolls_back_every_record(plans, model):
    client, app, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    p = preview(client, name="Walk", minutes=12, intensity="normal_assumed", details="")
    changes = {"weight_kg": 70, "items": [{"name": "Other", "minutes": 5},
        {"name": "Walk", "minutes": 12, "calorie_preview_id": p["id"]}]}
    saved = save(client, draft, **changes).json()
    assert saved["items"][1]["calorie_estimate"]["kcal"] == {"lower": 36, "upper": 60}
    changes["items"][1]["minutes"] = 20
    assert save(client, saved, **changes).status_code == 409
    with app.state.database.connect() as connection:
        connection.execute("UPDATE workout_previews SET created_at=0")
    assert commit(client, saved).status_code == 409
    assert records(client) == []
    assert open_draft(client, draft["plan_id"])["status"] == "draft"
    cleared = save(client, saved, items=[{"name": "Walk", "minutes": 20}]).json()
    assert commit(client, cleared).status_code == 200
    assert "calorie_estimate" not in records(client)[0]


def test_actual_estimate_persisted_and_not_added_to_intake(plans, model):
    client, _, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    p = preview(client, name="Walk", minutes=12, intensity="normal_assumed", details="")
    saved = save(client, draft, weight_kg=70, items=[{"name": "Walk", "minutes": 12, "calorie_preview_id": p["id"]}]).json()
    assert commit(client, saved).status_code == 200
    assert records(client)[0]["calorie_estimate"]["kcal"] == {"lower": 36, "upper": 60}
    summary = client.get(f"/api/summary?day={DAY}").json()
    assert summary["estimated_workout_calories"]["lower_total"] == 36
    assert summary["meal_count"] == 0 and summary["estimated_nutrition"]["kcal"]["count"] == 0


def test_parallel_confirmation_single_batch_and_cancel_versions(plans):
    client, app, _, _ = plans
    draft = open_draft(client, generate(client).json()["id"])
    url = f"/api/training-plans/{draft['plan_id']}/execution"
    body = {"version": 1, "reviewed": True}
    result = client.request("DELETE", url, json=body)
    assert result.status_code == 200
    assert client.request("DELETE", url, json=body).json() == result.json()
    assert client.get("/api/training-plans/executions").json() == []
    resumed = open_draft(client, draft["plan_id"])
    assert resumed["version"] == 3 and resumed["items"][0]["minutes"] is None
    assert save(client, draft).status_code == 409
    saved = save(client, resumed, items=[{"name": "Walk", "minutes": 12}]).json()
    user_id = client.get("/api/auth/me").json()["id"]
    service = TrainingExecutionService(app.state.database, user_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.commit(draft["plan_id"], TrainingExecutionCommit(version=saved["version"], reviewed=True)), range(4)))
    assert all(item == results[0] for item in results)
    assert len(records(client)) == 1
    assert client.request("DELETE", url, json={"version": results[0]["version"], "reviewed": True}).status_code == 409


def test_stale_recommendation_can_record_fact_not_regenerate(plans):
    client, app, _, _ = plans
    plan = generate(client).json()
    client.put("/api/profile", json={"preferences": "孕期"})
    assert generate(client).status_code == 409
    draft = open_draft(client, plan["id"])
    saved = save(client, draft, items=[{"name": "实际步行", "minutes": 5}]).json()
    assert commit(client, saved).status_code == 200
    assert accept(client, plan).status_code == 409
    assert calls(app) == 1
