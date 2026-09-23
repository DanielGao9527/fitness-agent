"""Source-bound expansion, without private records or real provider requests."""
import json

import pytest

from config import ROOT
from services import training_catalog
from services.coach import meal_adjustment
from services.meal_nutrient_reference import reference_data, amount_nutrients
from services.plan_foods import FOODS, exclusions
from services.training_context import training_command
from services.training_program import session, SESSIONS
from services.training_recommendations import build, equipment, history_summary
from test_training_program import facts, evidence
from test_foundation import DAY, application, client, register
from test_knowledge import library, mutate
from test_training_plans import plans
from test_training_recommendations import setup, recommend, result
from test_coach import BASE, send

NEW_ACTIONS = [row['id'] for row in training_catalog.read_catalog()[0][48:]]


@pytest.mark.parametrize('food_id', list(FOODS)[45:])
def test_each_new_food_is_requestable_and_numerically_sourced(food_id):
    name, group, _, basis, lower, _ = FOODS[food_id]
    assert meal_adjustment('我想吃' + name)['group_choices'] == {group: [food_id]}
    assert food_id in exclusions({'food_allergies': name + '过敏'})
    data = reference_data()
    nutrients = amount_nutrients(food_id, lower, data)
    assert basis and nutrients['kcal'] > 0


@pytest.mark.parametrize('restriction,blocked', [
    ('鱼过敏', {'tuna', 'cod', 'salmon'}),
    ('鸡蛋过敏', {'egg-noodles', 'wholemeal-bread'}),
    ('牛奶过敏', {'wholemeal-bread', 'milk', 'yogurt'}),
    ('大豆过敏', {'tofu', 'soybeans', 'wholemeal-bread'}),
    ('豆类过敏', {'kidney-beans', 'peas', 'peanuts', 'soybeans'}),
    ('小麦过敏', {'wholemeal-bread', 'egg-noodles', 'couscous'}),
    ('坚果过敏', {'almonds', 'walnuts', 'cashews', 'hazelnuts', 'peanuts'}),
    ('芝麻过敏', {'sesame-oil', 'wholemeal-bread'}),
    ('禽肉过敏', {'chicken', 'duck', 'turkey'}),
])
def test_allergen_groups_expand_with_catalog(restriction, blocked):
    assert blocked <= set(exclusions({'food_allergies': restriction}))


@pytest.mark.parametrize('text,yes,no', [
    ('健身房，坐姿腿弯举机', {'seated-curl-machine'}, {'hamstring-machine'}),
    ('只用壶铃', {'kettlebell', 'floor'}, {'dumbbell', 'barbell'}),
    ('龙门架，可调滑轮，单D型把手', {'single-handle'}, {'handles', 'ankle-cuff'}),
    ('龙门架，双把手', {'handles', 'single-handle'}, {'adjustable-pulley'}),
    ('龙门架，可调滑轮，踝带，没有稳固扶手', {'ankle-cuff'}, {'stable-support'}),
    ('史密斯机，没有史密斯安全限位', {'smith-machine'}, {'smith-stops'}),
    ('史密斯机，史密斯安全限位', {'smith-machine', 'smith-stops'}, {'rack', 'safeties'}),
    ('环形弹力带，稳固扶手', {'loop-band', 'stable-support'}, {'band', 'pullup-band'}),
    ('弹力带', {'band'}, {'loop-band', 'pullup-band'}),
    ('单杠，引体辅助弹力带', {'pullup-bar', 'pullup-band'}, {'loop-band'}),
    ('腿举机', {'leg-press-machine'}, {'calf-platform'}),
])
def test_explicit_attachments_and_negations(text, yes, no):
    resources, uncertain = equipment(text)
    assert not uncertain
    assert yes <= resources and not no & resources


@pytest.mark.parametrize('action_id', NEW_ACTIONS)
def test_each_new_action_is_reachable_and_replacement_checked(action_id):
    catalog, _ = training_catalog.read_catalog()
    action = next(row for row in catalog if row['id'] == action_id)
    profile = facts()['profile']
    e = evidence()
    # Try real session slots with only this action's declared capabilities.
    selected = None
    for key in SESSIONS:
        plan = session(key, catalog, action['needs'], e, profile, set(), 120)
        for row in plan['exercises']:
            if action_id == row['id'] or action_id in {other['id'] for other in row['alternatives']}:
                selected = session(key, catalog, action['needs'], e, profile, set(), 120,
                                   {row['id']: action_id})
                break
        if selected:
            break
    assert selected, action_id
    assert action_id in {row['id'] for row in selected['exercises']}
    assert action['source'] in e
    assert action['focus'] in history_summary({'day': DAY, 'completed': [
        {'day': DAY, 'name': action['name'], 'minutes': 20}]})['recent_loads']
    del e[action['source']]
    without = session(key, catalog, action['needs'], e, profile, set(), 120)
    assert action_id not in {row['id'] for row in without['exercises']}
    assert action_id not in {alt['id'] for row in without['exercises'] for alt in row['alternatives']}


