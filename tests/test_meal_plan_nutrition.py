import json
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.meal_plans as plan_api
from app import create_app
from model.factory import ModelError
from schemas import PlanNutritionRequest
from services.meal_plan_nutrition import MealPlanNutritionService
from test_foundation import DAY, PASSWORD, application, client, meal, register
from test_knowledge import library, mutate
from test_knowledge_answers import calls
from test_meal_plans import plans, generate, accept

URL = '/api/meal-plans/nutrition'


class EstimateModel:
    def __init__(self):
        self.messages, self.output, self.on_call = [], None, None

    def generate(self, *, system_prompt, message):
        assert '尚未吃过' in system_prompt and '完整区间' in system_prompt
        value = json.loads(message)
        self.messages.append(value)
        if self.on_call:
            self.on_call()
        if isinstance(self.output, Exception):
            raise self.output
        if self.output is not None:
            return self.output(value)
        return json.dumps({'items': [estimate(item['index']) for item in value['items']]})


def estimate(index, **changes):
    return {'index': index, 'status': 'estimated', 'kcal': {'lower': 90, 'upper': 120},
            'protein': {'lower': 2, 'upper': 3}, 'carbs': {'lower': 8, 'upper': 10},
            'fat': {'lower': 1, 'upper': 2}, 'assumptions': ['Synthetic full portion interval'], **changes}


@pytest.fixture
def checked(plans, monkeypatch):
    client, app, _, source = plans
    app.state.settings = replace(app.state.settings, nutrition_enabled=True)
    model = EstimateModel()
    monkeypatch.setattr(plan_api, 'create_nutrition_model', lambda settings: model)
    plan = generate(client).json()
    return client, app, model, source, plan


def body(*plans):
    return {'client_id': str(uuid4()), 'items': [{'plan_id': p['id'], 'version': p.get('nutrition_review', {}).get('version', 0)} for p in plans]}


def post(client, *plans):
    return client.post(URL, json=body(*plans))


def latest(client, plan):
    return next(p for p in client.get(f"/api/meal-plans?day={plan['day']}").json() if p['id'] == plan['id'])


def test_complete_scope_privacy_persistence_and_no_writes(checked):
    client, app, model, _, plan = checked
    request = body(plan)
    response = client.post(URL, json=request)
    assert response.status_code == 200, response.text
    result = latest(client, plan)['nutrition_review']
    assert result['version'] == 1 and result['status'] == 'ready'
    assert result['result']['totals']['kcal'] == {'lower': 270, 'upper': 360}
    assert result['result']['independently_verified'] is False
    sent = model.messages[0]
    assert set(sent) == {'items'}
    for food, item in zip(sent['items'], plan['items']):
        assert set(food) == {'index', 'name', 'grams', 'amount_description'}
        assert food['grams'] is None and item['basis'] in food['amount_description']
        assert f"{item['lower']}{item['unit']}至{item['upper']}{item['unit']}" in food['amount_description']
    assert client.post(URL, json=request).status_code == 200
    assert post(client, latest(client, plan)).status_code == 200
    assert len(model.messages) == 1 and calls(app) == 2
    assert accept(client, plan).status_code == 200
    assert client.post(URL, json=request).status_code == 200
    assert client.get(f'/api/meals?day={DAY}').json() == []
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD})
        assert latest(restarted, plan)['nutrition_review'] == result


def test_batch_reserved_upfront_and_unchanged_meal_reused(checked):
    client, app, model, _, dinner = checked
    lunch = generate(client, meal_type='lunch').json()
    breakfast = generate(client, meal_type='breakfast').json()
    request = body(breakfast, lunch, dinner)
    assert client.post(URL, json=request).status_code == 200
    assert [len(m['items']) for m in model.messages] == [5, 4]
    assert calls(app) == 5
    for plan in (breakfast, lunch, dinner):
        assert latest(client, plan)['nutrition_review']['result']['totals']['kcal']['lower'] == 270
    assert client.post(URL, json=request).status_code == 200 and len(model.messages) == 2
    replacement = generate(client).json()
    assert post(client, latest(client, lunch), replacement).status_code == 200
    assert len(model.messages) == 3 and len(model.messages[-1]['items']) == 3
    assert latest(client, lunch)['nutrition_review']['version'] == 1


