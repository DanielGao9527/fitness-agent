from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app import create_app

from test_foundation import DAY, application, client, register, workout
from test_knowledge import library
from test_meal_plans import plans
from test_coach_reviews import reviews, understand
from test_coach import BASE, conversation, send
from test_meal_consent import confirm, URL
from test_training_program import facts, evidence
from services.training_recommendations import build
from services.training_context import training_command, merge_training


@pytest.mark.parametrize('experience,minutes,low,high', [
    ('beginner',30,3,3), ('beginner',45,4,4),
    ('experienced',50,4,4), ('experienced',60,5,5), ('experienced',90,7,8),
])
def test_realistic_volume_respects_time_and_experience(experience, minutes, low, high):
    f = facts(training_split='ppl')
    f['profile'].update(experience=experience, minutes_per_session=minutes)
    if minutes >= 85:
        f['history']['completed'] = [{'day':'2026-09-13', 'name':'背和二头', 'minutes':40}]
    rec = build(f, evidence())
    assert low <= len(rec['exercises']) <= high
    assert all(2 <= item['sets'] <= (3 if experience == 'beginner' else 4) for item in rec['exercises'])
    assert len({item['id'] for item in rec['exercises']}) == len(rec['exercises'])
    assert rec['estimated_minutes'] <= minutes
    assert rec['estimated_minutes'] == 10 + 3 * sum(item['sets'] for item in rec['exercises'])


@pytest.mark.parametrize('split,last,next_focus', [
    ('ppl','胸和三头','pull'), ('ppl','背和二头','lower'),
    ('five','胸','back'), ('five','背','lower'), ('five','腿','shoulders'),
    ('four','肩和手臂','chest'), ('ppl','腿','push'),
])
def test_direction_follows_completed_workout_two_days_ago(split, last, next_focus):
    f = facts(training_split=split)
    f['profile']['training_days'] = 0
    f['history']['completed'] = [{'day':'2026-09-13', 'name':last, 'minutes':40}]
    rec = build(f, evidence())
    assert rec['focus'] == next_focus and '09-13' in rec['structure']['reason']
    assert 'program' not in rec
    assert build(f, evidence()) == rec, 'suggestions must not advance the rotation'


def test_older_or_future_workouts_do_not_advance_and_recent_loads_still_apply():
    f = facts(training_split='five')
    f['history']['completed'] = [{'day':'2026-09-08','name':'胸','minutes':40},
                                  {'day':'2026-09-16','name':'胸','minutes':40}]
    assert build(f, evidence())['focus'] == 'chest'
    f['history']['completed'] = [{'day':DAY,'name':'胸','minutes':40}]
    result = build(f, evidence())
    assert result['focus'] == 'lower'
    assert all(not set(row['loads']) & {'chest','arms','shoulders'} for row in result['exercises'])


def test_plain_gym_explains_limits_but_does_not_invent_equipment():
    f = facts(training_split='four')
    f['profile'].update(equipment='健身房和泳池', experience='beginner', minutes_per_session=45)
    f['history']['completed'] = [{'day':'2026-09-12','name':'胸和三头','minutes':40}]
    rec = build(f, evidence())
    assert rec['focus'] == 'lower' and len(rec['exercises']) == 4
    assert all(row['equipment'] == ['floor'] and 2 <= row['sets'] <= 3 for row in rec['exercises'])
    assert '具体器械' in rec['equipment_note']


def test_today_question_clears_old_focus_or_aerobic_without_losing_equipment():
    updated = merge_training({'kind':'strength','focus':'胸','replacements':{'db-bench':'barbell-bench'},
                              'equipment':'哑铃','minutes':40,'time_basis':'session'}, training_command('今天练什么'))
    assert 'focus' not in updated and 'replacements' not in updated
    assert updated['equipment'] == '哑铃' and 'minutes' not in updated
    assert merge_training({'kind':'aerobic','activity':'swim'},training_command('今天练什么')) == {'kind':'strength'}


