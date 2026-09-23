from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from services.training_recommendations import equipment
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_knowledge import library, mutate
from test_training_plans import plans
from test_coach import BASE, conversation, send


def recommend(client, data, **changes):
    return client.post(f"{BASE}/{data['id']}/recommend-training", json={
        'client_id': str(uuid4()), 'version': data['version'], **changes})


def result(response):
    assert response.status_code == 200, response.text
    return response.json()['turns'][-1]['response']['training_recommendation']


def setup(client, **changes):
    assert client.put('/api/profile', json={'equipment': '在家，只有哑铃', 'experience': 'experienced',
        'minutes_per_session': 40, **changes}).status_code == 200
    return send(client, conversation(client), '结合前几天的训练，今天练什么？').json()


def test_previous_policy_suggestion_becomes_stale_without_writing_facts(plans, monkeypatch):
    import services.training_recommendations as module
    client, app, model, _ = plans
    current_policy = module.POLICY
    data = setup(client)
    monkeypatch.setattr(module, 'POLICY', 'training-program-2026-09-20.1')
    saved = result(recommend(client, data))
    monkeypatch.setattr(module, 'POLICY', current_policy)
    assert current_policy != saved['policy']
    old = client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']
    assert old['stale'] and not model.messages
    assert client.get('/api/workouts', params={'day': DAY}).json() == []
    assert result(recommend(client, data))['policy'] == current_policy


def test_recommendation_is_not_fact_and_actual_changes_are_used(plans):
    client, app, model, _ = plans
    data = setup(client)
    first = result(recommend(client, data))
    assert first['focus'] == 'push' and first['status'] == 'ready'
    assert all(item['sets'] > 0 for item in first['exercises'])
    assert 'floor-press' in [item['id'] for item in first['exercises']]
    key = str(uuid4())
    response = recommend(client, data, client_id=key)
    assert recommend(client, data, client_id=key).json() == response.json()
    assert client.get('/api/workouts', params={'day': DAY}).json() == []
    assert client.get('/api/training-plans', params={'day': DAY}).json() == []
    client.post('/api/workouts', json=workout(name='腿臀', status='completed'))
    stale = client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']
    assert stale['stale']
    assert recommend(client, data, client_id=key).status_code == 409
    updated = result(recommend(client, data))
    assert all('legs' not in item['loads'] for item in updated['exercises'])
    assert [row['name'] for row in updated['history']['completed']] == ['腿臀']
    assert not model.messages
    with app.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ai_usage').fetchone()[0] == 0


def test_recent_upper_body_chooses_lower_and_excludes_legacy_plans(plans):
    client, _, _, _ = plans
    data = setup(client)
    for name, day, status in [('肩', '2026-09-14', 'completed'), ('手臂和背', DAY, 'completed'),
                              ('腿', DAY, 'planned'), ('胸', '2026-09-16', 'completed')]:
        assert client.post('/api/workouts', json=workout(name=name, day=day, status=status)).status_code == 201
    rec = result(recommend(client, data))
    assert rec['focus'] == 'lower'
    assert len(rec['history']['completed']) == 2
    assert {item['focus'] for item in rec['exercises']} <= {'legs', 'core'}
    assert rec['exercises'] and rec['gaps']


def test_cardio_not_counted_until_recorded_and_uses_calendar_week(plans):
    client, _, _, _ = plans
    data = setup(client, equipment='健身房和泳池')
    rec = result(recommend(client, data))
    assert rec['weekly_cardio'] == {'cardio_days': 0, 'cardio_minutes': 0}
    assert [item['id'] for item in rec['aerobic_options']] == ['swim']
    assert rec['aerobic_is_alternative']
    assert result(recommend(client, data))['weekly_cardio']['cardio_days'] == 0
    for name, day, status in [('游泳', '2026-09-14', 'completed'), ('游泳', DAY, 'planned'),
                              ('游泳', '2026-09-13', 'completed')]:
        client.post('/api/workouts', json=workout(name=name, day=day, status=status))
    assert result(recommend(client, data))['weekly_cardio'] == {'cardio_days': 1, 'cardio_minutes': 30}


@pytest.mark.parametrize('text,expected,uncertain', [
    ('在家，只有哑铃', {'floor', 'dumbbell'}, False),
    ('家里没有哑铃，只有跑步机', {'floor', 'treadmill'}, False),
    ('健身房和泳池', {'floor', 'pool'}, False),
    ('健身房但不能去泳池', {'floor'}, False),
    ('没有哑铃', set(), True), ('外星器械', set(), True),
])
def test_equipment_no_negated_or_invented_resources(text, expected, uncertain):
    assert equipment(text) == (expected, uncertain)


@pytest.mark.parametrize('changes,status', [
    ({'preferences': '孕期'}, 'blocked'),
    ({'equipment': ''}, 'needs_input'),
    ({'equipment': '健身房，有个没见过的机器'}, 'needs_input'),
])
def test_missing_or_unsafe_profile_no_exercises(plans, changes, status):
    client, _, _, _ = plans
    rec = result(recommend(client, setup(client, **changes)))
    assert rec['status'] == status and not rec['exercises'] and not rec['aerobic_options']


def test_unknown_history_and_explicit_focus_failure(plans):
    client, _, _, _ = plans
    data = setup(client, equipment='在家，徒手')
    data = send(client, data, '练背').json()
    assert result(recommend(client, data))['status'] == 'needs_input'
    assert client.post('/api/workouts', json=workout(name='力量训练', status='completed')).status_code == 201
    data = send(client, data, '练胸').json()
    rec = result(recommend(client, data))
    assert '部位' in rec['message'] and not rec['exercises']


def test_time_context_pending_health_and_sources(plans):
    client, _, _, library = plans
    data = setup(client)
    data = send(client, data, '今天只有20分钟').json()
    assert result(recommend(client, data))['status'] == 'needs_input'
    data = send(client, data, '今天总共30分钟').json()
    client.post('/api/workouts', json=workout(name='游泳', minutes=20, status='completed'))
    assert result(recommend(client, data))['status'] == 'needs_input'
    data = send(client, data, '接下来还有30分钟').json()
    rec = result(recommend(client, data))
    assert rec['available_minutes'] == 30 and rec['estimated_minutes'] <= 30
    mutate(library, lambda doc: next(s for s in doc['sources'] if s['id']=='mayo-chest').update(status='withdrawn'))
    assert client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['training_recommendation']['stale']
    assert result(recommend(client, data))['status'] == 'ready'
    mutate(library, lambda doc: next(s for s in doc['sources'] if s['id']=='mayo-strength').update(status='withdrawn'))
    assert result(recommend(client, data))['status'] == 'unavailable'
    data = send(client, data, '肩疼').json()
    assert recommend(client, data).status_code == 409


def test_persistence_account_isolation_validation_and_disabled(plans):
    client, app, _, library = plans
    data = setup(client)
    saved = result(recommend(client, data))
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.app.state.knowledge = library
        restarted.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD})
        assert result(restarted.get(f"{BASE}/{data['id']}")) == saved
        restarted.cookies.clear()
        register(restarted, 'bob')
        assert recommend(restarted, data).status_code == 404
    assert recommend(client, data, version=0).status_code == 422
    assert recommend(client, data, user_id=2).status_code == 422
    assert client.post(f"{BASE}/{data['id']}/recommend-training", json={'version':data['version'], 'client_id':str(uuid4())},
        headers={'Origin':'https://evil.example'}).status_code == 403
    app.state.settings = replace(app.state.settings, training_plan_enabled=False)
    assert recommend(client, data).status_code == 503
