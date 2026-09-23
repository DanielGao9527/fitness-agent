from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient

from app import create_app
from services.business_time import business_today
from services.energy_feedback import training_increment, unadjusted
from services.energy_estimates import maintenance_reference
from schemas import EnergyEstimateInputs
from test_foundation import application, client, meal, register, PASSWORD
from test_nutrition_targets import setup, save_standard, state, profile, day_body
from test_meal_intake import context, URL


def baseline(client, application, **values):
    user = setup(client, **values)
    response = save_standard(client)
    assert response.status_code == 200, response.text
    with application.state.database.connect() as connection:
        connection.execute("UPDATE intake_targets SET effective_from=? WHERE user_id=?",
                           ((business_today() - timedelta(days=10)).isoformat(), user["id"]))
    return user, state(client)["target"]["kcal"]


def records(client, kcal, offset=1, kinds=("breakfast", "lunch", "dinner"), reviewed=True):
    day = (business_today() - timedelta(days=offset)).isoformat()
    output = []
    for kind in kinds:
        response = client.post("/api/meals", json=meal(day=day, meal_type=kind, name="Synthetic food", grams=300,
                              kcal_per_100g=kcal / len(kinds) / 3, source="Synthetic label"))
        assert response.status_code == 201, response.text
        output.append(response.json())
    if reviewed:
        current = context(client, day)
        response = client.put(URL, json={"day": day, "version": current["version"], "context_hash": current["context_hash"],
                              "record_state": "complete", "confirmed": True})
        assert response.status_code == 200, response.text
    return output


@pytest.mark.parametrize("difference,expected", [(300, 100), (-300, -100), (0, 0)])
def test_reviewed_history_adjusts_gradually_without_writes(client, application, difference, expected):
    _, kcal = baseline(client, application)
    records(client, kcal - difference)
    with application.state.database.connect() as connection:
        before = list(connection.iterdump())
    result = state(client)
    assert result["energy_feedback"]["adjustment_kcal"] == expected
    assert result["target"]["kcal"] == kcal + expected
    assert result["baseline"]["kcal"] == kcal
    assert result["target"]["calculation"]["kcal"] == kcal
    assert unadjusted(result)["target"]["kcal"] == kcal
    for _ in range(3):
        assert state(client) == result
    with application.state.database.connect() as connection:
        assert list(connection.iterdump()) == before
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert state(restarted) == result


def test_cap_manual_override_and_baseline_change(client, application):
    _, kcal = baseline(client, application)
    records(client, kcal - 1000)
    adjusted = state(client)
    assert 0 < adjusted["energy_feedback"]["adjustment_kcal"] <= min(150, kcal * .05)
    assert client.post("/api/nutrition-target/day", json=day_body(client, 3200)).status_code == 200
    assert state(client)["target"]["kcal"] == 3200
    assert state(client)["energy_feedback"]["status"] == "manual_override"
    profile(client, goal="muscle_gain")
    assert state(client)["target"]["kcal"] == 3200
    assert client.post("/api/nutrition-target/day", json=day_body(client, None)).status_code == 200
    assert state(client)["energy_feedback"]["eligible_days"] == 0
    assert state(client)["target"]["kcal"] == state(client)["baseline"]["kcal"]


@pytest.mark.parametrize("options", [{"kinds": ("breakfast",)}, {"reviewed": False}, {"offset": 8}])
def test_missing_unreviewed_and_old_days_are_not_zero(client, application, options):
    _, kcal = baseline(client, application)
    records(client, kcal - 500, **options)
    assert state(client)["energy_feedback"]["eligible_days"] == 0
    assert state(client)["target"]["kcal"] == kcal


def test_stale_review_and_cross_user_isolation(client, application):
    _, kcal = baseline(client, application)
    meals = records(client, kcal - 300)
    assert state(client)["energy_feedback"]["eligible_days"] == 1
    with TestClient(application) as other:
        register(other, "bob")
        profile(other)
        assert save_standard(other).status_code == 200
        assert state(other)["energy_feedback"]["eligible_days"] == 0
    row = meals[0]
    changed = {key: value for key, value in row.items() if key != "id"}
    changed["grams"] = 310
    assert client.put(f"/api/meals/{row['id']}", json=changed).status_code == 200
    assert state(client)["energy_feedback"]["eligible_days"] == 0
    assert state(client)["target"]["kcal"] == kcal


def test_future_and_history_are_not_rewritten(client, application):
    _, kcal = baseline(client, application)
    records(client, kcal - 300)
    assert state(client)["target"]["kcal"] > kcal
    for offset in (-1, 1):
        result = state(client, (business_today() + timedelta(days=offset)).isoformat())
        assert result["target"]["kcal"] == kcal
        assert "energy_feedback" not in result


def training(calories=(400, 500), **changes):
    return {"status": "completed", "minutes": 60, "calorie_estimate": {"basis": "gross_activity", "kcal": dict(zip(("lower", "upper"), calories))}, **changes}


def test_gross_activity_never_added_twice_and_unknown_is_skipped(client, application):
    baseline(client, application)
    calculation = state(client)["target"]["calculation"]
    inactive = maintenance_reference(calculation["measurements"], EnergyEstimateInputs.model_validate(
        {**calculation["maintenance"]["inputs"], "activity": "inactive"}))["kcal"]
    expected = tuple(max(0, inactive * 23 / 24 + value - calculation["maintenance"]["kcal"]) for value in (400, 500))
    assert training_increment([training()], calculation) == pytest.approx(expected)
    assert training_increment([training(status="planned")], calculation) == (0, 0)
    assert training_increment([training(calorie_estimate=None)], calculation) is None
    assert training_increment([training(minutes=-1)], calculation) is None
    assert training_increment([training(calories=(500, 400))], calculation) is None
    assert training_increment([training(calories=(0, float("nan")))], calculation) is None
    active = deepcopy(calculation)
    active["maintenance"]["kcal"] = inactive + 900
    assert training_increment([training()], active) == (0, 0)


def test_unknown_training_skips_day_and_complete_training_is_used(client, application):
    user, kcal = baseline(client, application)
    records(client, kcal)
    day = (business_today() - timedelta(days=1)).isoformat()
    from test_foundation import workout
    saved = client.post("/api/workouts", json=workout(day=day, status="completed")).json()
    assert state(client)["energy_feedback"]["eligible_days"] == 0
    with application.state.database.connect() as connection:
        row = json.loads(connection.execute("SELECT payload FROM workouts WHERE id=? AND user_id=?", (saved["id"], user["id"])).fetchone()[0])
        row.update(training(calories=(1500, 1550)))
        connection.execute("UPDATE workouts SET payload=? WHERE id=? AND user_id=?", (json.dumps(row), saved["id"], user["id"]))
    result = state(client)
    assert result["energy_feedback"]["eligible_days"] == 1
    assert result["target"]["kcal"] > kcal
