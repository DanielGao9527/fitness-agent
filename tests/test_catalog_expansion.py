import json
from dataclasses import replace

import pytest

from config import ROOT
from model.factory import ModelError
from schemas import MealPlanSelection
from services import training_catalog
from services.coach import meal_adjustment
from services.meal_nutrient_reference import amount_nutrients, reference_data
from services.meal_plans import REQUIRED, MealPlanService
from services.plan_foods import FOODS, ANIMAL_FOODS, exclusions, catalog
from services.training_recommendations import TrainingRecommendationService, equipment
from test_foundation import DAY, application, client, register, workout
from test_knowledge import library, mutate
from test_meal_plans import plans
from test_quick_meals import quick, ask, recommend as meal_recommend, current_plans, confirm
from test_training_recommendations import setup, recommend, result
from test_coach import send, BASE


@pytest.fixture
def expanded(plans):
    plans[1].state.settings = replace(plans[1].state.settings, training_plan_enabled=True)
    return plans


@pytest.mark.parametrize('restriction,blocked', [
    ('鱼过敏', {'salmon', 'cod'}), ('海鲜过敏', {'salmon', 'cod', 'shrimp'}),
    ('牛奶过敏', {'milk', 'yogurt'}), ('酸奶过敏', {'milk', 'yogurt'}),
    ('乳糖不耐受', {'milk', 'yogurt'}), ('坚果过敏', {'almonds'}),
    ('芝麻过敏', {'sesame-oil'}), ('小麦过敏', {'wholewheat-pasta'}),
    ('麸质过敏', {'wholewheat-pasta', 'barley', 'oats'}),
    ('豆类过敏', {'tofu', 'lentils', 'chickpeas', 'green-beans'}),
    ('鸡蛋过敏', {'egg', 'wholewheat-pasta'}), ('大米过敏', {'rice', 'brown-rice', 'rice-noodles'}),
])
def test_new_allergy_coverage(restriction, blocked):
    assert blocked <= set(exclusions({'food_allergies': restriction}))


def test_plant_only_unknown_and_conversation_restrictions():
    assert ANIMAL_FOODS <= set(exclusions({}, plant_only=True))
    assert {'milk', 'yogurt'} <= set(exclusions({}, temporary=['milk']))
    with pytest.raises(ModelError):
        exclusions({'food_allergies': '某些坚果但不知道具体哪种过敏'})
    with pytest.raises(ModelError):
        exclusions({}, [key for key, row in FOODS.items() if row[1] == 'protein'])


@pytest.mark.parametrize('text,group,ids', [
    ('鸡肉换成三文鱼', 'protein', ['salmon']), ('我想吃猪肉', 'protein', ['pork']),
    ('我想吃全麦意面', 'starch', ['wholewheat-pasta']), ('菠菜换成番茄', 'vegetable', ['tomato']),
    ('我想吃苹果', 'fruit', ['apple']), ('我想吃牛奶', 'dairy', ['milk']),
    ('我想吃杏仁', 'fat', ['almonds']),
])
def test_new_conversation_choices(text, group, ids):
    assert meal_adjustment(text)['group_choices'] == {group: ids}


def test_every_food_has_usable_reference_and_basis():
    data = reference_data()
    assert len(data['foods']) == len(FOODS) == 75
    assert {row['group'] for row in catalog()} == {'starch', 'protein', 'vegetable', 'fat', 'fruit', 'dairy'}
    for key, row in FOODS.items():
        assert row[3] and row[4] <= row[5]
        assert amount_nutrients(key, row[4], data)['kcal'] > 0
    assert amount_nutrients('milk', 200, data)['kcal'] == 92
    assert amount_nutrients('salmon', 100, data)['protein'] == 20.4


