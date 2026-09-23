from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from schemas import EnergyAdjustmentConfirm
from services.intake_targets import IntakeTargetService
from test_energy_estimates import inputs, setup, preview_body, confirmation
from test_foundation import DAY, PASSWORD, application, client, meal, register, workout
from test_intake_targets import save, summary


def adjustment(**values):
    return {"purpose": "fat_loss", "amount_kcal": 300, "source": "Synthetic existing plan", "review_on": "2026-09-29", **values}


def proposed(client, **values):
    return client.post("/api/intake-target/adjustment", json=preview_body(client, **{"adjustment": adjustment(), **values}))


def confirmed_body(value, **values):
    return confirmation(value, adjustment=value["estimate"]["adjustment"], **values)


def confirm(client, **values):
    return client.post("/api/intake-target/confirm-adjustment", json=confirmed_body(proposed(client).json(), **values))


@pytest.mark.parametrize("purpose,expected", [("fat_loss", 2250), ("muscle_gain", 2850)])
def test_user_direction_only_read_only_and_explicit_save(client, application, purpose, expected):
    setup(client, goal="maintain", food_allergies="鸡肉过敏")
    assert save(client, kcal=2100).status_code == 200
    profile = client.get("/api/profile").json()
    before = summary(client)["intake_target"]
    response = proposed(client, adjustment=adjustment(purpose=purpose))
    assert response.status_code == 200, response.text
    value = response.json()
    estimate = value["estimate"]
    assert estimate["kcal"] == expected
    assert estimate["basis"] == "user_adjusted_reference"
    assert estimate["maintenance"]["kcal"] == 2550
    assert estimate["maintenance"]["unrounded_kcal"] == 2552.67
    assert estimate["exercise_added"] is False
    assert summary(client)["intake_target"] == before
    client.post("/api/workouts", json=workout(status="completed", minutes=120))
    assert proposed(client, adjustment=adjustment(purpose=purpose)).json() == value
    saved = client.post("/api/intake-target/confirm-adjustment", json=confirmed_body(value))
    assert saved.status_code == 200, saved.text
    assert saved.json()["target"]["estimate"] == estimate
    assert saved.json()["target"]["kcal"] == expected
    assert "非自动建议" in saved.json()["target"]["source"]
    assert client.get("/api/profile").json() == profile
    with application.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 2


@pytest.mark.parametrize("changes", [{"purpose": "maintain"}, {"purpose": ""}, {"amount_kcal": 0},
    {"amount_kcal": -100}, {"amount_kcal": 550}, {"amount_kcal": 125}, {"amount_kcal": "300"},
    {"amount_kcal": True}, {"amount_kcal": 300.0}, {"source": " "}, {"source": "x" * 201},
    {"review_on": None}, {"review_on": "not-a-date"}, {"user_id": 123}, {"maintenance_kcal": 2600}])
def test_invalid_adjustment_never_writes(client, changes):
    setup(client)
    assert proposed(client, adjustment=adjustment(**changes)).status_code == 422
    assert summary(client)["intake_target"]["target"] is None


@pytest.mark.parametrize("review_on", ["2026-09-14", DAY, "2026-10-14"])
def test_review_date_outside_window_rejected_on_preview_and_save(client, review_on):
    setup(client)
    value = proposed(client).json()
    assert proposed(client, adjustment=adjustment(review_on=review_on)).status_code == 409
    body = confirmed_body(value)
    body["adjustment"]["review_on"] = review_on
    assert client.post("/api/intake-target/confirm-adjustment", json=body).status_code == 409
    assert summary(client)["intake_target"]["target"] is None


@pytest.mark.parametrize("review_on", ["2026-09-16", "2026-10-13"])
def test_review_window_endpoints_are_accepted(client, review_on):
    setup(client)
    assert proposed(client, adjustment=adjustment(review_on=review_on)).status_code == 200


@pytest.mark.parametrize("profile,input_values,change,allowed", [
    ({"height_cm": 165, "weight_kg": 60}, {"equation_sex": "female"}, {"amount_kcal": 400}, True),
    ({"height_cm": 165, "weight_kg": 60}, {"equation_sex": "female"}, {"amount_kcal": 450}, False),
    ({"height_cm": 140, "weight_kg": 40}, {"age": 74, "equation_sex": "female"}, {"amount_kcal": 150}, False),
    ({"height_cm": 140, "weight_kg": 40}, {"age": 74, "equation_sex": "female"}, {"amount_kcal": 100}, True),
    ({"height_cm": 210, "weight_kg": 130}, {"activity": "very_active"}, {"purpose": "muscle_gain", "amount_kcal": 150}, False),
    ({"preferences": "孕期"}, {}, {}, False),
    ({"weight_kg": None}, {}, {}, False),
])
def test_bounds_or_health_scope_do_not_clamp_existing_target(client, profile, input_values, change, allowed):
    setup(client)
    save(client)
    client.put("/api/profile", json={"height_cm": 175, "weight_kg": 70, **profile})
    previous = summary(client)["intake_target"]["target"]
    result = proposed(client, inputs=inputs(**input_values), adjustment=adjustment(**change))
    assert result.status_code == (200 if allowed else 409), result.text
    assert summary(client)["intake_target"]["target"] == previous


