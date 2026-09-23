import secrets
import sqlite3
import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from database import Database
from security import COOKIE_NAME
from services.backups import create_backup, fingerprints, restore_backup
from services.guests import GUEST_HEADER, IDLE_SECONDS, MAX_SECONDS, cleanup
from services.usage import reserve_call, usage_status
from scripts.preflight import check
from test_foundation import DAY, PASSWORD, application, client, meal, register, workout
from test_coach import conversation, send


def guest(client):
    client.headers[GUEST_HEADER] = secrets.token_hex(32)
    response = client.post('/api/auth/guest', json={})
    assert response.status_code == 201, response.text
    return response


def test_guest_session_cookie_tab_proof_and_manual_features(client, application):
    response = guest(client)
    user = response.json()
    assert user['is_guest'] and user['username'] == '游客'
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'Max-Age' not in response.headers['set-cookie']
    assert 'expires=' not in response.headers['set-cookie'].lower()
    assert client.get('/api/auth/me').json()['id'] == user['id']
    assert client.put('/api/profile', json={'weight_kg': 80, 'display_name': 'Temporary'}).status_code == 200
    assert client.post('/api/meals', json=meal()).status_code == 201
    assert client.post('/api/workouts', json=workout()).status_code == 201
    chat = conversation(client)
    assert send(client, chat, '今天练什么').status_code == 200
    draft = client.post('/api/meal-drafts', json={'client_id': str(uuid4()), 'day': DAY,
                                               'meal_type': 'breakfast', 'text': '两个鸡蛋'})
    assert draft.status_code == 201
    assert client.get(f'/api/summary?day={DAY}').status_code == 200
    key = client.headers.pop(GUEST_HEADER)
    assert client.get('/api/profile').status_code == 401
    client.headers[GUEST_HEADER] = '0' * 64
    assert client.get('/api/profile').status_code == 401
    client.headers[GUEST_HEADER] = key
    assert client.get('/api/profile').json()['weight_kg'] == 80
    with application.state.database.connect() as connection:
        stored = connection.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
        assert stored['password_hash'] == '!guest-no-password'
        assert stored['username'].startswith('~guest_')
        assert key not in repr(tuple(connection.execute('SELECT * FROM guest_accounts').fetchone()))


def test_guest_isolated_from_other_guests_and_permanent_accounts(client, application):
    first = guest(client).json()
    saved = client.post('/api/meals', json=meal()).json()
    with TestClient(application) as other:
        second = guest(other).json()
        assert second['id'] != first['id']
        assert other.get(f'/api/meals?day={DAY}').json() == []
        assert other.delete(f"/api/meals/{saved['id']}").status_code == 404
        assert other.post('/api/meals', json=meal(user_id=first['id'])).status_code == 422
        permanent = register(other)
        assert permanent['id'] > 0
        assert other.get(f'/api/meals?day={DAY}').json() == []
    assert len(client.get(f'/api/meals?day={DAY}').json()) == 1


def test_logout_deletes_all_guest_children_without_refunding_usage(client, application):
    user = guest(client).json()
    db, settings = application.state.database, application.state.settings
    assert client.put('/api/profile', json={'display_name': 'Not retained'}).status_code == 200
    assert client.post('/api/meals', json=meal()).status_code == 201
    assert client.post('/api/workouts', json=workout()).status_code == 201
    chat = conversation(client)
    assert send(client, chat, '今天练什么').status_code == 200
    reserve_call(db, settings, user['id'], count=3)
    token = client.cookies.get(COOKIE_NAME)
    assert client.post('/api/auth/logout', json={}).status_code == 204
    with db.connect() as connection:
        for table in ('profiles', 'meals', 'workouts', 'sessions', 'ai_usage', 'guest_accounts'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table} WHERE user_id=?', (user['id'],)).fetchone()[0] == 0
        assert not connection.execute('SELECT 1 FROM users WHERE id=?', (user['id'],)).fetchone()
        assert connection.execute('SELECT COUNT(*) FROM coach_conversations').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM coach_turns').fetchone()[0] == 0
        assert connection.execute('SELECT SUM(calls) FROM guest_usage').fetchone()[0] == 3
        assert not connection.execute('PRAGMA foreign_key_check').fetchone()
    client.cookies.set(COOKIE_NAME, token)
    assert client.get('/api/profile').status_code == 401
    client.cookies.clear()
    next_user = guest(client).json()
    assert next_user['id'] != user['id']
    assert client.get(f'/api/meals?day={DAY}').json() == []
    assert usage_status(db, settings, next_user['id'])['used'] == 3
    assert check(settings)['today_shared_attempts'] == 3
    with pytest.raises(HTTPException) as error:
        reserve_call(db, settings, user['id'])
    assert error.value.status_code == 401


def test_restart_refresh_and_expiry_cleanup_preserves_registered_user(client, application):
    regular = register(client)
    client.put('/api/profile', json={'display_name': 'Keep forever'})
    temporary = guest(client).json()
    client.post('/api/meals', json=meal())
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restarted.headers[GUEST_HEADER] = client.headers[GUEST_HEADER]
        assert len(restarted.get(f'/api/meals?day={DAY}').json()) == 1
        with application.state.database.connect() as connection:
            connection.execute('UPDATE guest_accounts SET expires_at=0 WHERE user_id=?', (temporary['id'],))
        assert restarted.get('/api/profile').status_code == 401
        assert cleanup(application.state.database) == 1
        assert restarted.post('/api/auth/login', json={'username': regular['username'], 'password': PASSWORD}).status_code == 200
        assert restarted.get('/api/profile').json()['display_name'] == 'Keep forever'