def test_seven_item_composition_not_seven_unrestricted_foods():
    keys = ['barley', 'salmon', 'cabbage', 'tomato', 'almonds', 'apple', 'milk']
    items = [{'food_id': key, 'lower': FOODS[key][4], 'upper': FOODS[key][4]} for key in keys]
    selection = MealPlanSelection(items=items, chunk_ids=list(REQUIRED))
    assert len(MealPlanService.validate(selection, [])) == 7
    items[2] = {'food_id': 'banana', 'lower': 80, 'upper': 80}
    with pytest.raises(ModelError):
        MealPlanService.validate(MealPlanSelection(items=items, chunk_ids=list(REQUIRED)), [])


def test_optional_group_followups_preserve_meal_and_calculate(quick):
    client, _, _, _ = quick
    first = meal_recommend(client, ask(client, message='安排午餐和晚餐')).json()
    original = next(plan for plan in current_plans(first) if plan['meal_type'] == 'dinner')
    reply = meal_recommend(client, ask(client, first, '晚餐我想吃苹果'))
    assert reply.status_code == 200, reply.text
    added = next(plan for plan in current_plans(reply.json()) if plan['meal_type'] == 'dinner')
    assert any(row['food_id'] == 'apple' for row in added['items'])
    assert [row for row in added['items'] if row['group'] != 'fruit'] == [row for row in original['items'] if row['group'] != 'fruit']
    reply = meal_recommend(client, ask(client, reply.json(), '晚餐我想吃牛奶'))
    assert reply.status_code == 200, reply.text
    updated = next(plan for plan in current_plans(reply.json()) if plan['meal_type'] == 'dinner')
    assert {'apple', 'milk'} <= {row['food_id'] for row in updated['items']}
    assert len(updated['quick_nutrition']['rows']) == len(updated['items'])
    assert client.get('/api/meals', params={'day': DAY}).json() == []


def test_new_requested_food_conflicting_with_allergy_never_called(quick):
    client, _, model, _ = quick
    client.put('/api/profile', json={'food_allergies': '鱼过敏；牛奶过敏'})
    confirm(client)
    data = ask(client, message='安排晚餐')
    data = ask(client, data, '晚餐我想吃三文鱼')
    assert meal_recommend(client, data).status_code == 409
    assert not model.messages


def test_model_cannot_silently_omit_requested_optional_group(quick, monkeypatch):
    client, _, model, _ = quick
    original = model.generate
    def omit(**kwargs):
        output = json.loads(original(**kwargs))
        output['items'] = [row for row in output['items'] if FOODS[row['food_id']][1] != 'fruit']
        return json.dumps(output)
    monkeypatch.setattr(model, 'generate', omit)
    data = ask(client, message='安排晚餐')
    response = meal_recommend(client, ask(client, data, '晚餐我想吃苹果'))
    assert response.status_code != 200
    assert len(model.messages) == 1


def test_action_catalog_and_batched_source_coverage(expanded):
    client, app, _, retriever = expanded
    actions, fingerprint = training_catalog.read_catalog()
    assert len(actions) == 89 and fingerprint != 'unavailable'
    with app.state.database.connect() as connection:
        user = connection.execute('SELECT id FROM users').fetchone()[0]
    evidence = TrainingRecommendationService(app.state.database, user, retriever).evidence()
    assert {item['source'] for item in actions} <= set(evidence)
    assert len(evidence) == 103


@pytest.mark.parametrize('focus,venue,expected', [
    ('肩', '在家，只有哑铃', {'db-shoulder', 'lateral-raise'}),
    ('肩', '在家，哑铃和稳定椅子', {'db-shoulder', 'lateral-raise', 'reverse-fly'}),
    ('二头', '在家，只有哑铃', {'biceps-curl'}),
    ('三头', '在家，只有哑铃', {'triceps-extension'}),
    ('二头和三头', '在家，只有哑铃', {'biceps-curl', 'triceps-extension'}),
    ('背', '在家，只有弹力带', {'band-row'}),
    ('二头', '在家，只有弹力带', {'band-curl'}),
    ('背', '健身房，高位下拉机和坐姿划船机', {'lat-pulldown', 'machine-row'}),
    ('胸', '健身房，推胸机', {'machine-chest'}),
    ('三头', '健身房，绳索直杆下压', {'cable-triceps'}),
    ('腿', '健身房，腿举机和腿屈伸机和俯卧腿弯举机', {'leg-press', 'bridge', 'side-adduction', 'knee-extension', 'hamstring-curl'}),
])
def test_new_actions_are_reachable_without_paid_model(expanded, focus, venue, expected):
    client, _, model, _ = expanded
    data = send(client, setup(client, equipment=venue, minutes_per_session=120), '练' + focus).json()
    rec = result(recommend(client, data))
    assert rec['status'] == 'ready', rec
    assert {item['id'] for item in rec['exercises']} == expected
    assert rec['estimated_minutes'] <= rec['available_minutes']
    assert len(rec['exercises']) <= 6 and all(item['source_id'] for item in rec['exercises'])
    assert not model.messages
    assert client.get('/api/workouts', params={'day': DAY}).json() == []


