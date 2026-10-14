from schema_fixtures import strip_governance_v19
"""Synthetic lifecycle tests, not a semantic model accuracy benchmark."""
from dataclasses import replace
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from scripts.migrate_memory import migrate
from services import business_time
from services.backups import fingerprints, verify_backup
from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_meal_plans import plans
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_training_recommendations import recommend, result


@pytest.fixture
def scoped(reviews, monkeypatch):
    client, app, model, _ = reviews
    clock = [date.fromisoformat(DAY)]
    monkeypatch.setattr(business_time, 'business_today', lambda: clock[0])
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    assert client.put('/api/profile', json={'equipment': '在家，徒手', 'experience': 'experienced',
        'minutes_per_session': 40}).status_code == 200
    return client, app, model, clock


def draft(scoped, message='我平时训练的地方只有哑铃', value='只有哑铃', scope='long_term', **changes):
    client, _, model, _ = scoped
    def edit(data, out):
        turn = data['messages'][-1]
        out.update(scope='strength', command='', equipment_memory={
            'source_version': turn['version'], 'source_text': turn['message'], 'value': value, 'scope': scope})
        out['equipment_memory'].update(changes)
    model.edit = edit
    data = send(client, conversation(client), message).json()
    response = client.post(f"{BASE}/{data['id']}/understand", json={
        'version': data['version'], 'client_id': str(uuid4()), 'auto_apply': True})
    return data, response


def accept(scoped, **kwargs):
    _, response = draft(scoped, **kwargs)
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['status'] == 'ready'
    response = confirm(scoped[0], ready)
    assert response.status_code == 200, response.text
    return response.json()


def advice(client, **changes):
    current = send(client, conversation(client, **changes), '今天练什么').json()
    return result(recommend(client, current))


@pytest.mark.parametrize('scope,message,cross_source,cross_value', [
    ('session', '这次在朋友家训练，只有哑铃', 'profile', '在家，徒手'),
    ('day', '今天在朋友家训练，只有哑铃', 'day', '只有哑铃'),
    ('long_term', '以后我训练的地方只有哑铃', 'profile', '只有哑铃'),
])
def test_confirmed_scope_controls_final_advice_and_survives_restart(scoped, scope, message, cross_source, cross_value):
    client, app, model, _ = scoped
    raw, response = draft(scoped, message=message, scope=scope)
    ready = response.json()
    assert response.status_code == 200, response.text
    assert ready['pending'] and client.get('/api/profile').json()['equipment'] == '在家，徒手'
    assert advice(client)['equipment'] == '在家，徒手'
    saved_response = confirm(client, ready)
    assert saved_response.status_code == 200, saved_response.text
    saved = saved_response.json()
    local = result(recommend(client, saved))
    assert local['status'] == 'ready' and local['equipment'] == '只有哑铃'
    assert 'floor-press' in [x['id'] for x in local['exercises']]
    final = advice(client)
    assert final['equipment'] == cross_value and final['equipment_source'] == cross_source
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert advice(restarted)['equipment'] == cross_value
    before = client.get(f"{BASE}/{raw['id']}").json()['context']['day_equipment']['revision']
    assert confirm(client, ready).status_code == 200
    assert client.get(f"{BASE}/{raw['id']}").json()['context']['day_equipment']['revision'] == before
    assert len(model.messages) == 1
    assert client.get('/api/workouts', params={'day': DAY}).json() == []


def test_unknown_scope_requires_human_selection(scoped):
    client, _, _, _ = scoped
    _, response = draft(scoped, message='我训练的地方只有哑铃', scope='long_term')
    ready = response.json()
    assert ready['review']['equipment_memory']['scope'] == 'unspecified'
    assert confirm(client, ready).status_code == 422
    assert confirm(client, ready, equipment_scope='forever').status_code == 422
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    assert confirm(client, ready, equipment_scope='session').status_code == 200
    assert advice(client)['equipment'] == '在家，徒手'


@pytest.mark.parametrize('scope,message', [('day', '今天我训练的地方只有哑铃'),
                                         ('session', '这次我训练的地方只有哑铃')])
def test_midnight_expires_conditions_and_cached_advice(scoped, scope, message):
    client, _, _, clock = scoped
    saved = accept(scoped, message=message, scope=scope)
    first = result(recommend(client, saved))
    assert first['equipment'] == '只有哑铃'
    clock[0] += timedelta(days=1)
    refreshed = client.get(f"{BASE}/{saved['id']}").json()
    assert 'equipment' not in refreshed['training_context']
    assert refreshed['turns'][-1]['response']['training_recommendation']['stale']
    assert recommend(client, saved, client_id=first['client_id']).status_code == 409
    assert advice(client, day=clock[0].isoformat())['equipment'] == '在家，徒手'


