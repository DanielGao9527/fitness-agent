import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from schemas import BodyMeasurementCreate, BodyMeasurementUpdate
from services.body_measurements import BodyMeasurementService
from test_foundation import DAY, application, client, register, meal, workout


def body(**changes):
    return {"client_id": str(uuid4()), "day": DAY, "weight_kg": 72.5, **changes}


def review(client, day=DAY, days=30):
    response = client.get('/api/body-measurements', params={"day": day, "days": days})
    assert response.status_code == 200, response.text
    return response.json()


def update_body(record, **changes):
    return {key: value for key, value in {**record, **changes}.items() if key != 'id'}


def test_authentication_same_origin_and_no_model(client):
    assert client.get('/api/body-measurements', params={'day': DAY}).status_code == 401
    assert client.post('/api/body-measurements', json=body()).status_code == 401
    register(client)
    assert client.post('/api/body-measurements', json=body(), headers={'Origin': 'https://elsewhere.invalid'}).status_code == 403
    assert client.post('/api/body-measurements', json=body(user_id=99)).status_code == 422
    assert client.post('/api/body-measurements', json=body()).status_code == 201
    assert review(client)['metrics']['weight_kg']['count'] == 1


@pytest.mark.parametrize('change', [
    {'weight_kg': None}, {'weight_kg': 0}, {'weight_kg': 401}, {'weight_kg': '72'},
    {'weight_kg': True}, {'weight_kg': False}, {'body_fat_percent': 76}, {'body_fat_percent': 0},
    {'body_fat_percent': True}, {'notes': 'a' * 301}, {'day': '2026-02-30'},
    {'day': (date.today() + timedelta(days=1)).isoformat()}, {'unexpected': 'private'},
])
def test_strict_validation(client, change):
    register(client)
    assert client.post('/api/body-measurements', json=body(**change)).status_code == 422
    assert review(client)['records'] == []


def test_empty_partial_and_date_boundaries(client):
    register(client)
    empty = review(client)
    assert empty['metrics']['weight_kg'] == {'count': 0, 'first': None, 'last': None, 'change': None}
    assert empty['review']['nutrition']['kcal']['known_total'] is None
    assert review(client, '0001-01-01')['days'] == 1
    assert review(client, '9999-12-31', 90)['days'] == 90
    for params in ({'day': DAY, 'days': 31}, {'day': 'invalid'}, {'day': DAY, 'days': 999}):
        assert client.get('/api/body-measurements', params=params).status_code == 422
    assert client.post('/api/body-measurements', json=body(weight_kg=None, body_fat_percent=23)).status_code == 201
    result = review(client)
    assert result['metrics']['weight_kg']['count'] == 0
    assert result['metrics']['body_fat_percent']['change'] is None


def test_create_replay_same_day_conflicts_update_version_and_delete_tombstone(client):
    register(client)
    payload = body(notes='Synthetic measurement')
    record = client.post('/api/body-measurements', json=payload).json()
    assert client.post('/api/body-measurements', json=payload).json() == record
    assert client.post('/api/body-measurements', json={**payload, 'weight_kg': 75}).status_code == 409
    assert client.post('/api/body-measurements', json=body()).status_code == 409
    change = update_body(record, weight_kg=71.5)
    updated = client.put(f"/api/body-measurements/{record['id']}", json=change).json()
    assert updated['version'] == 2 and updated['weight_kg'] == 71.5
    assert client.put(f"/api/body-measurements/{record['id']}", json=change).json() == updated
    assert client.put(f"/api/body-measurements/{record['id']}", json=update_body(record, weight_kg=70)).status_code == 409
    assert client.post('/api/body-measurements', json=payload).status_code == 409
    assert client.delete(f"/api/body-measurements/{record['id']}?version=1").status_code == 409
    assert client.delete(f"/api/body-measurements/{record['id']}?version=2").status_code == 204
    assert client.delete(f"/api/body-measurements/{record['id']}?version=2").status_code == 204
    assert client.put(f"/api/body-measurements/{record['id']}", json=change).status_code == 404
    assert client.post('/api/body-measurements', json=payload).status_code == 409
    assert review(client)['records'] == []
    replacement = client.post('/api/body-measurements', json=body(weight_kg=70)).json()
    assert replacement['id'] != record['id']
    assert client.delete(f"/api/body-measurements/{record['id']}?version=2").status_code == 204
    assert review(client)['records'] == [replacement]


def test_move_day_collision_is_atomic(client):
    register(client)
    record = client.post('/api/body-measurements', json=body()).json()
    client.post('/api/body-measurements', json=body(day='2026-09-14'))
    assert client.put(f"/api/body-measurements/{record['id']}", json=update_body(record, day='2026-09-14')).status_code == 409
    assert review(client)['records'][-1] == record
    moved = client.put(f"/api/body-measurements/{record['id']}", json=update_body(record, day='2026-08-10')).json()
    assert moved['day'] == '2026-08-10'
    assert len(review(client)['records']) == 1
    assert len(review(client, days=90)['records']) == 2


