from datetime import date
from uuid import uuid4

import pytest

from schemas import Profile
from services.training_catalog import read_catalog, read_systems
from services.training_context import merge_training, training_command
from services.training_program import ROTATIONS, session
from services.training_recommendations import REQUIRED, build, equipment, history_summary
from test_foundation import DAY, PASSWORD, application, client, register, workout
from test_knowledge import library, mutate
from test_training_plans import plans
from test_training_recommendations import setup, recommend, result
from test_coach import BASE, conversation, send


VENUE = '哑铃，可调训练凳，龙门架，双侧可调滑轮，双把手，绳索把手，直杆把手，杠铃，深蹲架，保护杠，高位下拉机，俯卧腿弯举机，腿屈伸机，单杠'


def facts(**changes):
    profile = Profile(equipment=VENUE, experience='experienced', training_days=5,
                      minutes_per_session=60, **changes).model_dump()
    return {'day': DAY, 'profile': profile, 'request': {}, 'constraints': {},
            'history': {'day': DAY, 'window_start': '2026-09-09', 'completed': []}}


def evidence():
    actions, _ = read_catalog()
    systems, _ = read_systems()
    return {key: {'chunk_id': key, 'url': 'https://www.cdc.gov/', 'title': key, 'publisher': 'Fixture'}
            for key in {*REQUIRED, *[item['source'] for item in actions],
                        *[key for system in systems.values() for key in system['source_ids']]}}


@pytest.mark.parametrize('split,days', [('ppl', 3), ('four', 4), ('five', 5)])
def test_each_structure_starts_today_without_hypothetical_week(split, days):
    f = facts(training_split=split)
    f['profile']['training_days'] = days
    rec = build(f, evidence())
    assert 'program' not in rec
    assert rec['structure']['split'] == split
    assert rec['focus'] == ROTATIONS[split][0]
    assert rec['exercises'] and rec['estimated_minutes'] <= 60


@pytest.mark.parametrize('text,present,absent', [
    ('龙门架，双侧可调滑轮，双把手', {'cable','dual-pulley','adjustable-pulley','handles'}, {'bench'}),
    ('杠铃，没有卧推架和保护杠', {'barbell'}, {'rack','safeties'}),
    ('可调训练凳，没有上斜凳', {'bench'}, {'incline-bench'}),
    ('龙门架，没有双侧可调滑轮', {'cable'}, {'dual-pulley','adjustable-pulley'}),
    ('在家，只有哑铃，没有内收机', {'floor','dumbbell'}, {'adductor-machine'}),
    ('健身房', {'floor'}, {'dumbbell','barbell','cable'}),
])
def test_capabilities_respect_explicit_absence(text, present, absent):
    found, uncertain = equipment(text)
    assert not uncertain
    assert present <= found and not absent & found


def test_no_bench_or_safeties_invented_and_unsupported_device_has_gap():
    f = facts()
    f['profile']['equipment'] = '只有杠铃'
    f['request'] = {'focus': '胸'}
    rec = build(f, evidence())
    assert not any(item['id'] == 'barbell-bench' for item in rec['exercises'])
    f['profile']['equipment'] = '只用龙门架'
    rec = build(f, evidence())
    assert not rec['exercises']
    f['profile']['equipment'] = '只用龙门架，双侧可调滑轮，双把手'
    rec = build(f, evidence())
    assert [item['id'] for item in rec['exercises']] == ['cable-fly']
    assert rec['gaps']


def test_adduction_without_machine_is_not_replaced_by_squat():
    f = facts()
    f['profile']['equipment'] = '在家，只有哑铃，没有内收机'
    f['request'] = {'focus': '内收肌'}
    rec = build(f, evidence())
    assert [item['id'] for item in rec['exercises']] == ['side-adduction']
    assert rec['exercises'][0]['target'] == '大腿内侧'


def test_swaps_require_same_pattern_resources_and_live_evidence():
    f = facts()
    f['request'] = {'focus': '胸', 'replacements': {'db-bench': 'barbell-bench'}}
    rec = build(f, evidence())
    assert rec['exercises'][0]['id'] == 'barbell-bench'
    f['request']['replacements'] = {'db-bench': 'goblet-squat'}
    rec = build(f, evidence())
    assert not {'db-bench', 'goblet-squat'} & {i['id'] for i in rec['exercises']}
    assert any('替代未通过' in gap for gap in rec['gaps'])
    f['request']['replacements'] = {'db-bench': 'barbell-bench'}
    e = evidence()
    del e['r118-barbell-bench:basics']
    assert 'barbell-bench' not in {i['id'] for i in build(f, e)['exercises']}