def test_new_equipment_negation_and_no_gym_assumption():
    assert equipment('健身房') == ({'floor'}, False)
    assert equipment('在家，没有哑铃和弹力带') == ({'floor'}, False)
    assert equipment('健身房，没有高位下拉机，有坐姿划船机') == ({'floor', 'row-machine'}, False)
    assert equipment('健身房，神秘机器')[1]


def test_new_source_withdrawal_and_catalog_changes_invalidate(expanded, monkeypatch, tmp_path):
    client, _, _, retriever = expanded
    data = send(client, setup(client), '练二头').json()
    assert result(recommend(client, data))['status'] == 'ready'
    mutate(retriever, lambda doc: next(s for s in doc['sources'] if s['id'] == 'mayo-biceps').update(status='withdrawn'))
    assert result(client.get(f"{BASE}/{data['id']}"))['stale']
    assert 'biceps-curl' not in {item['id'] for item in result(recommend(client, data))['exercises']}
    mutate(retriever, lambda doc: next(s for s in doc['sources'] if s['id'] == 'r118-hammer-curl').update(status='withdrawn'))
    assert result(recommend(client, data))['status'] == 'unavailable'
    path = tmp_path / 'actions.json'
    path.write_text('{bad', encoding='utf-8')
    monkeypatch.setattr(training_catalog, 'DATA_PATH', path)
    assert training_catalog.read_catalog() == ([], 'unavailable')
    assert result(recommend(client, data))['status'] == 'unavailable'


def test_actual_shoulder_work_blocks_arm_extension_and_medical_scope_stops(expanded):
    client, _, _, _ = expanded
    data = send(client, setup(client), '练三头').json()
    client.post('/api/workouts', json=workout(name='肩', status='completed'))
    assert result(recommend(client, data))['status'] == 'needs_input'
    client.put('/api/profile', json={'preferences': '孕期'})
    assert result(recommend(client, data))['status'] == 'blocked'


@pytest.mark.parametrize('query,source', [('哑铃二头弯举', 'mayo-biceps'), ('三头 臂屈伸', 'mayo-triceps'),
                                        ('高位下拉', 'mayo-lat-pulldown'), ('侧平举', 'ace-lateral-raise'),
                                        ('酸奶 低脂', 'phe-eatwell')])
def test_new_summaries_are_retrievable(library, query, source):
    assert source in {hit.source_id for hit in library.search(query)}


@pytest.mark.parametrize('kind', ['duplicate', 'resource', 'load', 'malformed'])
def test_invalid_action_catalog_fails_closed(tmp_path, monkeypatch, kind):
    doc = json.loads((ROOT / 'data/training/exercises.json').read_text(encoding='utf-8'))
    if kind == 'duplicate':
        doc['exercises'].append(doc['exercises'][0])
    elif kind == 'resource':
        doc['exercises'][0]['needs'] = ['invented-machine']
    elif kind == 'load':
        doc['exercises'][0]['loads'] = ['legs']
    path = tmp_path / 'catalog.json'
    path.write_text('bad' if kind == 'malformed' else json.dumps(doc), encoding='utf-8')
    monkeypatch.setattr(training_catalog, 'DATA_PATH', path)
    assert training_catalog.read_catalog() == ([], 'unavailable')
