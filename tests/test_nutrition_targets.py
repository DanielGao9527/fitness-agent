from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import PASSWORD, application, client, register, meal
from test_energy_estimates import inputs

TODAY = date.today().isoformat()


def state(client, day=TODAY):
    response = client.get(f"/api/intake-target?day={day}")
    assert response.status_code == 200, response.text
    return response.json()


def setup(client, **values):
    user = register(client)
    assert profile(client, **values).status_code == 200
    return user


def profile(client, **values):
    current = client.get('/api/profile').json()
    return client.put('/api/profile', json={**current, "height_cm": 175, "weight_kg": 70, **values})


def preview(client, standard=None, **values):
    data = state(client)
    return client.post('/api/nutrition-target/preview', json={"day": TODAY, "context_hash": data["context_hash"],
                      "standard": standard or {"inputs": inputs()}, **values})


def request(client, standard=None):
    standard = standard or {"inputs": inputs()}
    result = preview(client, standard)
    assert result.status_code == 200, result.text
    data = result.json()
    return {"day": TODAY, "context_hash": data["target_state"]["context_hash"], "version": data["target_state"]["version"],
            "kcal": data["calculation"]["kcal"], "standard": standard, "client_id": str(uuid4()), "confirmed": True, "general_adult": True}


def save_standard(client, standard=None):
    return client.post('/api/nutrition-target/standard', json=request(client, standard))


def day_body(client, kcal=3500, day=TODAY):
    data = state(client, day)
    return {"day": day, "context_hash": data["context_hash"], "version": data["version"], "kcal": kcal,
            "client_id": str(uuid4()), "confirmed": True, "general_adult": True}


def test_formula_preview_never_writes_and_goals_change_calculation(client, application):
    setup(client)
    values = {}
    for goal in ("maintain", "fat_loss", "muscle_gain"):
        profile(client, goal=goal)
        values[goal] = preview(client).json()["calculation"]
    assert values["fat_loss"]["kcal"] < values["maintain"]["kcal"] < values["muscle_gain"]["kcal"]
    assert values["fat_loss"]["adjustment_kcal"] == -250
    assert values["muscle_gain"]["adjustment_kcal"] == 150
    with application.state.database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intake_targets').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM ai_usage').fetchone()[0] == 0


def test_standing_fixed_day_only_restore_and_restart(client, application):
    setup(client)
    fixed = {"mode": "fixed", "fixed_kcal": 3200}
    saved = save_standard(client, fixed)
    assert saved.status_code == 200, saved.text
    body = day_body(client)
    assert client.post('/api/nutrition-target/day', json=body).status_code == 200
    assert state(client)["target"]["kcal"] == 3500
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert state(client, tomorrow)["target"]["kcal"] == 3200
    profile(client, goal="fat_loss", weight_kg=80)
    assert state(client)["target"]["kcal"] == 3500
    assert state(client, tomorrow)["target"]["kcal"] == 3200
    with TestClient(create_app(application.state.settings)) as other:
        other.post('/api/auth/login', json={"username": "alice", "password": PASSWORD})
        assert state(other)["target"]["kcal"] == 3500
        assert state(other, tomorrow)["target"]["kcal"] == 3200
    assert client.post('/api/nutrition-target/day', json=day_body(client, None)).status_code == 200
    assert state(client)["target"]["kcal"] == 3200 and not state(client)["day_override"]


def test_goal_weight_and_inputs_link_but_body_fat_not_faked(client):
    setup(client)
    first = save_standard(client).json()
    base = first["target"]["kcal"]
    profile(client, goal="fat_loss")
    assert state(client)["status"] == "active" and state(client)["target"]["kcal"] < base
    profile(client, goal="muscle_gain", weight_kg=80)
    larger = state(client)
    assert larger["target"]["kcal"] > base
    version = larger["version"]
    profile(client, goal="muscle_gain", weight_kg=80, body_fat_percent=20, experience="experienced")
    assert state(client)["version"] == version
    assert state(client)["target"]["kcal"] == larger["target"]["kcal"]
    assert save_standard(client, {"inputs": inputs(activity="active")}).json()["target"]["kcal"] > larger["target"]["kcal"]


def test_custom_offsets_fixed_priority_and_no_double_exercise(client):
    setup(client, goal="muscle_gain")
    p = preview(client, {"inputs": inputs(), "muscle_gain_kcal": 300, "offset_kcal": 100}).json()["calculation"]
    assert p["kcal"] == p["maintenance"]["kcal"] + 400
    assert p["exercise_added"] is False
    assert preview(client, {"inputs": inputs(), "muscle_gain_kcal": 500, "offset_kcal": 500}).status_code == 409
    assert preview(client, {"inputs": inputs(), "muscle_gain_kcal": 0, "offset_kcal": -100}).status_code == 409
    fixed = {"mode": "fixed", "fixed_kcal": 3200}
    assert save_standard(client, fixed).json()["target"]["kcal"] == 3200
    profile(client, goal="fat_loss")
    assert state(client)["target"]["kcal"] == 3200