@pytest.mark.parametrize('case', ['unknown', 'zero'])
def test_unknown_not_zero(checked, case):
    client, _, model, _, plan = checked
    model.output = lambda value: json.dumps({'items': [
        {'index': item['index'], 'status': 'unknown', 'question': 'Synthetic unknown'} if case == 'unknown' and item['index'] == 0
        else estimate(item['index'], kcal={'lower': 0, 'upper': 0}) for item in value['items']]})
    assert post(client, plan).status_code == 200
    result = latest(client, plan)['nutrition_review']['result']
    assert result['unknown_count'] == (1 if case == 'unknown' else 0)
    assert result['totals'] is None if case == 'unknown' else result['totals']['kcal'] == {'lower': 0, 'upper': 0}


@pytest.mark.parametrize('change', ['profile', 'record', 'target', 'revoke', 'source', 'new_plan'])
def test_changes_during_call_reject_late_estimates(checked, change):
    from test_intake_targets import save
    from test_meal_intake import review, revoke
    client, app, model, source, old = checked
    save(client)
    reviewed = review(client).json()
    plan = generate(client, intake_context_hash=reviewed['context_hash']).json()
    def changed():
        if change == 'profile': client.put('/api/profile', json={'weight_kg': 72})
        elif change == 'record': client.post('/api/meals', json=meal())
        elif change == 'target': save(client, kcal=2200)
        elif change == 'revoke': revoke(client)
        elif change == 'new_plan': generate(client, intake_context_hash=reviewed['context_hash'])
        else: mutate(source, lambda value: next(item for item in value['sources'] if item['id'] == 'phe-eatwell').update(status='withdrawn'))
    model.on_call = changed
    response = post(client, plan)
    assert response.status_code in (409, 503), response.text
    assert latest(client, plan)['nutrition_review']['status'] == 'failed'
    assert 'result' not in latest(client, plan)['nutrition_review']


def test_failure_retry_and_old_replay_cannot_bill_again(checked):
    client, app, model, _, plan = checked
    first = body(plan)
    model.output = ModelError('MODEL_TIMEOUT', 'Synthetic timeout', 504)
    assert client.post(URL, json=first).status_code == 504
    failed = latest(client, plan)
    assert failed['items'] == plan['items'] and failed['nutrition_review']['status'] == 'failed'
    assert client.post(URL, json=first).status_code == 409
    model.output = None
    second = body(failed)
    assert client.post(URL, json=second).status_code == 200
    assert client.post(URL, json=first).status_code == 409
    assert len(model.messages) == 2 and calls(app) == 3


def test_second_batch_failure_is_atomic(checked):
    client, _, model, _, dinner = checked
    lunch = generate(client, meal_type='lunch').json()
    def fail_later():
        if len(model.messages) == 2:
            raise ModelError('MODEL_TIMEOUT', 'Synthetic second batch', 504)
    model.on_call = fail_later
    assert post(client, lunch, dinner).status_code == 504
    for plan in (lunch, dinner):
        review = latest(client, plan)['nutrition_review']
        assert review['status'] == 'failed' and 'result' not in review


def test_concurrent_attempt_and_expired_inflight_revision(checked):
    client, app, model, source, plan = checked
    request = body(plan)
    def during():
        assert client.post(URL, json=request).status_code == 409
        assert post(client, latest(client, plan)).status_code == 409
    model.on_call = during
    assert client.post(URL, json=request).status_code == 200
    assert len(model.messages) == 1
    with app.state.database.connect() as c:
        row = c.execute('SELECT payload FROM meal_plans WHERE id=?', (plan['id'],)).fetchone()
        payload = json.loads(row[0])
        payload['nutrition_review'].update(status='generating', started_at=time.time()-181)
        c.execute('UPDATE meal_plans SET payload=? WHERE id=?', (json.dumps(payload), plan['id']))
    model.on_call = None
    assert post(client, latest(client, plan)).status_code == 200
    assert latest(client, plan)['nutrition_review']['version'] == 2


