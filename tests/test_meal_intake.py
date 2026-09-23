import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from schemas import MealIntakeReview
from services.meal_intake import MealIntakeService
from test_foundation import DAY, PASSWORD, application, client, meal, register, workout
from test_intake_targets import save, summary
from test_knowledge import library
from test_meal_plans import plans, request, generate, accept
from test_nutrition import result

URL = '/api/meal-plans/intake-context'


def context(client, day=DAY):
    response = client.get(f'{URL}?day={day}')
    assert response.status_code == 200, response.text
    return response.json()


def review_body(client, **changes):
    current = context(client)
    return {"day": DAY, "version": current["version"], "context_hash": current["context_hash"],
            "record_state": "complete" if current["record_count"] else "none_yet", "confirmed": True, **changes}


def review(client, **changes):
    return client.put(URL, json=review_body(client, **changes))


def revoke(client, version=None):
    return client.post(URL + '/revoke', json={"day": DAY, "version": context(client)["version"] if version is None else version})


def test_no_target_legacy_planning_and_read_only_context(plans):
    client, app, model, _ = plans
    assert context(client)["status"] == "unset"
    assert review(client).status_code == 409
    assert generate(client).status_code == 200
    assert "intake_reference" not in model.messages[0]
    with app.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM meal_intake_reviews").fetchone()[0] == 0


def test_empty_is_unknown_until_explicit_and_old_summary_unchanged(plans):
    client, app, model, _ = plans
    save(client, kcal=2000)
    current = context(client)
    assert current["status"] == "confirm_empty" and current["recorded_kcal"] is None
    assert current["remaining_kcal"] is None
    assert generate(client, intake_context_hash=current["context_hash"]).status_code == 409
    assert not model.messages
    value = review(client).json()
    assert value["status"] == "ready" and value["recorded_kcal"] == {"lower": 0, "upper": 0}
    assert value["remaining_kcal"] == {"lower": 2000, "upper": 2000}
    assert summary(client)["intake_comparison"]["status"] == "no_records"
    assert context(client, '2026-09-16')["status"] == 'confirm_empty'
    with app.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [{"confirmed": False}, {"confirmed": 1}, {"confirmed": None},
    {"record_state": "guessed"}, {"version": True}, {"version": -1}, {"version": "0"},
    {"context_hash": "fake"}, {"user_id": 1}, {"target_kcal": 2000}, {"recorded_kcal": 0}])
def test_strict_confirmation(plans, changes):
    client, _, _, _ = plans
    save(client)
    assert review(client, **changes).status_code == 422
    assert context(client)["record_state"] is None


def test_confirmation_consistent_with_records_and_unknown_not_zero(plans):
    client, _, _, _ = plans
    save(client)
    assert review(client, record_state='complete').status_code == 409
    record = client.post('/api/meals', json=meal()).json()
    assert context(client)["status"] == 'unknown_intake'
    assert review(client).status_code == 409
    client.delete(f"/api/meals/{record['id']}")
    client.post('/api/meals', json=meal(kcal_per_100g=0, source='Synthetic zero label'))
    assert review(client, record_state='none_yet').status_code == 409
    value = review(client).json()
    assert value['record_state'] == 'complete' and value['recorded_kcal'] == {'lower': 0, 'upper': 0}


def test_shared_range_arithmetic_manual_precedence_no_workout_addition(plans):
    client, app, _, _ = plans
    save(client)
    records = [client.post('/api/meals', json=meal(kcal_per_100g=60, source='Synthetic label')).json(),
               client.post('/api/meals', json=meal(name='Synthetic estimate')).json()]
    with app.state.database.connect() as c:
        for record in records:
            payload = {key: value for key, value in record.items() if key != 'id'}
            payload['nutrition_estimate'] = result()
            c.execute('UPDATE meals SET payload=? WHERE id=?', (json.dumps(payload), record['id']))
    value = review(client).json()
    assert value['recorded_kcal'] == {'lower': 250, 'upper': 350}
    assert value['remaining_kcal'] == {'lower': 1750, 'upper': 1850}
    assert value['recorded_kcal'] == summary(client)['intake_comparison']['recorded_kcal']
    client.post('/api/workouts', json=workout(status='completed', minutes=120))
    assert context(client) == value


