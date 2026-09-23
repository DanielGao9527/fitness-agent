import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from schemas import EnergyEstimateInputs, EnergyEstimateConfirm
from services.energy_estimates import maintenance_reference
from services.intake_targets import IntakeTargetService
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_intake_targets import save, summary


def inputs(**values):
    return {"age": 30, "equation_sex": "male", "activity": "inactive", "general_adult": True, **values}


def setup(client, **profile):
    user = register(client)
    assert client.put("/api/profile", json={"height_cm": 175, "weight_kg": 70, **profile}).status_code == 200
    return user


def preview_body(client, **values):
    state = client.get(f"/api/intake-target?day={DAY}").json()
    return {"day": DAY, "context_hash": state["context_hash"], "inputs": inputs(), **values}


def preview(client, **values):
    return client.post("/api/intake-target/estimate", json=preview_body(client, **values))


def confirmation(value, **values):
    state, estimate = value["target_state"], value["estimate"]
    return {"client_id": str(uuid4()), "version": state["version"], "context_hash": state["context_hash"],
            "effective_from": state["day"], "kcal": estimate["kcal"], "method": estimate["method"],
            "inputs": estimate["inputs"], "confirmed": True, **values}


@pytest.mark.parametrize("sex,height,weight,activity,raw,kcal", [
    ("male", 175, 70, "inactive", 2552.67, 2550),
    ("male", 175, 70, "low_active", 2754.87, 2750),
    ("male", 175, 70, "active", 2934.62, 2950),
    ("male", 175, 70, "very_active", 3226.67, 3250),
    ("female", 165, 60, "inactive", 2021.00, 2000),
    ("female", 165, 60, "low_active", 2182.87, 2200),
    ("female", 165, 60, "active", 2319.45, 2300),
    ("female", 165, 60, "very_active", 2551.68, 2550),
])
def test_published_adult_equations_in_cm_kg_years(sex, height, weight, activity, raw, kcal):
    estimate = maintenance_reference({"height_cm": height, "weight_kg": weight, "goal": "maintain"},
                                     EnergyEstimateInputs(**inputs(equation_sex=sex, activity=activity)))
    assert estimate["unrounded_kcal"] == pytest.approx(raw)
    assert estimate["kcal"] == kcal and estimate["rounding_kcal"] == 50
    assert estimate["basis"] == "estimated_maintenance" and estimate["exercise_added"] is False


def test_preview_read_only_and_goal_direction_does_not_invent_adjustments(client, application):
    setup(client, goal="fat_loss", food_allergies="西兰花过敏")
    save(client, kcal=2100)
    before = summary(client)["intake_target"]
    response = preview(client)
    assert response.status_code == 200, response.text
    assert response.json()["estimate"]["kcal"] == 2550
    assert summary(client)["intake_target"] == before
    client.post("/api/workouts", json=workout(status="completed", minutes=120))
    assert preview(client).json() == response.json()
    with application.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0
    assert client.get("/api/profile").json()["food_allergies"] == "西兰花过敏"


@pytest.mark.parametrize("values", [{"age": 18}, {"age": 101}, {"age": True}, {"age": "30"}, {"age": 30.5},
    {"equation_sex": ""}, {"equation_sex": "unknown"}, {"activity": ""}, {"activity": "weekly_three"},
    {"general_adult": False}, {"general_adult": 1}, {"general_adult": None}, {"height_cm": 180}, {"user_id": 9}])
def test_estimate_requires_explicit_valid_inputs(client, values):
    setup(client)
    assert preview(client, inputs=inputs(**values)).status_code == 422
    assert summary(client)["intake_target"]["target"] is None


@pytest.mark.parametrize("profile", [{"height_cm": None}, {"weight_kg": None}, {"height_cm": 139},
    {"height_cm": 211}, {"weight_kg": 39}, {"weight_kg": 201}, {"weight_kg": 50}, {"weight_kg": 125},
    {"preferences": "怀孕"}, {"food_allergies": "糖尿病饮食"}, {"preferences": "服药期间"}])
def test_missing_or_outside_scope_keeps_existing_target(client, profile):
    setup(client)
    save(client)
    client.put("/api/profile", json={"height_cm": 175, "weight_kg": 70, **profile})
    old = summary(client)["intake_target"]["target"]
    assert preview(client).status_code == 409
    assert summary(client)["intake_target"]["target"] == old


