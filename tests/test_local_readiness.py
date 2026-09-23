import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from database import Database
from schemas import BodyMeasurementCreate
from scripts.preflight import check
from scripts.rehearse_recovery import rehearse
from security import COOKIE_NAME
from services import business_time
from services.access_guard import AccessGuard, WINDOW
from services.backups import create_backup, manifest_path, restore_backup, verify_backup
from services.training_context import merge_training, training_command
from services.training_recommendations import equipment
from services.usage import reserve_call
from test_foundation import application, client, register, meal, workout, PASSWORD


def test_consistent_wal_backup_and_restore_relogin_data_and_quota(client, application, tmp_path):
    user = register(client)
    token = client.cookies.get(COOKIE_NAME)
    client.put('/api/profile', json={'display_name': 'Synthetic only', 'weight_kg': 70})
    food = meal(name='Synthetic eggs')
    assert client.post('/api/meals', json=food).status_code == 201
    assert client.post('/api/workouts', json=workout()).status_code == 201
    reserve_call(application.state.database, application.state.settings, user['id'], count=3)
    backup, restored = tmp_path / 'backup.sqlite3', tmp_path / 'restored.sqlite3'
    with application.state.database.connect() as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE profiles SET payload='{}'")
        create_backup(application.state.database.path, backup)
        writer.rollback()
    metadata = verify_backup(backup)
    assert metadata['tables'] == 18
    assert 'Synthetic' not in manifest_path(backup).read_text()
    result = restore_backup(backup, restored)
    assert result['sessions_revoked'] and result['usage_preserved']
    with TestClient(create_app(Settings(database_path=restored))) as recovered:
        recovered.cookies.set(COOKIE_NAME, token)
        assert recovered.get('/api/profile').status_code == 401
        recovered.cookies.clear()
        assert recovered.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 200
        assert recovered.get('/api/profile').json()['display_name'] == 'Synthetic only'
        assert recovered.get('/api/usage').json()['used'] == 3
        assert recovered.post('/api/meals', json=food).status_code == 201
        assert len(recovered.get('/api/meals?day=' + food['day']).json()) == 1
        assert len(recovered.get('/api/workouts?day=' + food['day']).json()) == 1
        register(recovered, 'other')
        assert recovered.get('/api/meals?day=' + food['day']).json() == []
    assert client.get('/api/auth/me').status_code == 200
    assert client.get('/api/usage').json()['used'] == 3


def test_restore_preserves_all_non_session_tables(client, application, tmp_path):
    register(client)
    backup, restored = tmp_path / 'copy.sqlite3', tmp_path / 'recovered.sqlite3'
    create_backup(application.state.database.path, backup)
    restore_backup(backup, restored)
    with closing(sqlite3.connect(backup)) as before, closing(sqlite3.connect(restored)) as after:
        names = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for name in names:
            if name not in ('sessions', 'meal_consents'):
                assert before.execute(f'SELECT * FROM "{name}"').fetchall() == after.execute(f'SELECT * FROM "{name}"').fetchall()
        assert after.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_backup_existing_path_or_sidecar_never_overwritten(client, application, tmp_path):
    target = tmp_path / 'existing.sqlite3'
    target.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):
        create_backup(application.state.database.path, target)
    assert target.read_bytes() == b'preserve'
    with pytest.raises(ValueError):
        create_backup(application.state.database.path, application.state.database.path)
    target = tmp_path / 'sidecar.sqlite3'
    target.with_name(target.name + '-wal').write_bytes(b'preserve')
    with pytest.raises(ValueError):
        create_backup(application.state.database.path, target)
    assert not target.exists()


def test_corrupt_unknown_and_tampered_backups_rejected(client, application, tmp_path):
    corrupt = tmp_path / 'broken.sqlite3'
    corrupt.write_bytes(b'not sqlite')
    target = tmp_path / 'unused.sqlite3'
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(corrupt, target)
    assert not target.exists()
    with closing(sqlite3.connect(corrupt := tmp_path / 'unknown.sqlite3')) as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError):
        create_backup(corrupt, target)
    create_backup(application.state.database.path, target)
    with target.open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ValueError, match='checksum'):
        restore_backup(target, tmp_path / 'restore.sqlite3')
    assert not (tmp_path / 'restore.sqlite3').exists()