def test_superseded_inflight_result_does_not_overwrite(checked):
    client, app, model, source, plan = checked
    service = MealPlanNutritionService(app.state.database, client.get('/api/auth/me').json()['id'], source)
    def during():
        with app.state.database.connect() as c:
            payload = json.loads(c.execute('SELECT payload FROM meal_plans WHERE id=?',(plan['id'],)).fetchone()[0])
            payload['nutrition_review']['started_at'] = time.time()-181
            c.execute('UPDATE meal_plans SET payload=? WHERE id=?', (json.dumps(payload), plan['id']))
        service.estimate(PlanNutritionRequest(**body(latest(client, plan))), lambda count: EstimateModel(), 'synthetic')
    model.on_call = during
    assert post(client, plan).status_code == 409
    final = latest(client, plan)['nutrition_review']
    assert final['status'] == 'ready' and final['version'] == 2


@pytest.mark.parametrize('case', ['bad_json', 'duplicate', 'missing', 'reversed', 'mass', 'energy'])
def test_invalid_outputs_do_not_change_plan(checked, case):
    client, _, model, _, plan = checked
    def output(value):
        rows = [estimate(item['index']) for item in value['items']]
        if case == 'bad_json': return 'not JSON'
        if case == 'duplicate': rows[-1]['index'] = 0
        if case == 'missing': rows.pop()
        if case == 'reversed': rows[0]['kcal'] = {'lower': 10, 'upper': 1}
        if case == 'mass': rows[0]['protein'] = {'lower': 999, 'upper': 1000}
        if case == 'energy': rows[0]['kcal'] = {'lower': 9999, 'upper': 99999}
        return json.dumps({'items': rows})
    model.output = output
    assert post(client, plan).status_code == 502
    assert latest(client, plan)['items'] == plan['items']
    assert latest(client, plan)['nutrition_review']['status'] == 'failed'


@pytest.mark.parametrize('change', [{'user_id': 1}, {'items': []}, {'client_id': 'fake'}])
def test_strict_body(checked, change):
    client, _, model, _, plan = checked
    assert client.post(URL, json={**body(plan), **change}).status_code == 422
    assert not model.messages


def test_auth_scope_version_origin_budget_and_rebinding(checked):
    client, app, model, _, plan = checked
    request = body(plan)
    with TestClient(app) as other:
        assert other.post(URL, json=request).status_code == 401
        register(other, 'bob')
        assert other.post(URL, json=request).status_code == 404
    assert client.post(URL, json=request, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(URL, json={**request, 'items': request['items']*2}).status_code == 422
    assert client.post(URL, json={**request, 'items': [{'plan_id':plan['id'],'version':True}]}).status_code == 422
    later = generate(client, day='2026-09-16', meal_type='lunch').json()
    assert post(client, plan, later).status_code == 422
    stale = generate(client).json()
    assert post(client, plan).status_code == 409
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=0)
    assert post(client, stale).status_code == 429
    assert not model.messages
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=30)
    request = body(latest(client, stale))
    assert client.post(URL, json=request).status_code == 200
    request['items'][0]['version'] += 1
    assert client.post(URL, json=request).status_code == 409


def test_unconfigured_keeps_plan(checked, monkeypatch):
    client, _, _, _, plan = checked
    def unavailable(settings): raise ModelError('MODEL_NOT_CONFIGURED', 'Not configured', 503)
    monkeypatch.setattr(plan_api, 'create_nutrition_model', unavailable)
    assert post(client, plan).status_code == 503
    assert latest(client, plan)['items'] == plan['items']
