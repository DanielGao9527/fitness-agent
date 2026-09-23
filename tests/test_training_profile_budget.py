import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import create_app
from schemas import Profile, CoachTrainingUpdate
from services.training_context import current_conditions, merge_training, training_command
from services.training_recommendations import build
from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_training_plans import plans
from test_coach import BASE, send
from test_training_program import VENUE, facts, evidence
from test_training_recommendations import setup, recommend, result


@pytest.mark.parametrize('split', ['auto', 'full_body', 'upper_lower'])
def test_removed_splits_rejected_on_new_input(client, split):
    register(client)
    assert client.put('/api/profile', json={'training_split': split}).status_code == 422
    with pytest.raises(ValidationError):
        CoachTrainingUpdate(source_text='change split', split=split)
    assert client.get('/api/profile').json()['training_split'] == 'ppl'


@pytest.mark.parametrize('split', ['ppl', 'four', 'five'])
def test_three_splits_accept_120_minute_profile(client, split):
    register(client)
    response = client.put('/api/profile', json={'training_split': split, 'minutes_per_session': 120})
    assert response.status_code == 200
    profile = client.get('/api/profile').json()
    assert (profile['training_split'], profile['minutes_per_session']) == (split, 120)


@pytest.mark.parametrize('legacy', ['auto', 'full_body', 'upper_lower'])
def test_legacy_profile_read_bridge_is_persistent_readonly_and_private(client, application, legacy):
    register(client)
    client.put('/api/profile', json={'minutes_per_session':120})
    with application.state.database.connect() as connection:
        user_id, payload = connection.execute('SELECT user_id,payload FROM profiles').fetchone()
        old = json.loads(payload)
        old['training_split'] = legacy
        stored = json.dumps(old, ensure_ascii=False)
        connection.execute('UPDATE profiles SET payload=? WHERE user_id=?', (stored, user_id))
        connection.commit()
    assert client.get('/api/profile').json()['training_split'] == 'ppl'
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get('/api/profile').json()['minutes_per_session'] == 120
        assert restarted.get('/api/profile').json()['training_split'] == 'ppl'
        restarted.cookies.clear()
        register(restarted, 'otherprofile')
        assert restarted.get('/api/profile').json()['minutes_per_session'] != 120
    with application.state.database.connect() as connection:
        assert connection.execute('SELECT payload FROM profiles WHERE user_id=?', (user_id,)).fetchone()[0] == stored


def test_budget_is_not_a_forced_duration():
    f = facts(training_split='ppl')
    f['profile']['minutes_per_session'] = 120
    rec = build(f, evidence())
    assert rec['available_minutes'] == 120
    assert 60 < rec['estimated_minutes'] < 120
    assert '档案中每次可用120分钟' in rec['message']
    assert f"预计约{rec['estimated_minutes']}分钟" in rec['message']
    assert '不为凑满时间' in rec['message']
    assert len(rec['exercises']) == 6
    assert all(item['sets'] <= 4 for item in rec['exercises'])
    assert any(item['sets'] == 4 for item in rec['exercises'])


def test_explicit_override_survives_unchanged_profile_but_not_profile_edits():
    profile = Profile(minutes_per_session=60, training_split='five').model_dump()
    context = merge_training({}, {'minutes':40, 'time_basis':'session', 'split':'ppl'}, profile)
    assert current_conditions(context, profile) == context
    today = merge_training(context, training_command('今天练什么'), profile)
    assert today['minutes'] == 40 and today['split'] == 'ppl'
    new = {**profile, 'minutes_per_session':120}
    changed = current_conditions(today, new)
    assert 'minutes' not in changed and 'time_basis' not in changed
    assert changed['split'] == 'ppl'
    changed = current_conditions(changed, {**new, 'training_split':'four'})
    assert 'split' not in changed and 'profile_basis' not in changed
    assert context['minutes'] == 40, 'normalization must not mutate saved history'


def test_aerobic_switch_discards_orphaned_split_anchor():
    profile = Profile().model_dump()
    context = merge_training({}, {'split':'five', 'minutes':30, 'time_basis':'session'}, profile)
    aerobic = merge_training(context, {'kind':'aerobic'}, profile)
    assert aerobic['profile_basis'] == {'minutes_per_session':profile['minutes_per_session']}


def test_latest_profile_reaches_existing_chat_and_regeneration(plans):
    client, app, model, _ = plans
    data = setup(client, equipment=VENUE, minutes_per_session=60)
    data = send(client, data, '本次40分钟').json()
    first = result(recommend(client, data))
    assert first['available_minutes'] == 40
    profile = client.get('/api/profile').json()
    assert client.put('/api/profile', json={**profile, 'minutes_per_session':120}).status_code == 200
    restored = client.get(f"{BASE}/{data['id']}").json()
    assert restored['turns'][-1]['response']['training_recommendation']['stale']
    fresh = result(recommend(client, restored))
    assert fresh['available_minutes'] == 120
    assert '档案中每次可用120分钟' in fresh['message']
    data = send(client, restored, '今天练什么').json()
    assert result(recommend(client, data))['available_minutes'] == 120
    data = send(client, data, '本次40分钟').json()
    assert result(recommend(client, data))['available_minutes'] == 40
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.app.state.knowledge = app.state.knowledge
        restarted.cookies.update(client.cookies)
        assert result(recommend(restarted, data))['available_minutes'] == 40
    assert not model.messages
    assert client.get('/api/workouts', params={'day': DAY}).json() == []
    assert client.get('/api/training-plans', params={'day': DAY}).json() == []
    client.cookies.clear()
    register(client, 'otherbudget')
    assert recommend(client, data).status_code == 404


@pytest.mark.parametrize('label', ['全身训练', '上下肢交替', '自动安排'])
def test_removed_split_chat_is_clear_and_can_recover(plans, label):
    client, _, model, _ = plans
    data = setup(client)
    data = send(client, data, '改成' + label).json()
    rec = result(recommend(client, data))
    assert rec['status'] == 'needs_input' and not rec['exercises']
    assert '只提供三分化、四分化和五分化' in rec['message']
    data = send(client, data, '改成四分化').json()
    assert result(recommend(client, data))['structure']['split'] == 'four'
    assert not model.messages
