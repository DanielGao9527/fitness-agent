"""Private memory through final suggestions, using isolated API/model fixtures."""
import json

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from services.training_catalog import read_catalog
from services.backups import fingerprints, verify_backup
from scripts.migrate_memory import migrate
from schema_fixtures import strip_training_records_v23
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_knowledge import library
from test_meal_plans import plans
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_equipment_memory_scopes import scoped, accept as accept_equipment
from test_training_recommendations import recommend, result
from test_training_plans import plans as aerobic_plans, request as aerobic_request, accept as accept_plan


@pytest.fixture(autouse=True)
def forbid_real_providers(monkeypatch):
    from model.qwen import QwenTextModel
    from model.embeddings import QwenEmbeddingProvider
    def forbidden(*args, **kwargs):
        pytest.fail('This acceptance suite must not contact a real provider')
    monkeypatch.setattr(QwenTextModel, 'generate', forbidden)
    monkeypatch.setattr(QwenEmbeddingProvider, 'embed', forbidden)


def request_advice(client):
    data = send(client, conversation(client), '今天练什么').json()
    return data, result(recommend(client, data))


@pytest.mark.parametrize('change', ['create_delete', 'edit_restore', 'move_restore', 'status_restore'])
def test_actual_record_aba_never_revives_old_suggestion(scoped, change):
    client, _, _, _ = scoped
    accept_equipment(scoped)
    original = workout(name='胸背肩手臂', day='2026-09-14', status='completed')
    if change != 'create_delete':
        saved = client.post('/api/workouts', json=original).json()
    data, first = request_advice(client)
    if change == 'create_delete':
        saved = client.post('/api/workouts', json=original).json()
        assert client.delete(f"/api/workouts/{saved['id']}").status_code == 204
    else:
        changed = {'day': original['day'], 'name': '腿臀', 'minutes': 30, 'status': 'completed'}
        if change == 'move_restore':
            changed.update(day='2026-09-01', name=original['name'])
        if change == 'status_restore':
            changed.update(status='planned', name=original['name'])
        assert client.put(f"/api/workouts/{saved['id']}", json=changed).status_code == 200
        assert client.put(f"/api/workouts/{saved['id']}", json={
            'day': original['day'], 'name': original['name'], 'minutes': 30, 'status': 'completed'}).status_code == 200
    # Do not read between mutations: invalidation must survive an unseen A→B→A.
    old = client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']
    assert old['stale'], 'Restoring the record content must not reactivate old advice'
    assert recommend(client, data, client_id=first['client_id']).status_code == 409
    assert not result(recommend(client, data))['stale']


def test_confirmed_memory_actual_history_and_corrections_reach_final_advice(scoped):
    client, app, _, _ = scoped
    accept_equipment(scoped)
    original = workout(name='胸背肩手臂', day='2026-09-14', status='completed')
    saved = client.post('/api/workouts', json=original).json()
    assert client.post('/api/workouts', json=workout(name='腿臀', status='planned')).status_code == 201
    data, first = request_advice(client)
    assert first['equipment'] == '只有哑铃' and first['focus'] == 'lower'
    assert [r['name'] for r in first['history']['completed']] == ['胸背肩手臂']
    catalog = {item['id']: item for item in read_catalog()[0]}
    assert first['exercises'] and all(catalog[item['id']]['needs'] <= {'floor', 'dumbbell'} for item in first['exercises'])
    assert client.put('/api/profile', json={'equipment': '在家，徒手', 'experience': 'experienced',
                                          'minutes_per_session': 40}).status_code == 200
    assert client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']['stale']
    assert recommend(client, data, client_id=first['client_id']).status_code == 409
    bodyweight = result(recommend(client, data))
    assert bodyweight['equipment'] == '在家，徒手'
    assert bodyweight['exercises'] and all(catalog[item['id']]['needs'] <= {'floor'} for item in bodyweight['exercises'])
    assert client.put(f"/api/workouts/{saved['id']}", json={
        'day': original['day'], 'name': '腿臀', 'minutes': 30, 'status': 'completed'}).status_code == 200
    assert client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']['stale']
    fresh = result(recommend(client, data))
    assert fresh['focus'] == 'push' and fresh['equipment'] == '在家，徒手'
    assert [r['name'] for r in fresh['history']['completed']] == ['腿臀']
    assert len(client.get('/api/workouts', params={'day': DAY}).json()) == 1
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.app.state.knowledge = app.state.knowledge
        assert restarted.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 200
        restored = result(restarted.get(f"{BASE}/{data['id']}"))
        assert restored == fresh
        _, next_advice = request_advice(restarted)
        assert next_advice['equipment'] == '在家，徒手' and next_advice['focus'] == 'push'
        restarted.cookies.clear()
        register(restarted, 'bob')
        assert restarted.get(f"{BASE}/{data['id']}").status_code == 404
        assert restarted.get('/api/workouts', params={'day': original['day']}).json() == []
        assert restarted.put(f"/api/workouts/{saved['id']}", json={
            'day': original['day'], 'name': '胸', 'minutes': 30, 'status': 'completed'}).status_code == 404
        _, other = request_advice(restarted)
        assert other['status'] == 'needs_input' and not other['history']['completed']


