from schema_fixtures import strip_governance_v19
"""Personal fact revisions must protect final advice, including A -> B -> A."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from deploy.check_release import check_schema
from scripts.migrate_memory import migrate
from services.backups import fingerprints, inspect_connection, verify_backup, create_backup, restore_backup
from services.records import RecordService
from services.profile_facts import write_profile
from schemas import Profile
from test_foundation import application, client, register
from test_knowledge import library
from test_meal_plans import plans
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_coach_memory import _confirmed_training
from test_training_recommendations import recommend, result


def change_equipment(client, value):
    response = client.put('/api/profile', json={'equipment': value})
    assert response.status_code == 200, response.text
    return response


def test_aba_profile_correction_never_revives_old_local_equipment(reviews):
    client, app, model, _ = reviews
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    saved = _confirmed_training(client, model)
    local = send(client, saved, '器械改为在家，徒手').json()
    assert result(recommend(client, local))['equipment'] == '在家，徒手'
    change_equipment(client, '健身房和泳池')
    change_equipment(client, '在家，只有哑铃')
    current = client.get(f"{BASE}/{local['id']}").json()
    assert 'equipment' not in current['training_context']
    assert current['turns'][-1]['response']['training_recommendation']['stale']
    final = result(recommend(client, current))
    assert final['status'] == 'ready' and final['equipment'] == '在家，只有哑铃'
    assert final['equipment_source'] == 'profile'


def test_aba_also_invalidates_advice_without_a_local_override(reviews):
    client, app, model, _ = reviews
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    _confirmed_training(client, model)
    current = send(client, conversation(client), '今天练什么').json()
    first = result(recommend(client, current))
    change_equipment(client, '在家，徒手')
    change_equipment(client, '在家，只有哑铃')
    assert client.get(f"{BASE}/{current['id']}").json()['turns'][-1]['response']['training_recommendation']['stale']
    assert recommend(client, current, client_id=first['client_id']).status_code == 409


def test_aba_during_understanding_and_before_confirmation_rejects_old_draft(reviews):
    client, _, model, _ = reviews
    change_equipment(client, '在家，只有哑铃')
    model.edit = lambda _, out: out.update(scope='strength', command='')
    current = send(client, conversation(client), '想练得轻松一些').json()

    def correct():
        change_equipment(client, '在家，徒手')
        change_equipment(client, '在家，只有哑铃')

    model.on_call = correct
    assert understand(client, current).status_code == 409
    model.on_call = None
    ready = understand(client, current)
    assert ready.status_code == 200, ready.text
    correct()
    assert confirm(client, ready.json()).status_code == 409


def test_aba_does_not_recall_superseded_equipment_or_approved_digest(reviews):
    client, _, model, _ = reviews
    old = _confirmed_training(client, model)
    url = f'{BASE}/memory/digests'
    candidate = client.post(f"{url}/from-conversation/{old['id']}", json={}).json()
    assert client.post(f"{url}/{candidate['id']}/approve", json={'version': candidate['version']}).status_code == 200
    change_equipment(client, '在家，徒手')
    change_equipment(client, '在家，只有哑铃')
    assert client.post(f'{BASE}/memory/search', json={'query': '哑铃'}).json()['items'] == []
    current = send(client, conversation(client), '上次说过的哑铃训练').json()
    assert understand(client, current).status_code == 200
    assert not model.messages[-1].get('historical_digests')
    assert not model.messages[-1].get('historical_evidence')


def test_confirmed_equipment_preserves_body_metadata_and_confirmation_is_idempotent(reviews):
    client, app, model, _ = reviews
    client.put('/api/profile', json={'weight_kg': 71})
    with app.state.database.connect() as connection:
        before = json.loads(connection.execute('SELECT payload FROM profiles').fetchone()[0])
    saved = _confirmed_training(client, model)
    with app.state.database.connect() as connection:
        after = json.loads(connection.execute('SELECT payload FROM profiles').fetchone()[0])
    assert after['_body_baselines'] == before['_body_baselines']
    assert after['_body_sources'] == before['_body_sources']
    fact = client.get('/api/profile/facts').json()
    assert confirm(client, saved).status_code == 200
    assert client.get('/api/profile/facts').json() == fact


def test_equipment_revision_survives_restart_noop_and_unrelated_save(reviews):
    client, app, model, _ = reviews
    _confirmed_training(client, model)
    before = client.get('/api/profile').headers.get('X-Equipment-Revision')
    assert before is not None and int(before) > 0
    change_equipment(client, '在家，只有哑铃')
    client.put('/api/profile', json={'weight_kg': 72})
    assert client.get('/api/profile').headers['X-Equipment-Revision'] == before
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get('/api/profile').headers['X-Equipment-Revision'] == before


def test_stale_page_cannot_overwrite_new_equipment_even_after_aba(client):
    register(client)
    initial = change_equipment(client, '在家，只有哑铃')
    old_revision = initial.headers['X-Equipment-Revision']
    change_equipment(client, '在家，徒手')
    change_equipment(client, '在家，只有哑铃')
    denied = client.put('/api/profile', json={'equipment': '泳池', 'display_name': 'must not save'},
                        headers={'X-Equipment-Revision': old_revision})
    assert denied.status_code == 409
    assert client.get('/api/profile').json()['display_name'] != 'must not save'
    current = client.get('/api/profile')
    assert int(current.headers['X-Equipment-Revision']) == int(old_revision) + 2
    assert client.put('/api/profile', json={'equipment': '泳池'}, headers={
        'X-Equipment-Revision': current.headers['X-Equipment-Revision']}).status_code == 200


@pytest.mark.parametrize('revision', ['-1', 'not-a-number'])
def test_revision_input_and_profile_fact_reads_are_authenticated(client, revision):
    assert client.get('/api/profile/facts').status_code == 401
    register(client)
    assert client.put('/api/profile', json={'equipment': '泳池'},
                      headers={'X-Equipment-Revision': revision}).status_code == 422
    assert client.put('/api/profile', json={'equipment': '泳池', 'user_id': 2}).status_code == 422
    assert client.get('/api/profile/facts').json()['items'][0]['revision'] == 0


def test_concurrent_equipment_saves_have_one_winner(client, application):
    user = register(client)
    initial = change_equipment(client, '在家，只有哑铃')
    revision = int(initial.headers['X-Equipment-Revision'])

    def save(value):
        try:
            RecordService(application.state.database, user['id']).save_profile(
                Profile(equipment=value), expected_equipment_revision=revision)
            return 'saved'
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ['在家，徒手', '泳池']))
    assert results.count('saved') == 1 and results.count(409) == 1
    assert int(client.get('/api/profile').headers['X-Equipment-Revision']) == revision + 1


def test_provenance_is_private_and_chat_deletion_does_not_revoke_profile(reviews):
    client, app, model, _ = reviews
    saved = _confirmed_training(client, model)
    facts = client.get('/api/profile/facts').json()['items'][0]
    assert facts['status'] == 'active' and facts['source'] == 'conversation_review'
    assert facts['conversation_id'] == saved['id'] and facts['turn_version'] == 1
    with TestClient(app) as other:
        stranger = register(other, 'bob')
        assert other.get('/api/profile/facts').json()['items'][0]['revision'] == 0
        with app.state.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            with pytest.raises(HTTPException) as denied:
                write_profile(connection, stranger['id'], {'equipment': '泳池'}, equipment_explicit=True,
                              source='conversation_review', conversation_id=saved['id'], turn_version=1)
            assert denied.value.status_code == 404
    assert client.request('DELETE', f"{BASE}/{saved['id']}", json={'version': saved['version']}).status_code == 204
    after = client.get('/api/profile/facts').json()['items'][0]
    assert after['revision'] == facts['revision'] and after['conversation_id'] is None
    assert after['source_available'] is False and after['status'] == 'active'
    assert client.get('/api/profile').json()['equipment'] == '在家，只有哑铃'
    assert client.post(f'{BASE}/memory/search', json={'query': '哑铃'}).json()['items'] == []
    with app.state.database.connect() as connection:
        audit = [dict(row) for row in connection.execute('SELECT * FROM profile_fact_events')]
    assert '哑铃' not in json.dumps(audit, ensure_ascii=False)
    # A clear revokes the active fact; keeping only metadata does not retain its value.
    change_equipment(client, '')
    cleared = client.get('/api/profile/facts').json()['items'][0]
    assert cleared['status'] == 'cleared' and cleared['revision'] == facts['revision'] + 1


def test_clear_then_same_value_does_not_restore_old_local_conditions(reviews):
    client, _, model, _ = reviews
    saved = _confirmed_training(client, model)
    old = send(client, saved, '器械改为在家，徒手').json()
    change_equipment(client, '')
    change_equipment(client, '在家，只有哑铃')
    assert 'equipment' not in client.get(f"{BASE}/{old['id']}").json()['training_context']


def test_noop_or_unrelated_save_does_not_drop_a_valid_local_condition(reviews):
    client, _, model, _ = reviews
    old = _confirmed_training(client, model)
    local = send(client, old, '器械改为在家，徒手').json()
    change_equipment(client, '在家，只有哑铃')
    assert client.put('/api/profile', json={'weight_kg': 73}).status_code == 200
    assert client.get(f"{BASE}/{local['id']}").json()['training_context']['equipment'] == '在家，徒手'


def test_v15_migration_seeds_provenance_and_preserves_every_business_row(tmp_path):
    from schema_fixtures import strip_memory_v18
    source = tmp_path / 'legacy.sqlite3'
    db = Database(source)
    db.initialize()
    with db.connect() as connection:
        strip_governance_v19(connection)
        strip_memory_v18(connection)
        connection.execute('DROP TABLE coach_day_equipment')
        connection.execute('DROP TABLE profile_fact_events')
        connection.execute('PRAGMA user_version=15')
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'synthetic','synthetic')")
        connection.execute('INSERT INTO profiles(user_id,payload) VALUES (1,?)',
                           (json.dumps({'equipment': '在家，只有哑铃', '_body_baselines': {'preserve': True}}),))
    before = fingerprints(source)
    with pytest.raises(ValueError, match='migration'):
        check_schema(Database, inspect_connection, source)
    backup = tmp_path / 'before.sqlite3'
    result = migrate(source, backup)
    assert result['schema'] == 20 and result['old_tables_preserved']
    assert verify_backup(backup)['schema'] == 15
    assert all(fingerprints(source)[name] == value for name, value in before.items())
    with db.connect() as connection:
        event = dict(connection.execute('SELECT * FROM profile_fact_events').fetchone())
        assert event['source'] == 'legacy_profile' and event['revision'] == 1
        assert event['conversation_id'] is None
    assert source.with_name(source.name + '.pre-v16.bak').exists()
    check_schema(Database, inspect_connection, source)
    assert RecordService(db, 1).profile_snapshot()[1] == 1
    fresh = tmp_path / 'v16.sqlite3'
    restored = tmp_path / 'restored.sqlite3'
    create_backup(source, fresh)
    assert restore_backup(fresh, restored)['schema'] == 20
    assert fingerprints(source)['profile_fact_events'] == fingerprints(restored)['profile_fact_events']
    db.initialize()
    assert RecordService(db, 1).profile_snapshot()[1] == 1
    with pytest.raises(ValueError, match='v14 through v19'):
        migrate(source, tmp_path / 'not-created.sqlite3')


def test_legacy_unversioned_context_is_not_assumed_current(reviews):
    client, app, model, _ = reviews
    old = _confirmed_training(client, model)
    local = send(client, old, '器械改为在家，徒手').json()
    with app.state.database.connect() as connection:
        response = json.loads(connection.execute(
            'SELECT response FROM coach_turns WHERE conversation_id=? AND version=?',
            (local['id'], local['version'])).fetchone()[0])
        response['training_context'].pop('profile_revision_basis')
        connection.execute('UPDATE coach_turns SET response=? WHERE conversation_id=? AND version=?',
                           (json.dumps(response), local['id'], local['version']))
    assert 'equipment' not in client.get(f"{BASE}/{local['id']}").json()['training_context']


def test_replaying_confirmed_review_after_correction_cannot_restore_old_fact(reviews):
    client, _, model, _ = reviews
    saved = _confirmed_training(client, model)
    change_equipment(client, '在家，徒手')
    before = client.get('/api/profile/facts').json()
    assert confirm(client, saved).status_code == 200
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    assert client.get('/api/profile/facts').json() == before