def test_chain_swap_and_exact_commands_do_not_lose_unrelated_context():
    current = {'focus':'胸', 'minutes':40, 'replacements':{'db-bench':'barbell-bench'}}
    cmd = training_command('把杠铃平板卧推换成哑铃地板推胸', continuing=True)
    merged = merge_training(current, cmd)
    assert merged['replacements'] == {'db-bench':'floor-press'}
    assert merged['minutes'] == 40 and merged['focus'] == '胸'
    merged = merge_training(merged, training_command('改成三分化'))
    assert 'focus' not in merged and 'replacements' not in merged and merged['split'] == 'ppl'
    assert training_command('改成三分化，但我肩受伤了') is None
    assert training_command('只用哑铃，肩疼', continuing=True) is None


@pytest.mark.parametrize('minutes', [15,20,30,40,60])
def test_time_budget_and_explicit_missing_slots(minutes):
    f = facts()
    f['request'] = {'focus': '腿', 'minutes':minutes, 'time_basis':'session'}
    rec = build(f, evidence())
    assert rec['estimated_minutes'] <= minutes
    assert 10 + 3 * sum(item['sets'] for item in rec['exercises']) == rec['estimated_minutes']
    if minutes < 28:
        assert any('时间不足' in gap for gap in rec['gaps'])


def test_insufficient_days_and_withdrawn_program_principles_fail_closed():
    f = facts(training_split='five')
    f['profile']['training_days'] = 3
    assert build(f, evidence())['status'] == 'ready'
    e = evidence()
    del e['acsm-2026:programming']
    assert build(f, e)['status'] == 'unavailable'


def test_recorded_loads_not_draft_and_leg_curl_not_arm_curl():
    f = facts(training_split='ppl')
    f['history']['completed'] = [{'day':DAY, 'name':'胸和肩', 'minutes':30, 'details':''}]
    rec = build(f, evidence())
    assert not any(set(item['loads']) & {'chest','shoulders','arms'} for item in rec['exercises'])
    assert history_summary({'day':DAY, 'completed':[{'day':DAY,'name':'器械俯卧腿弯举','minutes':20}]})['recent_loads'] == ['legs']
    before = build(f, evidence())
    assert build(f, evidence()) == before


def test_every_action_is_legible_has_valid_source_and_equipment():
    actions, fingerprint = read_catalog()
    assert len(actions) == 89 and fingerprint != 'unavailable'
    assert len({row['id'] for row in actions}) == 89
    for row in actions:
        assert '\ufffd' not in str(row)
        assert row['source'] in evidence() and row['focus'] in row['loads']


def test_profile_setting_old_clients_and_invalid_values(plans):
    client, _, _, _ = plans
    assert client.put('/api/profile', json={'training_split':'four','training_days':4}).status_code == 200
    assert client.put('/api/profile', json={'equipment':'哑铃'}).json()['training_split'] == 'four'
    assert client.put('/api/profile', json={'training_split':'anything'}).status_code == 422
    assert client.put('/api/profile', json={'training_split':None}).status_code == 422


def test_program_is_private_idempotent_and_stale_when_settings_or_sources_change(plans):
    client, app, model, retriever = plans
    data = setup(client, training_split='five', training_days=5, minutes_per_session=60, equipment=VENUE)
    token = str(uuid4())
    first = recommend(client, data, client_id=token)
    assert result(first)['structure']['split'] == 'five'
    assert 'program' not in result(first)
    assert recommend(client, data, client_id=token).json() == first.json()
    assert client.get('/api/workouts', params={'day':DAY}).json() == []
    assert client.get('/api/training-plans', params={'day':DAY}).json() == []
    mutate(retriever, lambda d: next(s for s in d['sources'] if s['id']=='acsm-2026').update(status='withdrawn'))
    assert result(client.get(f"{BASE}/{data['id']}"))['stale']
    assert recommend(client, data, client_id=token).status_code == 409
    assert result(recommend(client, data))['status'] == 'unavailable'
    assert not model.messages
    client.cookies.clear()
    register(client, 'second')
    assert client.get(f"{BASE}/{data['id']}").status_code == 404
