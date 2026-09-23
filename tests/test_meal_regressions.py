import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from services.meal_input import meal_mentions, require_single_meal
from test_foundation import DAY, application, client, meal, register
from test_draft_flow import create, changes, confirm_body
from test_nutrition import Model, result, estimate
from test_quick_meals import quick, plans, library, ask, recommend
from test_coach import BASE, send


@pytest.mark.parametrize('text', [
    '早上吃了两个鸡蛋，中午海底捞火锅，我吃了半盘羊肉',
    '早餐：鸡蛋两个；午餐：羊肉半盘',
    '我今天早上喝了一盒奶我中午吃了一盘豆腐',
])
def test_multiple_meals_cannot_be_parsed_or_silently_committed(client, monkeypatch, text):
    register(client)
    monkeypatch.setattr('api.routes.create_meal_text_model', lambda _: pytest.fail('No model request'))
    monkeypatch.setattr('api.routes.create_nutrition_model', lambda _: pytest.fail('No estimate request'))
    draft = create(client, text=text)
    url = f"/api/meal-drafts/{draft['id']}"
    assert client.post(url + '/parse', json={'version': draft['version']}).status_code == 422
    assert client.post('/api/meal-drafts/parse-text', json={'text': text}).status_code == 422
    draft = client.put(url, json=changes(draft)).json()
    assert estimate(client, draft).status_code == 422
    failure = client.post(url + '/confirm', json=confirm_body(draft))
    assert failure.status_code == 422 and '一次只录一餐' in failure.text
    assert client.get(url).json()['text'] == text
    assert client.get('/api/meals', params={'day': DAY}).json() == []


@pytest.mark.parametrize('text,expected', [
    ('午餐吃火锅，羊肉半盘、牛肉半盘', ['lunch']),
    ('早餐没吃，中午吃了一盘豆腐', ['lunch']),
    ('早上吃了两个鸡蛋，晚上打算吃面', ['breakfast']),
    ('早餐奶一盒，饼干两片', []),
    ('两个鸡蛋和一盒牛奶', []),
])
def test_single_meal_mentions(text, expected):
    assert meal_mentions(text) == expected
    require_single_meal(text, expected[0] if expected else 'breakfast')


def test_wrong_selected_meal_blocks_before_paid_call(client, monkeypatch):
    register(client)
    monkeypatch.setattr('api.routes.create_meal_text_model', lambda _: pytest.fail('No model request'))
    draft = create(client, text='中午吃了半盘羊肉', meal_type='breakfast')
    url = f"/api/meal-drafts/{draft['id']}"
    error = client.post(url + '/parse', json={'version': draft['version']})
    assert error.status_code == 422 and '当前选择的是早餐' in error.text
    with pytest.raises(HTTPException):
        require_single_meal('午餐吃了羊肉', 'snack')


def test_unknown_retry_only_reestimates_unknown_and_preserves_success(client, monkeypatch):
    register(client)
    model = Model(json.dumps({'items': [result(0), {'index': 1, 'status': 'unknown', 'question': 'Synthetic portion question'}]}))
    monkeypatch.setattr('api.routes.create_nutrition_model', lambda _: model)
    draft = create(client, text='午餐吃火锅，我吃了半盘羊肉和半盘牛肉', meal_type='lunch')
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, items=[
        {'name': '羊肉', 'grams': None, 'amount_description': '半盘，火锅涮煮'},
        {'name': '牛肉', 'grams': None, 'amount_description': '半盘（两人共享一盘），火锅涮煮'}])).json()
    draft = estimate(client, draft).json()
    kept = draft['nutrition']['items'][0]
    model.response = json.dumps({'items': [result(0)]})
    draft = estimate(client, draft).json()
    assert model.calls == 2 and draft['nutrition']['items'][0] == kept
    assert all(item['status'] == 'estimated' for item in draft['nutrition']['items'])
    assert estimate(client, draft).json() == draft and model.calls == 2
    assert all(item['grams'] is None for item in draft['items'])
    saved = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft)).json()
    assert len(saved['records']) == 2
    assert {item['meal_type'] for item in saved['records']} == {'lunch'}


def test_unknown_intake_reason_survives_next_turn_and_restart(quick):
    client, app, model, _ = quick
    assert client.post('/api/meals', json=meal(name='Synthetic lamb', grams=None, amount_description='half plate')).status_code == 201
    data = ask(client, message='安排晚餐')
    response = data['turns'][-1]['response']
    assert '没有生成餐单' in response['text'] and 'Synthetic lamb' in response['text']
    assert response['meal_readiness']['reason'] == 'unknown_intake'
    assert recommend(client, data).status_code == 409 and not model.messages
    data = send(client, data, '今天练什么').json()
    assert data['turns'][0]['response'] == response
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"{BASE}/{data['id']}").json()['turns'][0]['response'] == response
    with TestClient(app) as other:
        register(other, 'mealother')
        assert other.get(f"{BASE}/{data['id']}").status_code == 404


def test_recommendation_resumes_after_explicit_record_estimate(quick):
    client, app, model, _ = quick
    row = client.post('/api/meals', json=meal(name='Synthetic beef')).json()
    data = ask(client)
    assert data['meal_calculation']['reason'] == 'unknown_intake'
    body = {key: value for key, value in row.items() if key != 'id'}
    body.update(kcal_per_100g=100, protein_per_100g=10, carbs_per_100g=2, fat_per_100g=5, source='Synthetic values')
    assert client.put(f"/api/meals/{row['id']}", json=body).status_code == 200
    data = client.get(f"{BASE}/{data['id']}").json()
    assert data['meal_calculation']['ready']
    response = recommend(client, data)
    assert response.status_code == 200, response.text
    assert response.json()['turns'][-1]['response']['quick_request']['status'] == 'ready'
    assert len(client.get('/api/meals', params={'day': DAY}).json()) == 1