def test_alternatives_not_truncated_to_first_three():
    catalog, _ = training_catalog.read_catalog()
    resources = set().union(*(row['needs'] for row in catalog))
    plan = session('triceps', catalog, resources, evidence(), facts()['profile'], set(), 60)
    assert len(plan['exercises'][0]['alternatives']) >= 4
    assert 'db-kickback' in {row['id'] for row in plan['exercises'][0]['alternatives']}


@pytest.mark.parametrize('focus,venue,expected', [
    ('臀外侧', '只用龙门架，可调滑轮，踝带，稳固扶手', 'cable-abduction'),
    ('核心抗旋转', '在家，徒手', 'bird-dog'),
    ('分腿蹲', '在家，哑铃，平板凳', 'bulgarian-squat'),
])
def test_special_directions_use_new_actions(focus, venue, expected):
    f = facts()
    f['profile']['equipment'] = venue
    f['request'] = training_command('练' + focus)
    rec = build(f, evidence())
    assert expected in {row['id'] for row in rec['exercises']}, rec


@pytest.mark.parametrize('split,source', [
    ('ppl', 'r123-ethier-ppl:structure'),
    ('four', 'r123-nasm-splits:structure'),
    ('five', 'r123-nasm-splits:structure'),
])
def test_systems_actually_bound_to_evidence(split, source):
    f = facts(training_split=split)
    e = evidence()
    rec = build(f, e)
    framework = rec['structure']
    assert framework['split'] == split and source in framework['source_ids']
    assert source in {row['chunk_id'] for row in rec['sources']}
    source_framework = training_catalog.read_systems()[0][split]
    assert source_framework['adaptation'] and source_framework['original_structure']
    del e[source]
    rec = build(f, e)
    assert rec['status'] == 'unavailable' and not rec['exercises']


@pytest.mark.parametrize('kind', ['malformed', 'duplicate', 'missing_principles', 'oversize'])
def test_invalid_systems_fail_closed(tmp_path, monkeypatch, kind):
    doc = json.loads(training_catalog.SYSTEMS_PATH.read_text(encoding='utf-8'))
    if kind == 'duplicate':
        doc['systems'][0] = doc['systems'][1]
    if kind == 'missing_principles':
        doc['systems'][1]['source_ids'] = ['unknown:source']
    raw = 'bad' if kind == 'malformed' else ' ' * 30_001 if kind == 'oversize' else json.dumps(doc)
    path = tmp_path / 'systems.json'
    path.write_text(raw, encoding='utf-8')
    monkeypatch.setattr(training_catalog, 'SYSTEMS_PATH', path)
    assert training_catalog.read_systems() == ({}, 'unavailable')
    assert build(facts(), evidence())['status'] == 'unavailable'


def test_local_evidence_preserves_public_and_curated_provenance(library):
    systems, _ = training_catalog.read_systems()
    for system in systems.values():
        assert {h.chunk_id for h in library.get_chunks(system['source_ids'], topic='training')} == set(system['source_ids'])
    corpus = json.loads(library.source_path.read_text(encoding='utf-8'))
    public = [s for s in corpus['sources'] if s.get('origin','public_web')=='public_web']
    assert public and all(source['url'].startswith('https://') for source in public)
    notes = [s for s in corpus['sources'] if s.get('origin')=='curated_text']
    assert len(notes) == 3
    for source in notes:
        assert not source['url'] and not source['license_url'] and '未核实' in source['publisher']
        chunks = [f"{source['id']}:{section['id']}" for section in source['sections']]
        assert {hit.chunk_id for hit in library.get_chunks(chunks, topic='training')} == set(chunks)
    assert not library.get_chunks(['unpublished-series:structure'], topic='training')


@pytest.mark.parametrize('change', [{'status': 'withdrawn'}, {'reviewed_on': '2025-09-20', 'review_due': '2026-03-19'}])
def test_system_update_stales_saved_result_and_withdrawal_blocks(plans, tmp_path, monkeypatch, change):
    client, _, model, retriever = plans
    path = tmp_path / 'systems.json'
    path.write_bytes(training_catalog.SYSTEMS_PATH.read_bytes())
    monkeypatch.setattr(training_catalog, 'SYSTEMS_PATH', path)
    data = setup(client, training_split='ppl', training_days=3)
    assert result(recommend(client, data))['structure']['split'] == 'ppl'
    doc = json.loads(path.read_text(encoding='utf-8'))
    doc['systems'][2]['adaptation'] += ' 复核标记。'
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    assert result(client.get(f"{BASE}/{data['id']}"))['stale']
    mutate(retriever, lambda d: next(s for s in d['sources'] if s['id'] == 'r123-ethier-ppl').update(**change))
    assert result(recommend(client, data))['status'] == 'unavailable'
    assert not model.messages
    assert client.get('/api/workouts', params={'day': DAY}).json() == []
