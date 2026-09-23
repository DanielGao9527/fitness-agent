import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from model.qwen import provider_error
from services.business_time import business_today
from services.meal_plans import meal_restrictions
from services.plan_foods import ON_REQUEST_FOODS, MEAT
from test_foundation import application, client, register
from test_profile_body_sync import configured, state, measure, profile, current
from test_body_measurements import update_body
from test_meal_balance import sample, balance, item, assert_within
from test_meal_plans import plans, generate, library
from test_quick_meals import quick, ask, recommend, current_plans


def training(**changes):
    return {"client_id": str(uuid4()), "day": business_today().isoformat(), "name": "游泳", "minutes": 30,
            "status": "completed", "weight_kg": 85, "sync_weight": True, **changes}


def save(client, **changes):
    result = client.post('/api/workouts', json=training(**changes))
    assert result.status_code == 201, result.text
    return result.json()


def weight(client):
    return client.get('/api/profile').json()['weight_kg']


def edit(row, **changes):
    return {key: value for key, value in {**row, **changes}.items()
            if key not in ('id', 'calorie_estimate')}


def test_saved_weight_syncs_formula_without_fake_measurement_and_persists(client, application):
    configured(client)
    before = state(client)
    row = save(client)
    after = state(client)
    assert weight(client) == 85
    assert after['target']['kcal'] > before['target']['kcal']
    assert current(client)['review']['measurement_days'] == 0
    assert current(client)['current_body']['weight_kg']['source'] == 'workout'
    assert not any(key.startswith('_') for key in row)
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert weight(restarted) == 85
        restarted.cookies.clear()
        register(restarted, 'bob')
        assert weight(restarted) is None
        assert restarted.put(f"/api/workouts/{row['id']}", json=edit(row, weight_kg=95, sync_weight=True)).status_code == 404
        assert restarted.delete(f"/api/workouts/{row['id']}").status_code == 404
    assert weight(client) == 85


def test_default_estimation_weight_not_synced_without_explicit_edit(client):
    configured(client)
    previous = weight(client)
    save(client, sync_weight=False)
    assert weight(client) == previous


def test_replay_and_unrelated_edit_do_not_overwrite_newer_manual_value(client):
    configured(client)
    request = training()
    row = client.post('/api/workouts', json=request).json()
    profile(client, weight_kg=88)
    version = state(client)['version']
    assert client.post('/api/workouts', json=request).status_code == 201
    assert client.put(f"/api/workouts/{row['id']}", json=edit(row, notes='备注', sync_weight=True)).status_code == 200
    assert weight(client) == 88 and state(client)['version'] == version


def test_old_backfill_does_not_override_profile_or_latest_measurement(client):
    configured(client)
    measurement = measure(client, weight_kg=82, body_fat_percent=21)
    save(client, day=(business_today()-timedelta(days=7)).isoformat())
    assert weight(client) == 82
    save(client)
    assert weight(client) == 85
    assert client.put(f"/api/body-measurements/{measurement['id']}", json=update_body(measurement, weight_kg=86)).status_code == 200
    assert weight(client) == 86
    assert client.get('/api/profile').json()['body_fat_percent'] == 21


def test_correction_delete_and_clear_reproject_weight(client):
    configured(client)
    previous = weight(client)
    row = save(client)
    updated = client.put(f"/api/workouts/{row['id']}", json=edit(row, weight_kg=86, sync_weight=True))
    assert updated.status_code == 200 and weight(client) == 86
    assert client.put(f"/api/workouts/{row['id']}", json=edit(row, weight_kg=None)).status_code == 200
    assert weight(client) == previous
    second = save(client)
    assert client.delete(f"/api/workouts/{second['id']}").status_code == 204
    assert weight(client) == previous


@pytest.mark.parametrize('changes', [{'weight_kg': None}, {'status': 'planned'}, {'sync_weight': 'true'},
                                   {'day': (business_today()+timedelta(days=1)).isoformat()},
                                   {'_body_weight_saved_at': '2099-01-01'}])
def test_invalid_weight_sync_is_atomic(client, changes):
    configured(client)
    previous = weight(client)
    assert client.post('/api/workouts', json=training(**changes)).status_code == 422
    assert weight(client) == previous
    assert client.get('/api/workouts', params={'day': business_today().isoformat()}).json() == []


def test_batch_single_weight_one_target_update_and_conflict_rolls_back(client):
    configured(client)
    version = state(client)['version']
    items = [training(name='游泳'), training(name='胸和三头')]
    result = client.post('/api/workouts/batch', json={'items': items})
    assert result.status_code == 201, result.text
    assert weight(client) == 85 and state(client)['version'] == version + 1
    profile(client, weight_kg=87)
    assert client.post('/api/workouts/batch', json={'items': items}).status_code == 201
    assert weight(client) == 87
    conflict = [training(weight_kg=90), {**items[0], 'name': '冲突', 'sync_weight': False}]
    assert client.post('/api/workouts/batch', json={'items': conflict}).status_code == 409
    assert weight(client) == 87
    assert len(client.get('/api/workouts', params={'day': items[0]['day']}).json()) == 2
    assert client.post('/api/workouts/batch', json={'items': [training(), training(weight_kg=90)]}).status_code == 422


