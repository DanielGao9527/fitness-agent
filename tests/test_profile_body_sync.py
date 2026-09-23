import json
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import application, client, register
from test_nutrition_targets import setup, profile, preview, save_standard, state, day_body, request
from test_body_measurements import update_body

TODAY = date.today().isoformat()
FORMULA = {"mode": "calculated", "use_profile": True}


def configured(client):
    user = setup(client, age=30, equation_sex="male", activity="inactive")
    assert save_standard(client, FORMULA).status_code == 200
    return user


def measure(client, **changes):
    payload = {"client_id": str(uuid4()), "day": TODAY, "weight_kg": 80, **changes}
    result = client.post('/api/body-measurements', json=payload)
    assert result.status_code == 201, result.text
    return result.json()


def current(client):
    return client.get('/api/body-measurements', params={'day': TODAY, 'days': 30}).json()


@pytest.mark.parametrize('change', [{'age': True}, {'age': '30'}, {'age': 18}, {'age': 101},
                                    {'age': 30.5}, {'equation_sex': 'unknown'}, {'activity': 'unknown'}])
def test_strict_profile_energy_fields(client, change):
    register(client)
    assert client.put('/api/profile', json=change).status_code == 422


def test_profile_formula_missing_fields_not_guessed_or_paid(client, application):
    setup(client)
    result = preview(client, FORMULA)
    assert result.status_code == 409 and '个人档案' in result.text
    with application.state.database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intake_targets').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM ai_usage').fetchone()[0] == 0


def test_profile_formula_ignores_forged_inputs_and_tracks_saved_profile(client):
    configured(client)
    old = state(client)
    spoof = {**FORMULA, 'inputs': {'age': 90, 'equation_sex': 'female', 'activity': 'active', 'general_adult': True}}
    assert preview(client, spoof).json()['calculation']['kcal'] == old['target']['kcal']
    for changes in ({'age': 50}, {'age': 50, 'equation_sex': 'female'},
                    {'age': 50, 'equation_sex': 'female', 'activity': 'active'}):
        profile(client, **changes)
        updated = state(client)
        assert updated['status'] == 'active'
        assert updated['target']['kcal'] != old['target']['kcal']
        old = updated
    profile(client, age=None)
    assert state(client)['status'] == 'needs_input'
    profile(client, age=50)
    assert state(client)['status'] == 'active'


def test_old_clients_preserve_energy_fields_and_explicit_clear_is_honored(client):
    configured(client)
    assert client.put('/api/profile', json={'height_cm': 175, 'weight_kg': 70}).json()['age'] == 30
    assert state(client)['status'] == 'active'
    assert client.put('/api/profile', json={'height_cm': 175, 'weight_kg': 70, 'age': None}).json()['age'] is None
    assert state(client)['status'] == 'needs_input'


def test_legacy_profile_reads_confirmed_inputs_without_writing(client, application):
    user = setup(client)
    save_standard(client)
    with application.state.database.connect() as c:
        raw = json.loads(c.execute('SELECT payload FROM profiles WHERE user_id=?', (user['id'],)).fetchone()[0])
        for key in ('age', 'equation_sex', 'activity'):
            raw.pop(key, None)
        payload = json.dumps(raw)
        c.execute('UPDATE profiles SET payload=? WHERE user_id=?', (payload, user['id']))
    result = client.get('/api/profile').json()
    assert result['age'] == 30 and result['equation_sex'] == 'male' and result['activity'] == 'inactive'
    assert not any(key.startswith('_') for key in result)
    assert state(client)['profile_inputs']['age'] == 30
    with application.state.database.connect() as c:
        assert c.execute('SELECT payload FROM profiles WHERE user_id=?', (user['id'],)).fetchone()[0] == payload
        assert c.execute('SELECT COUNT(*) FROM intake_targets').fetchone()[0] == 1
    assert client.put('/api/profile', json={**result, 'age': None}).status_code == 200
    assert state(client)['status'] == 'needs_input'


def test_legacy_fixed_preserved_until_formula_confirmation(client):
    setup(client, age=30, equation_sex='male', activity='inactive')
    save_standard(client, {'mode': 'fixed', 'fixed_kcal': 3200})
    profile(client, age=40)
    assert state(client)['target']['kcal'] == 3200
    result = preview(client, FORMULA)
    assert result.status_code == 200
    assert state(client)['target']['kcal'] == 3200
    assert save_standard(client, FORMULA).json()['target']['kcal'] != 3200