@pytest.mark.parametrize('kcal,status', [(2100, 'target_reached'), (2300, 'target_reached')])
def test_nonpositive_difference_does_not_offer_numeric_planning(plans, kcal, status):
    client, _, model, _ = plans
    save(client)
    response = client.post('/api/meals', json=meal(grams=1000, kcal_per_100g=kcal / 10, source='Synthetic intake'))
    assert response.status_code == 201, response.text
    value = review(client).json()
    assert value['status'] == status
    assert generate(client, intake_context_hash=value['context_hash']).status_code == 409
    assert not model.messages
    assert generate(client).status_code == 200


def test_crossing_zero_range_is_not_clamped_or_used(plans):
    client, app, _, _ = plans
    save(client, kcal=1000)
    record = client.post('/api/meals', json=meal()).json()
    payload = {key: value for key, value in record.items() if key != 'id'}
    payload['nutrition_estimate'] = result()
    payload['nutrition_estimate']['kcal'] = {'lower': 900, 'upper': 1100}
    with app.state.database.connect() as c:
        c.execute('UPDATE meals SET payload=? WHERE id=?', (json.dumps(payload), record['id']))
    value = review(client).json()
    assert value['status'] == 'uncertain_remaining'
    assert value['remaining_kcal'] == {'lower': -100, 'upper': 100}
    assert generate(client, intake_context_hash=value['context_hash']).status_code == 409


def test_minimum_model_context_allergies_and_no_personal_formula_upload(plans):
    client, app, model, _ = plans
    client.put('/api/profile', json={'height_cm': 175, 'weight_kg': 70, 'food_allergies': '西兰花过敏'})
    save(client, source='PRIVATE-SOURCE-CANARY')
    reviewed = review(client).json()
    body = request(intake_context_hash=reviewed['context_hash'])
    response = client.post('/api/meal-plans', json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['intake_reference'] == model.messages[0]['intake_reference']
    reference = plan['intake_reference']
    assert reference['daily_target_kcal'] == 2100 and reference['requested_meals'] == ['dinner']
    assert reference['allocation'] == 'not_allocated' and reference['exercise_added'] is False
    sent = json.dumps(model.messages)
    assert 'PRIVATE-SOURCE-CANARY' not in sent and reviewed['context_hash'] not in sent
    for key in ('age', 'equation_sex', 'coefficients', 'user_id', 'version', 'source'):
        assert key not in reference
    assert all(item['food_id'] != 'broccoli' for item in plan['items'])
    assert client.post('/api/meal-plans', json=body).json()['id'] == plan['id']
    assert len(model.messages) == 1
    assert accept(client, plan).status_code == 200
    assert not client.get(f'/api/meals?day={DAY}').json()


@pytest.mark.parametrize('change', ['record', 'target', 'pause', 'profile', 'revoke', 'revoke_reconfirm'])
def test_changed_facts_invalidate_new_plans_and_late_output(plans, change):
    client, app, model, _ = plans
    save(client)
    value = review(client).json()
    draft = generate(client, intake_context_hash=value['context_hash']).json()
    def mutate():
        if change == 'record': client.post('/api/meals', json=meal(kcal_per_100g=10, source='Synthetic label'))
        elif change == 'target': save(client, kcal=2200)
        elif change == 'pause': save(client, kcal=None, source='')
        elif change == 'profile': client.put('/api/profile', json={'weight_kg': 80})
        else:
            revoke(client)
            if change == 'revoke_reconfirm': review(client)
    model.on_call = mutate
    response = generate(client, intake_context_hash=value['context_hash'])
    assert response.status_code == 409, response.text
    assert client.get(f'/api/meal-plans?day={DAY}').json()[0]['stale']
    assert accept(client, draft).status_code == 409
    with app.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM meal_plans WHERE status='failed'").fetchone()[0] == 1


def test_receipt_reuse_revoke_old_retry_and_restart(plans):
    client, app, _, _ = plans
    save(client)
    body = review_body(client)
    first = client.put(URL, json=body).json()
    assert client.put(URL, json=body).json() == first
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD})
        assert context(restarted) == first
    assert revoke(client).json()['version'] == 2
    assert revoke(client).json()['version'] == 2
    assert client.put(URL, json=body).status_code == 409
    renewed = review(client).json()
    assert renewed['version'] == 3
    assert revoke(client, version=1).status_code == 409
    assert context(client)['status'] == 'ready'