def test_restore_refuses_existing_destination(client, application, tmp_path):
    backup = tmp_path / 'copy.sqlite3'
    create_backup(application.state.database.path, backup)
    with pytest.raises((FileExistsError, ValueError)):
        restore_backup(backup, application.state.database.path)
    assert client.get('/api/health').status_code == 200


def test_recovery_rehearsal_reuses_no_existing_directory(client, application, tmp_path):
    register(client)
    directory = tmp_path / 'drill'
    result = rehearse(application.state.database.path, directory)
    assert result['passed'] and result['non_session_tables_preserved'] == 16
    assert not result['formal_database_replaced']
    with pytest.raises(FileExistsError):
        rehearse(application.state.database.path, directory)


@pytest.mark.parametrize('metadata', [[], {}, {'created_at': None}, {'created_at': '2026-09-20'}])
def test_malformed_backup_manifest_is_rejected(client, application, tmp_path, metadata):
    path = tmp_path / 'copy.sqlite3'
    valid = create_backup(application.state.database.path, path)
    altered = {**valid, **metadata} if isinstance(metadata, dict) and metadata else metadata
    manifest_path(path).write_text(json.dumps(altered), encoding='utf-8')
    with pytest.raises(ValueError):
        verify_backup(path)


def test_static_directory_cannot_hold_private_database_or_backup(client, application, monkeypatch, tmp_path):
    import services.backups as backup_module
    import app as app_module
    monkeypatch.setattr(backup_module, 'ROOT', tmp_path)
    monkeypatch.setattr(app_module, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        create_backup(application.state.database.path, tmp_path / 'static/secret.sqlite3')
    with pytest.raises(ValueError):
        create_app(Settings(database_path=tmp_path / 'static/private.sqlite3'))


def test_quota_endpoint_auth_isolation_readonly_and_no_secret(client, application):
    assert client.get('/api/usage').status_code == 401
    first = register(client)
    reserve_call(application.state.database, application.state.settings, first['id'], count=4)
    assert client.get('/api/usage').json()['used'] == 4
    with TestClient(application) as second:
        register(second, 'bob')
        for _ in range(3):
            body = second.get('/api/usage?user_id=' + str(first['id'])).json()
            assert body['used'] == 0 and body['remaining'] == 100
            assert 'user_id' not in body and 'global_used' not in body
    assert client.get('/api/usage').json()['used'] == 4


def test_registration_off_login_still_works(client, application):
    register(client)
    application.state.settings = replace(application.state.settings, registration_enabled=False)
    assert client.post('/api/auth/register', json={'username': 'bob', 'password': PASSWORD}).status_code == 403
    assert client.get('/api/health').json()['registration_enabled'] is False
    assert client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 200


def test_login_attempts_persist_and_expire(client, application, monkeypatch):
    register(client)
    for _ in range(19):
        assert client.post('/api/auth/login', json={'username': 'alice', 'password': 'wrong-password'}).status_code == 401
    response = client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD})
    assert response.status_code == 429 and int(response.headers['retry-after']) > 0
    guard = AccessGuard(application.state.database.path)
    guard.initialize()
    with pytest.raises(HTTPException):
        guard.reserve('different-ip', 'alice')
    import services.access_guard as module
    now = module.time.time()
    monkeypatch.setattr(module.time, 'time', lambda: now + WINDOW + 1)
    guard.reserve('testclient', 'alice')
    with closing(sqlite3.connect(guard.path)) as connection:
        rows = connection.execute('SELECT * FROM attempts').fetchall()
        assert len(rows) <= 2
        assert 'alice' not in repr(rows) and 'testclient' not in repr(rows)


def test_login_guard_parallel_limit(tmp_path):
    guard = AccessGuard(tmp_path / 'private.sqlite3')
    guard.initialize()
    def attempt(index):
        try:
            guard.reserve('test-ip', 'same_account')
            return True
        except HTTPException as error:
            assert error.status_code == 429
            return False
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(attempt, range(25))) == 20