def test_latest_measurement_syncs_but_older_backfill_does_not(client, application):
    configured(client)
    before = state(client)
    record = measure(client, body_fat_percent=24)
    after = state(client)
    assert after['target']['kcal'] > before['target']['kcal']
    assert client.get('/api/profile').json()['weight_kg'] == 80
    assert client.get('/api/profile').json()['body_fat_percent'] == 24
    measure(client, day=(date.today()-timedelta(days=7)).isoformat(), weight_kg=85, body_fat_percent=25)
    assert client.get('/api/profile').json()['weight_kg'] == 80
    assert state(client)['version'] == after['version']
    data = current(client)
    assert data['current_body']['weight_kg'] == {'value': 80, 'source': 'measurement', 'day': TODAY, 'record_id': record['id']}
    assert data['review']['measurement_days'] == 2
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get('/api/profile').json()['weight_kg'] == 80
        assert state(restarted)['target']['kcal'] == after['target']['kcal']
        restarted.cookies.clear()
        register(restarted, 'bob')
        assert current(restarted)['current_body']['weight_kg']['value'] is None
        assert state(restarted)['status'] == 'unset'


def test_partial_measurements_independent_and_no_daily_fake_rows(client):
    configured(client)
    profile(client, body_fat_percent=22)
    first = state(client)
    record = measure(client, weight_kg=80)
    after = state(client)
    assert client.get('/api/profile').json()['body_fat_percent'] == 22
    changed = client.put(f"/api/body-measurements/{record['id']}", json=update_body(record, body_fat_percent=23)).json()
    assert changed['body_fat_percent'] == 23
    assert state(client)['version'] == after['version']
    assert after['version'] == first['version'] + 1
    assert current(client)['review']['measurement_days'] == 1
    assert current(client)['metrics']['body_fat_percent']['count'] == 1
    assert current(client)['metrics']['body_fat_percent']['change'] is None
    future = client.get('/api/body-measurements', params={'day': (date.today()+timedelta(days=30)).isoformat()}).json()
    assert future['current_body']['weight_kg']['value'] == 80
    assert future['records'] == []


def test_manual_newer_value_and_replay_do_not_revert_current_profile(client):
    configured(client)
    client_id = str(uuid4())
    record = measure(client, client_id=client_id)
    profile(client, weight_kg=82, body_fat_percent=21)
    request = {**{key: value for key, value in record.items() if key not in ('id', 'version')}, 'client_id': client_id}
    assert client.post('/api/body-measurements', json=request).status_code == 201
    assert client.get('/api/profile').json()['weight_kg'] == 82
    assert current(client)['current_body']['weight_kg']['source'] == 'profile'
    change = update_body(record, weight_kg=81)
    assert client.put(f"/api/body-measurements/{record['id']}", json=change).status_code == 200
    assert client.get('/api/profile').json()['weight_kg'] == 81
    profile(client, weight_kg=83)
    version = state(client)['version']
    assert client.put(f"/api/body-measurements/{record['id']}", json=change).status_code == 200
    assert client.get('/api/profile').json()['weight_kg'] == 83
    assert state(client)['version'] == version


def test_delete_latest_restores_manual_fallback_and_day_override_survives(client):
    configured(client)
    baseline = state(client)['target']['kcal']
    assert client.post('/api/nutrition-target/day', json=day_body(client, 3500)).status_code == 200
    record = measure(client)
    assert state(client)['target']['kcal'] == 3500
    tomorrow = (date.today()+timedelta(days=1)).isoformat()
    assert state(client, tomorrow)['target']['kcal'] > baseline
    assert client.delete(f"/api/body-measurements/{record['id']}?version=1").status_code == 204
    assert client.get('/api/profile').json()['weight_kg'] == 70
    assert state(client)['target']['kcal'] == 3500
    assert state(client, tomorrow)['target']['kcal'] == baseline
    assert current(client)['records'] == []


def test_delete_restores_previous_measurement_without_manual_baseline(client):
    register(client)
    old = measure(client, day=(date.today()-timedelta(days=7)).isoformat(), weight_kg=75, body_fat_percent=22)
    latest = measure(client, weight_kg=74)
    assert client.get('/api/profile').json()['body_fat_percent'] == 22
    assert client.delete(f"/api/body-measurements/{latest['id']}?version=1").status_code == 204
    assert client.get('/api/profile').json()['weight_kg'] == 75
    assert client.delete(f"/api/body-measurements/{old['id']}?version=1").status_code == 204
    assert client.get('/api/profile').json()['weight_kg'] is None


def test_measurement_changes_future_formula_not_past_ledger(client, monkeypatch):
    import services.nutrition_targets as module
    configured(client)
    first = state(client)
    monkeypatch.setattr(module, 'business_today', lambda: date.today() + timedelta(days=1))
    measure(client)
    assert state(client)['target']['id'] == first['target']['id']
    assert state(client, module.business_today().isoformat())['target']['kcal'] > first['target']['kcal']


def test_measurement_invalidates_pending_confirmation_and_out_of_scope_not_guessed(client):
    configured(client)
    pending = request(client, FORMULA)
    record = measure(client)
    assert client.post('/api/nutrition-target/standard', json=pending).status_code == 409
    client.put(f"/api/body-measurements/{record['id']}", json=update_body(record, weight_kg=30))
    assert client.get('/api/profile').json()['weight_kg'] == 30
    assert state(client)['status'] == 'needs_input'
    assert state(client)['target']['kcal'] is None
