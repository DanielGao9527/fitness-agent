"""Replay fixed dev responses; never call a provider or alter the frozen labels."""
import json
from pathlib import Path

import pytest

from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_meal_plans import plans
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_equipment_memory_scopes import scoped, draft, advice
from test_training_recommendations import recommend


def test_complete_exclusion_clause_survives_confirmation_and_final_advice(scoped):
    client, app, _, _ = scoped
    value = '只有瑜伽垫，其他器械都没有'
    _, response = draft(scoped, message='问的是家里的训练条件：' + value + '。',
                        value=value, scope='unspecified')
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['equipment_memory']['value'] == value
    saved = confirm(client, ready, equipment_scope='session')
    assert saved.status_code == 200, saved.text
    recommendation = recommend(client, saved.json())
    assert recommendation.status_code == 200, recommendation.text
    final = recommendation.json()['turns'][-1]['response']['training_recommendation']
    assert final['status'] == 'ready' and final['equipment'] == value
    assert final['equipment_source'] == 'current_conversation'
    assert final['exercises'] and all(set(e['equipment']) <= {'floor'} for e in final['exercises'])
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    assert advice(client)['equipment_source'] == 'profile'
    with app.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM workouts').fetchone()[0] == 0

ROOT = Path(__file__).resolve().parents[1]
DEV_RESPONSES = [json.loads(line) for line in
    (ROOT / 'evals/results/memory_v1_live_remaining_20260928/cases.jsonl').read_text(encoding='utf-8').splitlines()]
CLARIFY_CASES = [row for row in DEV_RESPONSES if row['split'] == 'dev'
    and row['family'] in ('uncertain_claim', 'future_condition', 'mixed_scopes')]
INPUTS = {row['id']: row for row in map(json.loads,
    (ROOT / 'evals/memory/cases.jsonl').read_text(encoding='utf-8').splitlines())}


@pytest.mark.parametrize('case', CLARIFY_CASES, ids=lambda row: row['id'])
def test_recorded_dev_response_becomes_clarification_without_applying(scoped, case):
    client, app, model, _ = scoped
    raw = case['steps'][0]['observed']['raw']
    assert raw is not None, 'Provider failure is not a replayable model response'
    model.edit = lambda data, out: (out.clear(), out.update(raw))
    current = send(client, conversation(client), INPUTS[case['id']]['steps'][0]['text']).json()
    response = understand(client, current, auto_apply=True)
    assert response.status_code == 200, response.text
    pending = response.json()
    assert pending['pending'] and pending['review']['status'] == 'needs_input'
    assert pending['review']['question']
    assert pending['review']['equipment_memory'] is None
    assert pending['review']['training'] is None
    assert pending['review']['profile_equipment_on_confirmation'] is None
    assert confirm(client, pending, equipment_scope='long_term').status_code == 409
    assert recommend(client, pending).status_code == 409
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    assert advice(client)['equipment'] == '在家，徒手'
    with app.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM coach_day_equipment').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM workouts').fetchone()[0] == 0


@pytest.mark.parametrize('text', ['明天只有哑铃', '平时只有哑铃，今天徒手', '未来三天只有哑铃'])
def test_unsupported_time_is_a_question_and_cannot_be_confirmed(scoped, text):
    client, app, _, _ = scoped
    _, response = draft(scoped, message=text)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['review']['status'] == 'needs_input'
    assert data['review']['equipment_memory'] is None and data['review']['training'] is None
    assert confirm(client, data).status_code == 409
    with app.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM coach_day_equipment').fetchone()[0] == 0
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'


@pytest.mark.parametrize('changes', [
    {'source_text': '可能只有哑铃'}, {'source_version': 2}, {'value': '杠铃'}, {'value': '只有哑铃'},
])
def test_uncertainty_does_not_soften_forged_source_or_lost_negation(scoped, changes):
    _, response = draft(scoped, message='可能只有哑铃，没有训练凳，还没确认。',
        **{'value': '只有哑铃，没有训练凳', **changes})
    assert response.status_code == 502, response.text


@pytest.mark.parametrize('text', ['今天可能只有哑铃，还没确认', '明天只有哑铃', '平时只有哑铃，今天徒手'])
def test_legacy_training_slot_cannot_bypass_clarification(scoped, text):
    client, _, model, _ = scoped
    model.edit = lambda data, out: out.update(scope='strength', command='', training={
        'source_text': data['messages'][-1]['message'], 'equipment': '只有哑铃'})
    data = send(client, conversation(client), text).json()
    response = understand(client, data, auto_apply=True)
    assert response.status_code == 200, response.text
    assert response.json()['review']['status'] == 'needs_input'
    assert response.json()['review']['training'] is None
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'


def test_explicit_followup_resolves_equipment_but_keeps_other_constraints(scoped):
    client, _, model, _ = scoped
    current, response = draft(scoped, message='训练的地方可能只有哑铃，还没确认。我对牛肉过敏。')
    pending = response.json()
    assert pending['review']['status'] == 'needs_input'
    assert pending['review']['notes'][0]['food_avoid'] == ['beef']
    later = send(client, pending, '我确认了，今天只有哑铃，没有训练凳。').json()
    def confirmed(data, out):
        turn = data['messages'][-1]
        out.update(scope='strength', command='', equipment_memory={'source_version': turn['version'],
            'source_text': turn['message'], 'value': '只有哑铃，没有训练凳', 'scope': 'day'})
    model.edit = confirmed
    response = understand(client, later, auto_apply=True)
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['status'] == 'ready'
    assert ready['review']['equipment_memory']['source_version'] == later['version']
    assert ready['review']['notes'][0]['food_avoid'] == ['beef']
    assert confirm(client, pending).status_code == 409
    accepted = confirm(client, ready)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['constraints']['food_avoid'] == ['beef']
    assert advice(client)['equipment'] == '只有哑铃，没有训练凳'
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'


@pytest.mark.parametrize('scope', ['session', 'day', 'long_term', 'unspecified'])
def test_model_cannot_choose_undeclared_scope(scoped, scope):
    client, _, _, _ = scoped
    _, response = draft(scoped, message='我训练的地方只有哑铃，帮我看下。', scope=scope)
    assert response.status_code == 200, response.text
    pending = response.json()
    assert pending['review']['equipment_memory']['scope'] == 'unspecified'
    assert confirm(client, pending).status_code == 422
    assert confirm(client, pending, equipment_scope='session').status_code == 200
    assert advice(client)['equipment'] == '在家，徒手'


def test_legacy_exact_phrase_requires_scope_and_explicit_save(scoped):
    client, _, model, _ = scoped
    model.edit = lambda data, out: out.update(scope='strength', command='')
    current = send(client, conversation(client), '在家，只有哑铃').json()
    response = understand(client, current, auto_apply=True)
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['equipment_memory']['scope'] == 'unspecified'
    assert ready['review']['profile_equipment_on_confirmation'] is None
    assert confirm(client, ready).status_code == 422
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    assert confirm(client, ready, equipment_scope='day').status_code == 200
    assert advice(client)['equipment'] == '在家，只有哑铃'
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
