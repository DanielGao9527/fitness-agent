"""Retired injury profile data never drives the current product."""
import json

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import ROOT
from services.plan_foods import exclusions
from services.intake_targets import target_state
from services.profile_context import read_profile
from schemas import Profile
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_knowledge import library
from test_training_plans import plans
from test_training_recommendations import setup, recommend, result
from test_coach import BASE, send
from test_meal_consent import URL, confirm
from datetime import date


def legacy(client, app, text):
    user_id = client.get('/api/auth/me').json()['id']
    with app.state.database.connect() as connection:
        row = connection.execute('SELECT payload FROM profiles WHERE user_id=?', (user_id,)).fetchone()
        saved = json.loads(row[0]) if row else {}
        saved['training_limitations'] = text
        connection.execute('INSERT INTO profiles(user_id,payload) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload',
                           (user_id, json.dumps(saved, ensure_ascii=False)))
    return user_id


@pytest.mark.parametrize('text', ['膝盖受伤', '肩膀不舒服', '腰部拉伤', '手腕和脚踝旧伤', '孕期'])
def test_retired_field_neither_blocks_nor_leaks_into_current_context(plans, text):
    client, app, model, _ = plans
    data = setup(client)
    user_id = legacy(client, app, text)
    profile = client.get('/api/profile').json()
    assert 'training_limitations' not in profile
    response = recommend(client, data)
    rec = result(response)
    assert rec['status'] == 'ready' and rec['exercises']
    assert 'training_limitations' not in response.text and text not in response.text
    assert 'training_scope' not in response.text
    agent = client.post('/api/agent/context', json={'day': DAY, 'message': '力量训练'})
    assert agent.status_code == 200 and 'training_limitations' not in agent.text
    assert not model.messages
    with app.state.database.connect() as connection:
        raw = json.loads(connection.execute('SELECT payload FROM profiles WHERE user_id=?', (user_id,)).fetchone()[0])
        assert raw['training_limitations'] == text  # Read-only requests do not erase history.
        assert not connection.execute('SELECT * FROM ai_usage').fetchall()
        assert not connection.execute('SELECT * FROM workouts').fetchall()


def test_rejected_retired_input_preserves_profile_and_save_removes_old_key(plans):
    client, app, _, _ = plans
    setup(client, food_allergies='西兰花过敏')
    user_id = legacy(client, app, '旧伤')
    profile = client.get('/api/profile').json()
    assert 'training_limitations' not in Profile.model_json_schema()['properties']
    rejected = client.put('/api/profile', json={**profile, 'training_limitations': '新值'})
    assert rejected.status_code == 422 and client.get('/api/profile').json() == profile
    assert client.put('/api/profile', json=profile).json() == profile
    with app.state.database.connect() as connection:
        raw = json.loads(connection.execute('SELECT payload FROM profiles WHERE user_id=?', (user_id,)).fetchone()[0])
        assert 'training_limitations' not in raw and raw['food_allergies'] == '西兰花过敏'
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get('/api/profile').json() == profile
        restarted.cookies.clear()
        register(restarted, 'scope_bob')
        assert restarted.get('/api/profile').json()['food_allergies'] == ''


def test_legacy_changes_no_longer_invalidate_meal_targets_consent_or_training(plans):
    client, app, _, _ = plans
    data = setup(client, food_allergies='西兰花过敏')
    user_id = client.get('/api/auth/me').json()['id']
    rec = result(recommend(client, data))
    assert confirm(client).status_code == 200
    consent = client.get(URL).json()
    with app.state.database.connect() as connection:
        before = target_state(connection, user_id, date.fromisoformat(DAY))['context_hash']
    legacy(client, app, '旧膝伤')
    assert client.get(URL).json() == consent
    with app.state.database.connect() as connection:
        assert target_state(connection, user_id, date.fromisoformat(DAY))['context_hash'] == before
        assert 'broccoli' in exclusions(read_profile(connection, user_id))
    latest = result(client.get(f"{BASE}/{data['id']}"))
    assert latest == rec and not latest['stale']
    assert client.put('/api/profile', json={'food_allergies': '牛肉过敏'}).status_code == 200
    assert not client.get(URL).json()['confirmed']


def test_new_adaptation_endpoint_absent_and_normal_history_still_used(plans):
    client, app, model, _ = plans
    data = setup(client)
    assert client.post(f"{BASE}/{data['id']}/training-scope", json={}).status_code == 404
    assert not (ROOT / 'services/training_scope.py').exists()
    assert client.post('/api/workouts', json=workout(name='腿臀', status='completed')).status_code == 201
    rec = result(recommend(client, data))
    assert rec['status'] == 'ready'
    assert all('legs' not in item['loads'] for item in rec['exercises'])
    assert len(client.get('/api/workouts', params={'day': DAY}).json()) == 1 and not model.messages


def test_out_of_scope_has_no_injury_questionnaire_or_alternative_offer(plans):
    client, _, _, _ = plans
    data = setup(client, preferences='孕期')
    rec = result(recommend(client, data))
    assert rec['status'] == 'blocked' and not rec['exercises'] and not rec['aerobic_options']
    assert '本产品' in rec['message'] and '不提供' in rec['message']
    assert not any(word in rec['message'] for word in ('是否', '允许范围', '删除', '这些信息可继续补充'))
    assert 'training_scope' not in rec