def test_metric_dates_independent_missing_not_zero_and_profile_unchanged(client, application):
    register(client)
    profile = client.put('/api/profile', json={'weight_kg': 80, 'body_fat_percent': 27}).json()
    for day, weight, fat in [('2026-09-01', 72.5, None), ('2026-09-03', None, 23.4),
                             ('2026-09-05', 71.2, None), ('2026-09-08', None, 22.9)]:
        assert client.post('/api/body-measurements', json=body(day=day, weight_kg=weight, body_fat_percent=fat)).status_code == 201
    result = review(client)
    assert result['metrics']['weight_kg']['change'] == -1.3
    assert result['metrics']['body_fat_percent']['change'] == -0.5
    assert result['metrics']['body_fat_percent']['first']['day'] == '2026-09-03'
    assert result['review']['measurement_days'] == 4 and result['profile_sync'] is True
    assert client.get('/api/profile').json() == profile
    with application.state.database.connect() as c:
        for table in ('ai_usage', 'intake_targets', 'meals', 'workouts'):
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_stage_review_only_actual_records_known_estimated_unknown(client, application):
    user = register(client)
    client.post('/api/meals', json=meal(kcal_per_100g=60, source='Synthetic label'))
    client.post('/api/meals', json=meal(name='Unknown'))
    client.post('/api/meals', json=meal(day='2026-07-01', name='Outside period'))
    client.post('/api/workouts', json=workout(status='completed'))
    client.post('/api/workouts', json=workout(day='2026-09-14', status='planned'))
    client.post('/api/workouts', json=workout(day='2026-07-01', status='completed'))
    # A stored synthetic estimate exercises the same aggregation as daily records.
    estimated_meal = {**meal(day='2026-09-14', grams=None), 'nutrition_estimate':
        {key: {'lower': 10, 'upper': 20} for key in ('kcal', 'protein', 'carbs', 'fat')}}
    for key in ('kcal', 'protein', 'carbs', 'fat'):
        estimated_meal[f'{key}_per_100g'] = None
    with application.state.database.connect() as c:
        c.execute('INSERT INTO meals(user_id,client_id,day,payload) VALUES(?,?,?,?)',
                  (user['id'], str(uuid4()), estimated_meal['day'], json.dumps(estimated_meal)))
    result = review(client)['review']
    assert result['meal_days'] == 2 and result['meal_count'] == 3
    assert result['nutrition']['kcal']['known_total'] == 150
    assert result['estimated_nutrition']['kcal'] == {'count': 1, 'unknown_count': 1, 'lower_total': 10, 'upper_total': 20}
    assert result['completed_workout_days'] == 1 and result['completed_minutes'] == 30
    assert result['planned_count'] == 1 and result['estimated_workout_calories']['unknown_count'] == 1


def test_isolation_and_restart(client, application):
    register(client)
    record = client.post('/api/body-measurements', json=body(notes='Private synthetic note')).json()
    initial = review(client)
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert review(restarted) == initial
        restarted.cookies.clear()
        register(restarted, 'bob')
        assert review(restarted)['records'] == []
        assert restarted.put(f"/api/body-measurements/{record['id']}", json=update_body(record)).status_code == 404
        assert restarted.delete(f"/api/body-measurements/{record['id']}?version=1").status_code == 404
        assert restarted.post('/api/body-measurements', json=body()).status_code == 201
    assert review(client) == initial


def test_concurrent_create_and_update(client, application):
    user = register(client)
    service = BodyMeasurementService(application.state.database, user['id'])
    payload = BodyMeasurementCreate(**body())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.create(payload), range(2)))
    assert results[0] == results[1]
    def change(weight):
        try:
            service.update(results[0]['id'], BodyMeasurementUpdate(**update_body(results[0], weight_kg=weight)))
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, [70, 71])) == [200, 409]


def test_v12_upgrade_backup_and_original_seventeen_tables(tmp_path):
    path = tmp_path / 'upgrade.sqlite3'
    db = Database(path)
    db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE body_measurements')
        c.execute('DROP TABLE guest_accounts')
        c.execute('DROP TABLE guest_usage')
        c.execute('PRAGMA user_version=12')
        c.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'synthetic','synthetic')")
        c.execute("INSERT INTO profiles(user_id,payload) VALUES(1,'{}')")
        tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    db.initialize()
    db.initialize()
    with db.connect() as c, sqlite3.connect(str(path) + '.pre-v13.bak') as backup:
        assert len(tables) == 17
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 12
        assert c.execute('PRAGMA user_version').fetchone()[0] == 14
        for table in tables:
            assert [tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')] == backup.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
        assert c.execute('SELECT COUNT(*) FROM body_measurements').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