def test_daily_confirmation_changes_at_beijing_day_boundary(client, monkeypatch):
    import services.meal_consent as module
    register(client)
    monkeypatch.setattr(module, 'business_today', lambda: date(2026,9,23))
    assert confirm(client).status_code == 200
    old = client.get(URL).json()
    assert old['confirmed']
    monkeypatch.setattr(module, 'business_today', lambda: date(2026,9,24))
    assert not client.get(URL).json()['confirmed']
    assert confirm(client, context_hash=old['context_hash']).status_code == 409
    assert confirm(client).status_code == 200


def test_auto_understanding_requires_consent_then_is_idempotent_and_no_actual_write(reviews):
    client, app, model, _ = reviews
    client.delete(URL)
    model.edit = lambda _, out: out.update(scope='strength', command='')
    data = send(client, conversation(client), '请帮我结合最近情况认真安排一下训练').json()
    assert data['pending']
    token = str(uuid4())
    assert understand(client, data, client_id=token, auto_apply=True).status_code == 409
    assert not model.messages
    assert confirm(client).status_code == 200
    profile = client.get('/api/profile').json()
    response = understand(client, data, client_id=token, auto_apply=True)
    assert response.status_code == 200, response.text
    applied = response.json()
    assert not applied['pending'] and applied['intent'] == 'training'
    assert applied['turns'][-1]['response']['source'] == 'applied_understanding'
    assert understand(client, data, client_id=token, auto_apply=True).status_code == 200
    assert len(model.messages) == 1
    assert client.get('/api/profile').json() == profile
    assert client.get('/api/workouts',params={'day':DAY}).json() == []
    assert client.get('/api/meals',params={'day':DAY}).json() == []
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restored = restarted.get(f"{BASE}/{data['id']}").json()
        assert not restored['pending']
        assert restored['turns'][-1]['response']['source'] == 'applied_understanding'
    client.cookies.clear()
    register(client,'otherauto')
    confirm(client)
    assert understand(client,data,auto_apply=True).status_code == 404


@pytest.mark.parametrize('kind', ['unclear','unresolved','revoked','profile_changed'])
def test_auto_understanding_does_not_bypass_questions_or_changed_conditions(reviews, kind):
    client, _, model, _ = reviews
    confirm(client)
    data = send(client, conversation(client), '帮我安排一下我接下来应该怎么弄').json()
    if kind == 'unclear':
        model.edit = lambda _, out: out.update(scope='unclear',command='',clarification='which_request')
    elif kind == 'unresolved':
        model.edit = lambda _, out: out['notes'][0].update(unresolved=True)
    elif kind == 'revoked':
        model.on_call = lambda: client.delete(URL)
    else:
        model.on_call = lambda: client.put('/api/profile',json={'food_allergies':'牛肉过敏'})
    response = understand(client,data,auto_apply=True)
    assert response.status_code == (200 if kind in ('unclear','unresolved') else 409)
    current = client.get(f"{BASE}/{data['id']}").json()
    assert current['pending'] and current['review']['status'] != 'confirmed'


def test_auto_understanding_keeps_allergy_floor_and_sticky_constraints(reviews):
    client, _, model, _ = reviews
    confirm(client)
    data = send(client, conversation(client), '我对牛肉过敏，晚餐给我搭配一下').json()
    applied = understand(client,data,auto_apply=True).json()
    assert not applied['pending'] and applied['constraints']['food_avoid'] == ['beef']
    later = send(client,applied,'晚餐吃什么').json()
    assert later['constraints']['food_avoid'] == ['beef']


@pytest.mark.parametrize('text,minutes,status', [
    ('我接下来还有三十分钟，帮我推荐今天练什么',30,200),
    ('我接下来还有半小时，帮我推荐今天练什么',30,200),
    ('我接下来还有30分钟，帮我推荐今天练什么',60,502),
    ('帮我结合最近的情况推荐今天练什么',60,502),
])
def test_auto_training_duration_must_match_source(reviews,text,minutes,status):
    client, _, model, _ = reviews
    confirm(client)
    model.edit = lambda _, out: out.update(scope='strength',command='',training={
        'source_text':text,'minutes':minutes,'time_basis':'session'})
    data = send(client,conversation(client),text).json()
    response = understand(client,data,auto_apply=True)
    assert response.status_code == status, response.text
    current = client.get(f"{BASE}/{data['id']}").json()
    assert current['pending'] == (status != 200)
