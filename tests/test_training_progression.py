import copy
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import create_app
from services.training_progression import SOURCE, BASES, guidance, performance_history, tracking_catalog
from services.training_recommendations import equipment, build
from services.training_catalog import read_catalog
from test_foundation import DAY, application, client, register, workout
from test_training_program import facts, evidence
from test_knowledge import library
from test_training_plans import plans
from test_training_recommendations import setup, recommend, result
from test_coach import BASE, send


def performance(**changes):
    return {'exercise_id': 'db-bench', 'load_basis': 'each', 'equipment_label': 'Fixture dumbbells / flat bench',
            'increment_kg': 1, 'technique_stable': True,
            'sets': [{'load_kg': 20, 'reps': 13, 'rir': 2} for _ in range(3)], **changes}


def payload(**changes):
    return workout(name='哑铃平板卧推', status='completed', performance=performance(), **changes)


def history():
    return [{'id': index, 'name': '哑铃平板卧推', 'day': (date.fromisoformat(DAY)-timedelta(days=offset)).isoformat(),
             'performance': performance()} for index, offset in enumerate([3, 6])]


ACTION = {'id': 'db-bench', 'sets': 2}
EVIDENCE = {SOURCE: {'chunk_id': SOURCE}}


def test_optional_record_roundtrip_retry_restart_and_cross_user(client, application):
    assert client.get('/api/workouts/exercises').status_code == 401
    user = register(client)
    assert len(client.get('/api/workouts/exercises').json()['items']) == len(BASES)
    body = payload()
    response = client.post('/api/workouts/batch', json={'items': [body]})
    assert response.status_code == 201, response.text
    saved = response.json()[0]
    assert saved['performance'] == performance()
    assert client.post('/api/workouts/batch', json={'items': [body]}).json() == [saved]
    conflict = copy.deepcopy(body)
    conflict['performance']['sets'][0]['reps'] = 12
    assert client.post('/api/workouts/batch', json={'items': [workout(), conflict]}).status_code == 409
    assert len(client.get('/api/workouts', params={'day': DAY}).json()) == 1
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get('/api/workouts', params={'day': DAY}).json() == [saved]
        restarted.cookies.clear()
        other = register(restarted, 'other')
        assert restarted.get('/api/workouts', params={'day': DAY}).json() == []
        with application.state.database.connect() as connection:
            assert performance_history(connection, other['id'], DAY) == []
            assert len(performance_history(connection, user['id'], DAY)) == 1
        update = {k:v for k,v in body.items() if k!='client_id'}
        assert restarted.put(f"/api/workouts/{saved['id']}", json=update).status_code == 404


def test_legacy_edit_preserves_clear_and_rename_are_explicit(client):
    register(client)
    body = payload()
    row = client.post('/api/workouts', json=body).json()
    old = {k:v for k,v in body.items() if k not in ('client_id','performance')}
    path = f"/api/workouts/{row['id']}"
    assert client.put(path, json=old).json()['performance'] == performance()
    assert client.put(path, json={**old, 'performance': None}).json()['performance'] is None
    assert client.put(path, json={**old, 'performance': performance()}).status_code == 200
    assert client.put(path, json={**old, 'name': '胸和肩'}).json()['performance'] is None


@pytest.mark.parametrize('change', [
    {'exercise_id': 'not-known'}, {'load_basis': 'total'}, {'equipment_label': ''}, {'sets': []},
    {'sets': [{'load_kg': -1, 'reps': 12}]}, {'sets': [{'load_kg': True, 'reps': 12}]},
    {'sets': [{'load_kg': 20, 'reps': True}]}, {'sets': [{'load_kg': 20, 'reps': 1.5}]},
    {'sets': [{'load_kg': 20, 'reps': 12, 'rir': -1}]}, {'increment_kg': 0}, {'technique_stable': 'yes'},
    {'sets': [{'load_kg': 20, 'reps': 12}]*13}, {'user_id': 2},
])
def test_bad_performance_atomic_no_write(client, change):
    register(client)
    bad = payload()
    bad['performance'].update(change)
    assert client.post('/api/workouts/batch', json={'items': [workout(), bad]}).status_code == 422
    assert client.get('/api/workouts', params={'day': DAY}).json() == []


def test_valid_progression_and_missing_optional_evidence():
    hint = guidance(ACTION, history(), DAY, EVIDENCE)
    assert hint['status'] == 'consider' and hint['suggested_kg'] == 21
    assert hint['current_kg'] == 20 and hint['basis'] == '每只哑铃'
    assert guidance(ACTION, history(), DAY, {})['status'] == 'unavailable'
    assert guidance({'id':'pushup'}, history(), DAY, EVIDENCE) is None
    assert guidance(ACTION, [], DAY, EVIDENCE)['status'] == 'insufficient'


@pytest.mark.parametrize('kind', ['same-day','stale','future','name','basis','equipment','increment',
    'count','few-sets','weight','variable-weight','reps-low','reps-high','rir-missing','rir-low',
    'technique','no-increment','step-small','step-large','malformed'])