def test_history_not_recalculated_after_profile_change(client, monkeypatch):
    import services.nutrition_targets as module
    setup(client)
    first = save_standard(client).json()
    monkeypatch.setattr(module, 'business_today', lambda: date.today() + timedelta(days=1))
    profile(client, goal="fat_loss", weight_kg=75)
    assert state(client)["target"]["id"] == first["target"]["id"]
    assert state(client)["target"]["kcal"] == first["target"]["kcal"]
    assert state(client, module.business_today().isoformat())["target"]["id"] != first["target"]["id"]


def test_missing_inputs_scope_and_out_of_range_fail_without_fallback(client):
    register(client)
    assert preview(client).status_code == 409
    profile(client)
    assert save_standard(client).status_code == 200
    profile(client, weight_kg=30)
    assert state(client)["status"] == "needs_input" and state(client)["target"]["kcal"] is None
    profile(client, preferences="糖尿病")
    assert state(client)["status"] == "out_of_scope"
    assert preview(client).status_code == 409
    assert client.post('/api/nutrition-target/day', json=day_body(client)).status_code == 409


@pytest.mark.parametrize("change", [{"kcal": True}, {"kcal": 1200}, {"kcal": 5001}, {"user_id": 1}, {"confirmed": False}, {"general_adult": 1}, {"day": "bad"}])
def test_strict_day_input(client, change):
    setup(client)
    assert client.post('/api/nutrition-target/day', json={**day_body(client), **change}).status_code == 422
    assert state(client)["status"] == "unset"


def test_stale_confirmation_and_idempotent_day_cancel(client, application):
    setup(client)
    body = request(client)
    profile(client, weight_kg=75)
    assert client.post('/api/nutrition-target/standard', json=body).status_code == 409
    body = request(client)
    assert client.post('/api/nutrition-target/standard', json=body).status_code == 200
    assert client.post('/api/nutrition-target/standard', json=body).status_code == 200
    assert client.post('/api/nutrition-target/standard', json={**body, "kcal": 3000}).status_code == 409
    override = day_body(client)
    assert client.post('/api/nutrition-target/day', json=override).status_code == 200
    reset = day_body(client, None)
    assert client.post('/api/nutrition-target/day', json=reset).status_code == 200
    assert client.post('/api/nutrition-target/day', json=override).status_code == 200
    assert not state(client)["day_override"]
    with application.state.database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intake_targets').fetchone()[0] == 3
        assert c.execute('SELECT COUNT(*) FROM meals').fetchone()[0] == 0


def test_isolation_auth_origin_and_concurrent_versions(client, application):
    setup(client)
    body = day_body(client)
    assert client.post('/api/nutrition-target/day', json=body, headers={"origin": "https://evil.example"}).status_code == 403
    with TestClient(application) as other:
        assert other.post('/api/nutrition-target/day', json=body).status_code == 401
        register(other, 'bob')
        assert state(other)["status"] == "unset"
        assert client.post('/api/nutrition-target/day', json=body).status_code == 200
        assert state(other)["status"] == "unset"
    a, b = day_body(client, 3300), day_body(client, 3400)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: client.post('/api/nutrition-target/day', json=value).status_code, [a, b]))
    assert sorted(results) == [200, 409]


def test_day_without_baseline_does_not_leak_to_tomorrow(client):
    setup(client)
    assert client.post('/api/nutrition-target/day', json=day_body(client)).status_code == 200
    assert state(client)["target"]["kcal"] == 3500
    assert state(client, (date.today()+timedelta(days=1)).isoformat())["status"] == "unset"


def test_macro_reference_updates_and_known_intake_changes_without_confirmation(client):
    setup(client)
    save_standard(client, {"mode": "fixed", "fixed_kcal": 2200})
    food = client.post('/api/meals', json=meal(day=TODAY, grams=100, kcal_per_100g=400, protein_per_100g=25, carbs_per_100g=50, fat_per_100g=10, source='synthetic')).json()
    get = lambda: client.get(f'/api/summary?day={TODAY}').json()["meal_calculation"]
    first = get()
    assert first["ready"] and first["recorded"]["kcal"]["lower"] == 400
    assert first["record_state"] is None
    profile(client, nutrition_reference='regular_training')
    second = get()
    assert second["macro"]["center"]["protein"] > first["macro"]["center"]["protein"]
    body = {key: value for key, value in food.items() if key not in ('id', 'client_id')}
    body['kcal_per_100g'] = 800
    assert client.put(f'/api/meals/{food["id"]}', json=body).status_code == 200
    assert get()["recorded"]["kcal"]["lower"] == 800