def test_due_date_preserves_history_pauses_difference_and_needs_explicit_new_version(client, application):
    setup(client)
    body = confirmed_body(proposed(client).json())
    assert client.post("/api/intake-target/confirm-adjustment", json=body).status_code == 200
    for day in (DAY, "2026-09-28", "2026-09-29", "2026-10-01"):
        client.post("/api/meals", json=meal(day=day, kcal_per_100g=100, source="Synthetic label"))
    original = summary(client)["intake_target"]["target"]
    for day in (DAY, "2026-09-28"):
        assert summary(client, day)["intake_comparison"]["status"] == "ready"
    for day in ("2026-09-29", "2026-10-01"):
        current = summary(client, day)
        assert current["intake_target"]["status"] == "review_due"
        assert current["intake_target"]["target"] == original
        assert current["intake_comparison"]["difference_kcal"] is None
        assert current["intake_comparison"]["recorded_kcal"] is not None
    client.post("/api/intake-target/confirm-adjustment", json=body)
    assert summary(client, "2026-09-29")["intake_target"]["status"] == "review_due"
    next_value = proposed(client, day="2026-09-29", adjustment=adjustment(review_on="2026-10-13")).json()
    response = client.post("/api/intake-target/confirm-adjustment", json=confirmed_body(next_value))
    assert response.status_code == 200 and response.json()["status"] == "active"
    assert summary(client)["intake_target"]["target"] == original
    with application.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 2


@pytest.mark.parametrize("patch,status", [({"kcal": 2000}, 409), ({"kcal": True}, 422),
    ({"method": "dri2023-adult-v1"}, 422), ({"source": "Forged authority"}, 422),
    ({"confirmed": 1}, 422), ({"confirmed": False}, 422), ({"user_id": "another"}, 422),
    ({"inputs": inputs(general_adult=False)}, 422)])
def test_confirm_strict_schema_and_recomputed_final(client, patch, status):
    setup(client)
    assert confirm(client, **patch).status_code == status
    assert summary(client)["intake_target"]["target"] is None


def test_tampered_adjustment_requires_matching_recalculation_and_scope(client):
    setup(client)
    body = confirmed_body(proposed(client).json())
    body["adjustment"]["amount_kcal"] = 400
    assert client.post("/api/intake-target/confirm-adjustment", json=body).status_code == 409
    body["kcal"] = 2150
    assert client.post("/api/intake-target/confirm-adjustment", json=body).status_code == 200
    request = preview_body(client, adjustment=adjustment())
    body = confirmed_body(proposed(client).json())
    client.put("/api/profile", json={"height_cm": 175, "weight_kg": 70, "preferences": "糖尿病"})
    assert client.post("/api/intake-target/adjustment", json=request).status_code == 409
    assert client.post("/api/intake-target/confirm-adjustment", json=body).status_code == 409
    assert summary(client)["intake_target"]["status"] == "out_of_scope"


def test_idempotency_conflict_restart_pause_and_legacy_request(client, application):
    setup(client)
    save(client)
    body = confirmed_body(proposed(client).json())
    saved = client.post("/api/intake-target/confirm-adjustment", json=body)
    assert client.post("/api/intake-target/confirm-adjustment", json=body).json() == saved.json()
    changed = {**body, "adjustment": adjustment(source="Changed")}
    assert client.post("/api/intake-target/confirm-adjustment", json=changed).status_code == 409
    assert client.post("/api/intake-target/confirm-adjustment", json={**body, "client_id": str(uuid4())}).status_code == 409
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert summary(restarted)["intake_target"]["target"] == saved.json()["target"]
        assert summary(restarted, "2026-09-29")["intake_target"]["status"] == "review_due"
    save(client, kcal=None, source="")
    assert client.post("/api/intake-target/confirm-adjustment", json=body).json()["status"] == "paused"
    assert summary(client, "2026-09-29")["intake_target"]["status"] == "paused"


def test_auth_origin_and_cross_user_with_same_inputs(client, application):
    for path in ("adjustment", "confirm-adjustment"):
        assert client.post(f"/api/intake-target/{path}", json={}).status_code == 401
    setup(client)
    request = preview_body(client, adjustment=adjustment())
    body = confirmed_body(proposed(client).json())
    for path, payload in (("adjustment", request), ("confirm-adjustment", body)):
        assert client.post(f"/api/intake-target/{path}", json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/intake-target/confirm-adjustment", json=body).status_code == 200
    with TestClient(application) as bob:
        register(bob, "bob")
        assert bob.post("/api/intake-target/adjustment", json=request).status_code == 409
        assert bob.post("/api/intake-target/confirm-adjustment", json=body).status_code == 409
        bob.put("/api/profile", json={"height_cm": 175, "weight_kg": 70})
        # Matching facts may have matching hashes, but writes and replay IDs remain per user.
        assert bob.post("/api/intake-target/confirm-adjustment", json=body).status_code == 200
        save(bob, kcal=None, source="")
        assert summary(client)["intake_target"]["status"] == "active"


def test_competing_adjustments_one_winner_and_old_targets_do_not_expire(client, application):
    user = setup(client)
    service = IntakeTargetService(application.state.database, user["id"])
    value = proposed(client).json()
    def submit(body):
        try:
            return service.change(EnergyAdjustmentConfirm(**body), adjusted=True)["version"]
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, [confirmed_body(value), confirmed_body(value)])) == [1, 409]
    save(client, kcal=2300)
    assert summary(client, "2027-01-01")["intake_target"]["status"] == "active"
    from test_energy_estimates import preview
    assert client.post("/api/intake-target/confirm-estimate", json=confirmation(preview(client).json())).status_code == 200
    assert service.get(date(2027, 1, 1))["status"] == "active"