def test_review_conflict_and_private_isolation(plans):
    client, app, _, _ = plans
    save(client)
    body = review_body(client)
    client.post('/api/meals', json=meal(kcal_per_100g=50, source='Synthetic label'))
    assert client.put(URL, json=body).status_code == 409
    assert client.put(URL, json=review_body(client), headers={'Origin': 'https://evil.example'}).status_code == 403
    reviewed = review(client).json()
    with TestClient(app) as other:
        assert other.get(f'{URL}?day={DAY}').status_code == 401
        assert other.put(URL, json=body).status_code == 401
        assert other.post(URL + '/revoke', json={'day':DAY,'version':1}).status_code == 401
        register(other, 'bob')
        save(other)
        assert other.put(URL, json=body).status_code == 409
        assert context(other)['record_state'] is None
        assert generate(other, intake_context_hash=reviewed['context_hash']).status_code == 409
        assert review(other).status_code == 200
        revoke(other)
        assert context(client)['status'] == 'ready'


def test_concurrent_same_review_is_idempotent(plans):
    client, app, _, _ = plans
    save(client)
    user = client.get('/api/auth/me').json()
    service = MealIntakeService(app.state.database, user['id'])
    body = MealIntakeReview(**review_body(client))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.confirm(body), range(2)))
    assert results[0] == results[1] and results[0]['version'] == 1


def test_multi_meal_context_and_replacement_reuses_confirmation(plans):
    from test_coach import conversation, send
    from test_meal_schedule import generate as generate_meal
    from test_coach_meal_adjustments import INITIAL, output
    client, _, model, _ = plans
    save(client)
    value = review(client).json()
    data = send(client, conversation(client), '午饭和晚饭一起安排').json()
    model.output = output(INITIAL)
    lunch = generate_meal(client, data, 'lunch', intake_context_hash=value['context_hash']).json()
    dinner = generate_meal(client, data, 'dinner', intake_context_hash=value['context_hash']).json()
    assert lunch['intake_reference']['requested_meals'] == ['lunch', 'dinner']
    changed = send(client, data, '晚饭的米饭换成玉米').json()
    model.output = output([{'food_id':'corn','lower':120,'upper':140}, *INITIAL[1:]])
    new_dinner = generate_meal(client, changed, 'dinner', intake_context_hash=value['context_hash']).json()
    assert new_dinner['items'][1:] == dinner['items'][1:]
    assert accept(client, lunch).status_code == 200
    assert context(client) == value


def test_adjusted_target_review_expiry_blocks_context(plans):
    from test_energy_adjustments import proposed, confirmed_body
    client, _, _, _ = plans
    client.put('/api/profile', json={'height_cm':175,'weight_kg':70})
    value = proposed(client).json()
    assert client.post('/api/intake-target/confirm-adjustment', json=confirmed_body(value)).status_code == 200
    assert review(client).status_code == 200
    assert context(client, '2026-09-29')['status'] == 'review_due'


def test_legacy_request_payload_and_fingerprint_compatible(plans):
    client, app, model, _ = plans
    original = request()
    plan = client.post('/api/meal-plans', json=original).json()
    with app.state.database.connect() as c:
        stored = json.loads(c.execute('SELECT input_payload FROM meal_plans WHERE id=?', (plan['id'],)).fetchone()[0])
        assert 'intake_context_hash' not in stored
    save(client)
    review(client)
    assert client.post('/api/meal-plans', json=original).json()['id'] == plan['id']
    assert accept(client, plan).status_code == 200 and len(model.messages) == 1


def test_v11_backup_upgrade_and_restart_preserve_original_tables(tmp_path):
    path = tmp_path / 'migration.sqlite3'
    db = Database(path)
    db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE meal_intake_reviews')
        c.execute('DROP TABLE body_measurements')
        c.execute('DROP TABLE guest_accounts')
        c.execute('DROP TABLE guest_usage')
        c.execute('PRAGMA user_version=11')
        c.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'synthetic','synthetic')")
        c.execute("INSERT INTO profiles(user_id,payload) VALUES (1,'{}')")
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    db.initialize()
    db.initialize()
    with db.connect() as c, sqlite3.connect(str(path) + '.pre-v12.bak') as backup:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 14
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 11
        assert len(tables) == 16
        for table in tables:
            assert [tuple(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')] == backup.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
        assert c.execute('SELECT COUNT(*) FROM meal_intake_reviews').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