def test_midnight_during_model_or_before_confirm_rejects(scoped):
    client, _, model, clock = scoped
    model.on_call = lambda: clock.__setitem__(0, clock[0] + timedelta(days=1))
    _, response = draft(scoped, scope='session', message='这次我训练的地方只有哑铃')
    assert response.status_code == 409, response.text
    clock[0] = date.fromisoformat(DAY)
    model.on_call = None
    _, response = draft(scoped, scope='day', message='今天我训练的地方只有哑铃')
    ready = response.json()
    clock[0] += timedelta(days=1)
    assert confirm(client, ready).status_code == 409


def test_daily_replacement_aba_never_revives_draft_and_delete_does_not_restore_old(scoped):
    client, _, _, _ = scoped
    old = accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    current = send(client, conversation(client), '今天练什么').json()
    first = result(recommend(client, current))
    accept(scoped, scope='day', message='今天我训练的地方只能徒手', value='只能徒手')
    latest = accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    assert recommend(client, current, client_id=first['client_id']).status_code == 409
    assert client.request('DELETE', f"{BASE}/{latest['id']}", json={'version': latest['version']}).status_code == 204
    assert advice(client)['equipment'] == '在家，徒手'
    with scoped[1].state.database.connect() as connection:
        row = connection.execute('SELECT * FROM coach_day_equipment').fetchone()
        assert row['value'] == '' and row['revision'] == 4 and row['conversation_id'] is None
    assert client.get(f"{BASE}/{old['id']}").json()['training_context'].get('equipment') is None


def test_day_change_while_model_runs_and_before_confirmation_rejects(scoped):
    client, app, model, _ = scoped
    saved = accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    def revoke():
        assert client.request('DELETE', f"{BASE}/{saved['id']}", json={'version': saved['version']}).status_code == 204
    model.on_call = revoke
    _, response = draft(scoped)
    assert response.status_code == 409
    model.on_call = None
    _, response = draft(scoped)
    ready = response.json()
    accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    assert confirm(client, ready).status_code == 409


def test_profile_correction_invalidates_day_and_semantic_history(scoped):
    client, _, _, _ = scoped
    durable = accept(scoped)
    hits = client.post(f'{BASE}/memory/search', json={'query': '哑铃'}).json()['items']
    assert len(hits) == 1
    url = f'{BASE}/memory/digests'
    digest = client.post(f"{url}/from-conversation/{durable['id']}", json={}).json()
    assert client.post(f"{url}/{digest['id']}/approve", json={'version': digest['version']}).status_code == 200
    accept(scoped, scope='day', message='今天我训练的地方只能徒手', value='只能徒手')
    assert client.put('/api/profile', json={'equipment': '泳池'}).status_code == 200
    assert client.put('/api/profile', json={'equipment': '只有哑铃'}).status_code == 200
    assert advice(client)['equipment'] == '只有哑铃'
    assert client.post(f'{BASE}/memory/search', json={'query': '哑铃'}).json()['items'] == []
    assert client.get(url).json()['items'][0]['stale']


def test_temporary_claims_never_become_cross_session_evidence_or_digest(scoped):
    client, _, _, _ = scoped
    saved = accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    assert client.post(f'{BASE}/memory/search', json={'query': '哑铃'}).json()['items'] == []
    assert client.post(f"{BASE}/memory/digests/from-conversation/{saved['id']}", json={}).status_code == 409


def test_session_overrides_day_until_profile_correction(scoped):
    client, _, _, _ = scoped
    accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    local = accept(scoped, scope='session', message='这次我训练的地方只能徒手', value='只能徒手')
    assert result(recommend(client, local))['equipment'] == '只能徒手'
    assert advice(client)['equipment'] == '只有哑铃'
    assert client.put('/api/profile', json={'equipment': ''}).status_code == 200
    assert result(recommend(client, local))['status'] == 'needs_input'
    assert advice(client)['status'] == 'needs_input'


def test_day_equipment_keeps_action_replacement_until_that_day_fact_changes(scoped):
    client, _, _, _ = scoped
    saved = accept(scoped, scope='day', message='今天我可用哑铃和平板训练凳', value='哑铃和平板训练凳')
    first = result(recommend(client, saved))
    assert 'db-bench' in [item['id'] for item in first['exercises']]
    changed = send(client, saved, '把哑铃平板卧推换成哑铃地板推胸').json()
    assert changed['training_context']['replacements'] == {'db-bench': 'floor-press'}
    assert client.get(f"{BASE}/{changed['id']}").json()['training_context']['replacements'] == {'db-bench': 'floor-press'}
    assert 'floor-press' in [item['id'] for item in result(recommend(client, changed))['exercises']]
    accept(scoped, scope='day', message='今天我训练的地方只能徒手', value='只能徒手')
    assert 'replacements' not in client.get(f"{BASE}/{changed['id']}").json()['training_context']