def test_irrelevant_changes_and_duplicate_submissions_do_not_invalidate(scoped):
    client, app, _, _ = scoped
    accept_equipment(scoped)
    record = workout(name='胸', day='2026-09-14', status='completed')
    saved = client.post('/api/workouts', json=record).json()
    data, first = request_advice(client)
    assert client.post('/api/workouts', json=record).status_code == 201
    assert client.put(f"/api/workouts/{saved['id']}", json={
        'day': record['day'], 'name': record['name'], 'minutes': 30, 'status': 'completed'}).status_code == 200
    for day, status in [('2026-09-08', 'completed'), ('2026-09-16', 'completed'), (DAY, 'planned')]:
        assert client.post('/api/workouts', json=workout(name='腿臀', day=day, status=status)).status_code == 201
    with TestClient(app) as other:
        register(other, 'bob')
        assert other.post('/api/workouts', json=workout(name='腿臀', status='completed')).status_code == 201
    current = result(client.get(f"{BASE}/{data['id']}"))
    assert not current['stale'] and current['context_hash'] == first['context_hash']
    assert result(recommend(client, data, client_id=first['client_id'])) == first


@pytest.mark.parametrize('when', ['during_understanding', 'before_confirmation'])
def test_record_aba_blocks_old_model_review_without_confirming(scoped, when):
    client, _, model, _ = scoped
    accept_equipment(scoped)
    text = '我想练胸，本次20分钟'
    model.edit = lambda _, out: out.update(scope='strength', command='', training={
        'source_text': text, 'minutes': 20, 'time_basis': 'session', 'focus': '胸'})
    def change():
        record = client.post('/api/workouts', json=workout(name='腿臀', status='completed')).json()
        assert client.delete(f"/api/workouts/{record['id']}").status_code == 204
    data = send(client, conversation(client), text).json()
    if when == 'during_understanding':
        model.on_call = change
        assert understand(client, data).status_code == 409
    else:
        ready = understand(client, data).json()
        change()
        assert confirm(client, ready).status_code == 409
    assert client.get(f"{BASE}/{data['id']}").json()['pending']
    assert client.get('/api/workouts', params={'day': DAY}).json() == []


@pytest.mark.parametrize('when', ['during_generation', 'before_acceptance'])
def test_record_aba_blocks_aerobic_plan_even_without_a_conversation(aerobic_plans, when):
    client, _, model, _ = aerobic_plans
    def change():
        record = client.post('/api/workouts', json=workout(name='腿臀', status='completed')).json()
        assert client.delete(f"/api/workouts/{record['id']}").status_code == 204
    if when == 'during_generation':
        model.on_call = change
        assert client.post('/api/training-plans', json=aerobic_request()).status_code == 409
    else:
        plan = client.post('/api/training-plans', json=aerobic_request()).json()
        change()
        assert accept_plan(client, plan).status_code == 409


def test_revision_transactions_rollback_and_account_deletion(scoped):
    client, app, _, _ = scoped
    user_id = client.get('/api/auth/me').json()['id']
    with app.state.database.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('INSERT INTO workouts(user_id,client_id,day,payload) VALUES (?,?,?,?)',
                    (user_id, 'rollback-only', DAY, json.dumps({'status': 'completed'})))
        assert con.execute('SELECT revision FROM training_record_revisions WHERE user_id=?', (user_id,)).fetchone()[0] == 1
        con.rollback()
    with app.state.database.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM training_record_revisions').fetchone()[0] == 0
    assert client.post('/api/workouts', json=workout(name='胸', status='completed')).status_code == 201
    with app.state.database.connect() as con:
        con.execute('DELETE FROM users WHERE id=?', (user_id,))
        assert con.execute('SELECT COUNT(*) FROM training_record_revisions').fetchone()[0] == 0
        assert con.execute('PRAGMA foreign_key_check').fetchone() is None


def test_v22_upgrade_preserves_business_rows_and_seeds_existing_actual_dates(scoped, tmp_path):
    client, app, _, _ = scoped
    assert client.post('/api/workouts', json=workout(name='胸', status='completed')).status_code == 201
    assert client.post('/api/workouts', json=workout(name='腿', day='2026-09-14', status='planned')).status_code == 201
    path = app.state.database.path
    with app.state.database.connect() as con:
        strip_training_records_v23(con)
        con.execute('PRAGMA user_version=22')
    before = fingerprints(path)
    backup = tmp_path / 'v22.sqlite3'
    upgraded = migrate(path, backup)
    assert upgraded['schema'] == 23 and upgraded['tables'] == 39
    assert verify_backup(backup)['schema'] == 22
    assert all(fingerprints(path)[key] == value for key, value in before.items())
    Database(path).initialize()
    with app.state.database.connect() as con:
        assert [dict(row) for row in con.execute('SELECT day,revision FROM training_record_revisions')] == [{'day': DAY, 'revision': 1}]
        assert [r[1] for r in con.execute('PRAGMA table_info(training_record_revisions)')] == ['user_id', 'day', 'revision']