def test_no_automatic_increase_without_comparable_actuals(kind):
    rows = history()
    p = rows[0]['performance']
    if kind == 'same-day': rows[1]['day'] = rows[0]['day']
    if kind == 'stale': rows[1]['day'] = '2026-01-01'
    if kind == 'future': rows[0]['day'] = '2027-01-01'
    if kind == 'name': rows[0]['name'] = '胸'
    if kind == 'basis': p['load_basis'] = 'total'
    if kind == 'equipment': p['equipment_label'] = 'different bench'
    if kind == 'increment': p['increment_kg'] = 2
    if kind == 'count': p['sets'].pop()
    if kind == 'few-sets':
        for row in rows: row['performance']['sets'] = row['performance']['sets'][:1]
    if kind == 'weight':
        for item in p['sets']: item['load_kg'] = 21
    if kind == 'variable-weight': p['sets'][0]['load_kg'] = 19
    if kind == 'reps-low': p['sets'][0]['reps'] = 12
    if kind == 'reps-high': p['sets'][0]['reps'] = 20
    if kind == 'rir-missing': p['sets'][0]['rir'] = None
    if kind == 'rir-low': p['sets'][0]['rir'] = 0
    if kind == 'technique': p['technique_stable'] = False
    if kind in ('no-increment','step-small','step-large'):
        for row in rows: row['performance']['increment_kg'] = {'no-increment':None,'step-small':0.1,'step-large':5}[kind]
    if kind == 'malformed': p['sets'][0]['reps'] = 'bad'
    result = guidance(ACTION, rows, DAY, EVIDENCE)
    assert result['status'] != 'consider' and 'suggested_kg' not in result


def test_new_machines_explicit_and_not_implied():
    for text, token in [('髋内收机','adductor-machine'),('髋外展机','abductor-machine'),
                        ('反向蝴蝶机','rear-delt-machine'),('三头伸展机','triceps-machine'),('卷腹机','abdominal-machine')]:
        assert equipment(text) == ({token}, False)
        resources, uncertain = equipment('健身房，没有'+text)
        assert not uncertain and token not in resources
    assert 'rear-delt-machine' not in equipment('蝴蝶机')[0]
    assert 'abductor-machine' not in equipment('内收机')[0]


def test_all_tracking_rules_match_actions():
    catalog = {item['id']: item for item in read_catalog()[0]}
    assert len(tracking_catalog()) == len(BASES)
    assert all('12' in catalog[key]['unit'] for key in BASES)


@pytest.mark.parametrize('completed_sets, expected', [(3, 'insufficient'), (4, 'consider')])
def test_today_hints_do_not_predict_future_performance(completed_sets, expected):
    f = facts()
    f['request'] = {'focus':'胸'}
    f['performance_history'] = history()
    for row in f['performance_history']:
        row['performance']['sets'] = [{'load_kg': 20, 'reps': 13, 'rir': 2} for _ in range(completed_sets)]
    rec = build(f, {**evidence(), **EVIDENCE})
    action = next(item for item in rec['exercises'] if item['id']=='db-bench')
    assert action['sets'] == 4
    assert action['load_guidance']['status'] == expected
    assert 'program' not in rec


@pytest.mark.parametrize('completed_sets, expected', [(3, 'insufficient'), (4, 'consider')])
def test_api_uses_actual_old_history_stales_and_does_not_write(plans, completed_sets, expected):
    client, app, model, _ = plans
    data = setup(client, equipment='哑铃，平板凳')
    data = send(client, data, '练胸').json()
    saved = []
    for day in ['2026-09-12','2026-09-09']:
        body = payload(day=day)
        body['performance']['sets'] = [{'load_kg': 20, 'reps': 13, 'rir': 2} for _ in range(completed_sets)]
        response=client.post('/api/workouts', json=body)
        assert response.status_code == 201
        saved.append(response.json())
    rec = result(recommend(client, data))
    action = next(row for row in rec['exercises'] if row['id']=='db-bench')
    assert action['sets'] == 4
    assert action['load_guidance']['status'] == expected
    assert client.get('/api/workouts',params={'day':DAY}).json() == []
    assert not model.messages
    client.delete(f"/api/workouts/{saved[0]['id']}")
    old=client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']
    assert old['stale']
    rec = result(recommend(client, data))
    assert next(row for row in rec['exercises'] if row['id']=='db-bench')['load_guidance']['status'] == 'insufficient'


def test_history_ignores_future_and_planned(client, application):
    user=register(client)
    for day, status in [('2026-09-09','completed'),('2026-09-03','completed'),('2026-09-12','planned'),('2027-01-01','completed')]:
        body=payload(day=day)
        body['status']=status
        assert client.post('/api/workouts', json=body).status_code == 201
    with application.state.database.connect() as connection:
        rows=performance_history(connection,user['id'],DAY)
    assert [row['day'] for row in rows] == ['2026-09-09']