@pytest.mark.parametrize('origin', ['https://testserver', 'http://evil.invalid', 'null', 'http://user@testserver', 'http://testserver:88'])
def test_origin_scheme_credentials_and_port_rejected(client, origin):
    assert client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}, headers={'origin': origin}).status_code == 403


def test_host_and_security_headers(client):
    assert client.get('/api/health', headers={'host': 'evil.invalid'}).status_code == 400
    result = client.get('/')
    assert "script-src 'self'" in result.headers['content-security-policy']
    assert "frame-ancestors 'none'" in result.headers['content-security-policy']
    assert client.get('/api/usage').headers['cache-control'] == 'no-store'
    assert client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}, headers={'origin': 'http://testserver:80'}).status_code == 401


@pytest.mark.parametrize('changes', [{'access_mode': 'bad'}, {'allowed_hosts': ('*',)}, {'access_mode': 'shared'},
    {'access_mode': 'shared', 'cookie_secure': True, 'public_origin': 'http://example.invalid'},
    {'access_mode': 'shared', 'cookie_secure': True, 'public_origin': 'https://example.invalid', 'allowed_hosts': ('example.invalid',)}])
def test_bad_shared_configuration_fails_before_start(tmp_path, changes):
    with pytest.raises(ValueError):
        create_app(Settings(database_path=tmp_path / 'private.sqlite3', **changes))
    assert not (tmp_path / 'private.sqlite3').exists()


def test_shared_configuration_preflight_does_not_deploy_or_expose_key(client, application, tmp_path):
    settings = replace(application.state.settings, access_mode='shared', public_origin='https://example.invalid',
        cookie_secure=True, registration_enabled=False, allowed_hosts=('example.invalid',), qwen_api_key='secret-never-print')
    settings.validate_access()
    backup = tmp_path / 'copy.sqlite3'
    create_backup(settings.database_path, backup)
    result = check(settings, for_sharing=True, backup=backup)
    assert result['checks_passed'] and result['deployment_performed'] is False and result['provider_calls_made'] == 0
    assert 'secret-never-print' not in json.dumps(result)
    assert not check(application.state.settings, for_sharing=True)['checks_passed']


def test_beijing_midnight_and_measurement_validation(monkeypatch):
    class Clock:
        value = datetime(2026, 9, 20, 15, 59, 59, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz):
            return cls.value.astimezone(tz)
    monkeypatch.setattr(business_time, 'datetime', Clock)
    assert business_time.business_today() == date(2026, 9, 20)
    with pytest.raises(ValueError):
        BodyMeasurementCreate(day='2026-09-21', weight_kg=70.0, client_id=uuid4())
    Clock.value = datetime(2026, 9, 20, 16, tzinfo=timezone.utc)
    assert business_time.business_today() == date(2026, 9, 21)
    assert BodyMeasurementCreate(day='2026-09-21', weight_kg=70.0, client_id=uuid4()).day == date(2026, 9, 21)


@pytest.mark.parametrize('description,expected', [('在家，只有可调节哑铃', {'floor', 'dumbbell'}),
    ('可调哑铃，平板卧推凳', {'floor', 'dumbbell', 'bench', 'seat'}),
    ('哑铃，上斜卧推凳', {'floor', 'dumbbell', 'incline-bench', 'seat'})])
def test_equipment_common_names(description, expected):
    assert equipment(description) == (expected, False)


def test_denied_capability_cannot_return_through_more_specific_device():
    found, unknown = equipment('哑铃，可调训练凳，没有训练凳')
    assert not unknown
    assert not found & {'seat', 'bench', 'incline-bench', 'backrest', 'adjustable-bench'}


def test_explicit_week_request_clears_old_single_focus_and_replacement():
    changed = merge_training({'focus': '胸', 'minutes': 45, 'split': 'ppl', 'replacements': {'db-bench': 'barbell-bench'}}, training_command('安排本周训练', continuing=True))
    assert changed == {'minutes': 45, 'split': 'ppl', 'kind': 'strength', 'weekly': True}