def test_unrelated_profile_edit_keeps_day_but_explicit_empty_profile_revokes_it(scoped):
    client, _, _, _ = scoped
    assert client.put('/api/profile', json={'equipment': ''}).status_code == 200
    accept(scoped, scope='day', message='今天我训练的地方只有哑铃')
    assert client.put('/api/profile', json={'minutes_per_session': 50}).status_code == 200
    assert advice(client)['equipment'] == '只有哑铃'
    assert client.put('/api/profile', json={'equipment': ''}).status_code == 200
    assert advice(client)['status'] == 'needs_input'


def test_equipment_slot_without_memory_object_still_requires_scope_review(scoped):
    client, _, model, _ = scoped
    def edit(data, out):
        out.update(scope='strength', command='', training={
            'source_text': data['messages'][-1]['message'], 'equipment': '只有哑铃'})
    model.edit = edit
    current = send(client, conversation(client), '我训练的地方只有哑铃').json()
    response = understand(client, current, auto_apply=True)
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['equipment_memory']['scope'] == 'unspecified'
    assert confirm(client, ready).status_code == 422


def test_legacy_durable_confirmation_does_not_shadow_later_day_condition(scoped):
    from test_coach_memory import _confirmed_training
    client, _, model, _ = scoped
    old = _confirmed_training(client, model)
    accept(scoped, scope='day', message='今天我训练的地方只能徒手', value='只能徒手')
    refreshed = client.get(f"{BASE}/{old['id']}").json()
    assert result(recommend(client, refreshed))['equipment'] == '只能徒手'


def test_isolation_and_delete_durable_source(scoped):
    client, app, _, _ = scoped
    saved = accept(scoped)
    day = accept(scoped, scope='day', message='今天我训练的地方只能徒手', value='只能徒手')
    with TestClient(app) as other:
        register(other, 'bob')
        assert other.get(f"{BASE}/{day['id']}").status_code == 404
        assert other.request('DELETE', f"{BASE}/{day['id']}", json={'version': day['version']}).status_code == 404
        assert other.get('/api/profile').json()['equipment'] == ''
        assert advice(other)['status'] == 'needs_input'
    assert client.request('DELETE', f"{BASE}/{saved['id']}", json={'version': saved['version']}).status_code == 204
    assert client.get('/api/profile').json()['equipment'] == '只有哑铃'


@pytest.mark.parametrize('message,changes', [
    ('我平时训练的地方只有哑铃', {'source_text': '只有哑铃'}),
    ('我平时训练的地方只有哑铃', {'source_version': 2}),
    ('我平时训练的地方只有哑铃', {'value': '杠铃'}),
    ('今天只有哑铃，没有训练凳', {'value': '只有哑铃'}),
])
def test_ungrounded_candidate_is_rejected(scoped, message, changes):
    client, app, _, _ = scoped
    _, response = draft(scoped, message=message, **changes)
    assert response.status_code == 502, response.text
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    with app.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM coach_day_equipment').fetchone()[0] == 0


@pytest.mark.parametrize('text,expected', [('今天只有哑铃', 'day'), ('这次只有哑铃', 'session')])
def test_model_cannot_promote_explicit_temporary_scope(scoped, text, expected):
    _, response = draft(scoped, message=text, scope='long_term')
    assert response.status_code == 200, response.text
    assert response.json()['review']['equipment_memory']['scope'] == expected


def test_v16_upgrade_preserves_old_tables_and_verified_backup(tmp_path):
    from schema_fixtures import strip_memory_v18
    path = tmp_path / 'old.sqlite3'
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        strip_governance_v19(connection)
        strip_memory_v18(connection)
        connection.execute('DROP TABLE coach_day_equipment')
        connection.execute('PRAGMA user_version=16')
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'synthetic','synthetic')")
    before = fingerprints(path)
    backup = tmp_path / 'backup.sqlite3'
    assert migrate(path, backup)['schema'] == 19
    assert verify_backup(backup)['schema'] == 16
    assert all(fingerprints(path)[name] == value for name, value in before.items())
    database.initialize()
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM coach_day_equipment').fetchone()[0] == 0