@pytest.mark.parametrize('status,code,word', [(500, None, '服务暂时异常'), (503, 'InvalidParameter', '服务暂时异常'),
    (400, 'InvalidParameter', '请求参数'), (400, 'model_not_found', '型号或接口'),
    (404, None, '型号或接口'), (403, None, '鉴权'), (429, None, '调用受限'),
    (400, 'data_inspection_failed', '未接受本次内容'), (402, None, '计费'),
    (400, 'Arrearage', '计费'), (302, None, '异常响应')])
def test_failure_categories_never_log_private_body(status, code, word, caplog):
    private = 'PRIVATE-KEY-AND-FOOD'
    response = httpx.Response(status, json={'error': {'code': code, 'message': private}, 'request_id': private})
    error = provider_error(response)
    assert word in str(error) and '故障编号' in str(error)
    assert private not in str(error) + caplog.text
    assert f'http={status}' in caplog.text


@pytest.mark.parametrize('body', [[], None, 'secret', {'error': []}, {'error': {'code': ['secret']}}, {'code': 'secret'}])
def test_unknown_provider_envelope_is_redacted(body, caplog):
    error = provider_error(httpx.Response(400, json=body))
    assert 'secret' not in str(error) + caplog.text


def test_rare_ingredients_excluded_from_model_and_default_choices(plans):
    client, _, model, _ = plans
    assert generate(client).status_code == 200
    allowed = {food['id'] for food in model.messages[-1]['allowed_foods']}
    assert not allowed & ON_REQUEST_FOODS
    assert {'rice', 'pork', 'egg', 'tofu', 'corn', 'sweet-potato', 'pakchoi'} <= allowed


@pytest.mark.parametrize('choices', [{}, {'protein': sorted(MEAT)}, {'starch': ['couscous']}])
def test_explicit_name_not_broad_meat_can_allow_rare_food(choices):
    facts = {'profile': {}, 'coach': {'meal_context': {'group_choices': choices}}}
    blocked, _, _ = meal_restrictions(facts, {})
    assert ('couscous' not in blocked) == (choices == {'starch': ['couscous']})
    assert 'turkey' in blocked
    facts['profile']['food_allergies'] = '小麦过敏'
    if choices == {'starch': ['couscous']}:
        from model.factory import ModelError
        with pytest.raises(ModelError):
            meal_restrictions(facts, {})


def test_joint_optimizer_cannot_reintroduce_rare_foods():
    plans, info, data = sample()
    blocked, _, _ = meal_restrictions({'profile': {}}, {})
    for plan in plans:
        plan['items'][0] = item('oats', 80)
        plan['excluded_foods'] = blocked
    result, _ = balance.balance(plans, {'lunch', 'dinner'}, info, data)
    assert_within(result, info, data)
    assert not {food['food_id'] for plan in result for food in plan['items']} & ON_REQUEST_FOODS


def test_no_common_protein_fails_before_request():
    from services.plan_foods import FOODS
    from model.factory import ModelError
    temporary = [key for key, food in FOODS.items() if food[1] == 'protein' and key != 'turkey']
    with pytest.raises(ModelError) as error:
        meal_restrictions({'profile': {}}, {'excluded_foods': temporary})
    assert error.value.code == 'PLAN_OPTIONS_INSUFFICIENT'


def test_full_meal_addition_has_clear_limit_without_extra_model_call(quick):
    client, app, model, _ = quick
    first = recommend(client, ask(client, message='安排晚餐')).json()
    original = current_plans(first)[0]
    # This tests the item-count boundary, not which foods a changing optimizer selects.
    import json
    from test_meal_balance import item
    from services import meal_nutrient_reference as nutrients
    with app.state.database.connect() as connection:
        payload = json.loads(connection.execute('SELECT payload FROM meal_plans WHERE id=?', (original['id'],)).fetchone()[0])
        payload['items'] = [item(key, amount) for key, amount in [('rice', 200), ('corn', 150), ('chicken', 200),
                            ('spinach', 150), ('broccoli', 150), ('olive-oil', 10), ('walnuts', 25)]]
        old = payload['quick_nutrition']
        payload['quick_nutrition'] = nutrients.nutrition_result(payload['items'], old['portion_reference'], old['allocation_note'], old['context'], nutrients.reference_data())
        connection.execute('UPDATE meal_plans SET payload=? WHERE id=?', (json.dumps(payload), original['id']))
    calls = len(model.messages)
    response = recommend(client, ask(client, first, '晚餐我想吃牛奶'))
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'PLAN_ITEM_LIMIT'
    assert len(model.messages) == calls