def test_confirmation_binds_provenance_inputs_versions_and_persistence(client, application):
    setup(client)
    original_profile = client.get("/api/profile").json()
    proposed = preview(client).json()
    body = confirmation(proposed)
    saved = client.post("/api/intake-target/confirm-estimate", json=body)
    assert saved.status_code == 200, saved.text
    target = saved.json()["target"]
    assert target["estimate"] == proposed["estimate"]
    assert target["kcal"] == 2550 and "维持参考" in target["source"]
    assert client.get("/api/profile").json() == original_profile
    assert client.post("/api/intake-target/confirm-estimate", json=body).json() == saved.json()
    assert client.post("/api/intake-target/confirm-estimate", json={**body, "inputs": inputs(age=31)}).status_code == 409
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert summary(restarted)["intake_target"]["target"]["estimate"] == target["estimate"]
    save(client, kcal=None, source="")
    assert client.post("/api/intake-target/confirm-estimate", json=body).json()["status"] == "paused"
    with application.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 2


@pytest.mark.parametrize("values,status", [({"kcal": 2000}, 409), ({"kcal": True}, 422),
    ({"method": "obsolete-v0"}, 422), ({"source": "Forged authority"}, 422), ({"user_id": 2}, 422),
    ({"confirmed": False}, 422), ({"confirmed": 1}, 422), ({"inputs": inputs(general_adult=False)}, 422)])
def test_confirmation_does_not_trust_client_result_or_source(client, values, status):
    setup(client)
    body = confirmation(preview(client).json(), **values)
    assert client.post("/api/intake-target/confirm-estimate", json=body).status_code == status
    assert summary(client)["intake_target"]["target"] is None


def test_stale_profile_and_competing_target_require_review(client):
    setup(client)
    request = preview_body(client)
    body = confirmation(preview(client).json())
    client.put("/api/profile", json={"height_cm": 175, "weight_kg": 75})
    assert client.post("/api/intake-target/estimate", json=request).status_code == 409
    assert client.post("/api/intake-target/confirm-estimate", json=body).status_code == 409
    body = confirmation(preview(client).json())
    save(client, kcal=2300)
    assert client.post("/api/intake-target/confirm-estimate", json=body).status_code == 409
    assert summary(client)["intake_target"]["target"]["kcal"] == 2300


def test_estimate_and_confirmation_require_identity_and_origin(client, application):
    assert client.post("/api/intake-target/estimate", json={}).status_code == 401
    assert client.post("/api/intake-target/confirm-estimate", json={}).status_code == 401
    setup(client)
    request = preview_body(client)
    proposed = preview(client).json()
    body = confirmation(proposed)
    assert client.post("/api/intake-target/estimate", json=request, headers={"Origin":"https://evil.example"}).status_code == 403
    assert client.post("/api/intake-target/confirm-estimate", json=body, headers={"Origin":"https://evil.example"}).status_code == 403
    with TestClient(application) as bob:
        register(bob, "bob")
        assert bob.post("/api/intake-target/estimate", json=request).status_code == 409
        assert bob.post("/api/intake-target/confirm-estimate", json=body).status_code == 409
        assert summary(bob)["intake_target"]["target"] is None


def test_confirm_concurrency_and_old_manual_payload_compatibility(client, application):
    user = setup(client)
    save(client)
    proposed = preview(client).json()
    service = IntakeTargetService(application.state.database, user["id"])
    def submit(body):
        try:
            service.change(EnergyEstimateConfirm(**body), estimated=True)
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, [confirmation(proposed), confirmation(proposed)])) == [200, 409]
    with application.state.database.connect() as c:
        previous = json.loads(c.execute("SELECT input_payload FROM intake_targets WHERE version=1").fetchone()[0])
    assert client.post("/api/intake-target", json=previous).status_code == 200
    assert summary(client)["intake_target"]["target"]["estimate"] is not None


def test_effective_date_history_and_future_changes_remain_separate(client):
    setup(client)
    save(client, kcal=2200)
    body = confirmation(preview(client, day="2026-09-20").json())
    assert client.post("/api/intake-target/confirm-estimate", json=body).status_code == 200
    assert summary(client)["intake_target"]["target"]["kcal"] == 2200
    assert summary(client, "2026-09-20")["intake_target"]["target"]["kcal"] == 2550


def test_high_result_is_rejected_not_clamped():
    with pytest.raises(HTTPException) as error:
        maintenance_reference({"height_cm": 210, "weight_kg": 170, "goal": "maintain"},
                              EnergyEstimateInputs(**inputs(age=19, activity="very_active")))
    assert error.value.status_code == 409