def test_inactivity_slides_but_never_beyond_one_day(client, application):
    user = guest(client).json()
    now = int(time.time())
    with application.state.database.connect() as connection:
        connection.execute('UPDATE guest_accounts SET created_at=?,expires_at=? WHERE user_id=?',
                           (now - MAX_SECONDS + 100, now + 10, user['id']))
    body = client.get('/api/auth/me').json()
    assert body['expires_at'] == now + 100
    assert body['max_expires_at'] == now + 100


def test_only_seven_utc_dates_of_aggregate_usage_are_retained(client, application):
    today = datetime.now(timezone.utc).date()
    with application.state.database.connect() as connection:
        for age in (0, 6, 7):
            connection.execute('INSERT INTO guest_usage VALUES (?,?,1)',
                               ((today - timedelta(days=age)).isoformat(), 'synthetic-network-hash'))
    cleanup(application.state.database)
    with application.state.database.connect() as connection:
        days = {row[0] for row in connection.execute('SELECT day FROM guest_usage')}
    assert days == {today.isoformat(), (today - timedelta(days=6)).isoformat()}


@pytest.mark.parametrize('header', ['', 'bad', 'x' * 65, 'G' * 64])
def test_invalid_guest_proof_and_body_do_not_create_accounts(client, application, header):
    assert client.post('/api/auth/guest', json={}, headers={GUEST_HEADER: header}).status_code == 422
    assert client.post('/api/auth/guest', json={'user_id': 1}, headers={GUEST_HEADER: 'a' * 64}).status_code == 422
    with application.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0


def test_guest_switch_and_origin_guards(client, application):
    settings = application.state.settings
    application.state.settings = replace(settings, guest_enabled=False)
    assert client.get('/api/health').json()['guest_enabled'] is False
    assert client.post('/api/auth/guest', json={}, headers={GUEST_HEADER: 'a' * 64}).status_code == 403
    application.state.settings = settings
    assert client.post('/api/auth/guest', json={}, headers={GUEST_HEADER: 'a' * 64, 'origin': 'https://evil.invalid'}).status_code == 403
    guest(client)
    application.state.settings = replace(settings, guest_enabled=False)
    assert client.get('/api/profile').status_code == 401


def test_network_creation_throttle_survives_new_guest_and_restart(client, application):
    for _ in range(10):
        guest(client)
    assert client.post('/api/auth/guest', json={}).status_code == 429
    with TestClient(create_app(application.state.settings)) as restarted:
        response = restarted.post('/api/auth/guest', json={}, headers={GUEST_HEADER: 'b' * 64})
        assert response.status_code == 429 and 'retry-after' in response.headers
    assert client.get('/api/profile').status_code == 200


def test_guest_quota_shared_by_network_and_atomic_with_site_limit(client, application):
    settings = replace(application.state.settings, guest_ai_daily_limit=5, ai_global_daily_limit=6)
    application.state.settings = settings
    user = guest(client).json()
    db = application.state.database
    def call(_):
        try:
            reserve_call(db, settings, user['id'])
            return True
        except HTTPException as error:
            assert error.status_code == 429
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(call, range(9))) == 5
    next_guest = guest(client).json()
    status = usage_status(db, settings, next_guest['id'])
    assert status['used'] == 5 and status['remaining'] == 0 and status['scope'] == 'guest_network'
    with pytest.raises(HTTPException):
        reserve_call(db, settings, next_guest['id'])
    regular = register(client)
    reserve_call(db, settings, regular['id'])
    with pytest.raises(HTTPException):
        reserve_call(db, settings, regular['id'])
    assert not usage_status(db, settings, regular['id'])['shared_available']


def test_shared_https_registration_and_login(tmp_path):
    settings = Settings(database_path=tmp_path/'shared.sqlite3', access_mode='shared', cookie_secure=True,
                        public_origin='https://fitness.invalid', allowed_hosts=('fitness.invalid',))
    with TestClient(create_app(settings), base_url='https://fitness.invalid') as client:
        health = client.get('/api/health').json()
        assert health['registration_enabled'] and health['guest_enabled']
        response = client.post('/api/auth/register', json={'username': 'signup', 'password': PASSWORD})
        assert response.status_code == 201 and 'Secure' in response.headers['set-cookie']
        client.post('/api/auth/logout', json={})
        assert client.post('/api/auth/login', json={'username': 'signup', 'password': PASSWORD}).status_code == 200
        assert client.get('/docs').status_code == 404


def test_v13_migration_backup_and_regular_fingerprints(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    db = Database(path)
    db.initialize()
    with db.connect() as connection:
        connection.executescript('DROP TABLE guest_accounts; DROP TABLE guest_usage; PRAGMA user_version=13;')
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'legacy','not-real')")
        connection.execute("INSERT INTO ai_usage VALUES ('2026-09-23',1,7)")
    before = fingerprints(path)
    db.initialize()
    after = fingerprints(path)
    assert all(before[name] == after[name] for name in before)
    assert path.with_name(path.name+'.pre-v14.bak').exists()
    db.initialize()
    assert fingerprints(path) == after
    snapshot, restored = tmp_path/'snapshot.sqlite3', tmp_path/'restored.sqlite3'
    assert create_backup(path, snapshot)['schema'] == 14
    assert restore_backup(snapshot, restored)['usage_preserved']
    assert fingerprints(restored)['ai_usage'] == after['ai_usage']
