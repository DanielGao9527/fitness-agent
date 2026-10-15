"""Redundant model questions must not bypass provenance or real missing inputs."""
import pytest
from fastapi.testclient import TestClient

from app import create_app
from evals.memory.protocol import load_suite
from test_foundation import application, client, register
from test_knowledge import library
from test_meal_plans import plans
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_equipment_memory_scopes import scoped, advice
from test_training_recommendations import recommend, result


def questioned(scoped, text, value, *, training=None, clarification='which_training', note=None):
    client, _, model, _ = scoped
    def edit(data, out):
        turn = data['messages'][-1]
        out.update(scope='strength', command='', clarification=clarification, training=training,
            equipment_memory={'source_version': turn['version'], 'source_text': turn['message'],
                              'value': value, 'scope': 'unspecified'})
        if note:
            out['notes'][-1].update(note)
    model.edit = edit
    current = send(client, conversation(client), text).json()
    return understand(client, current, auto_apply=True)


DEV = [case for case in load_suite('evals/memory/expressions_v2')[1]
       if case['split'] == 'dev' and case['family'] == 'unspecified_context']


@pytest.mark.parametrize('case', DEV, ids=lambda case: case['id'])
def test_equipment_only_dev_inputs_allow_scope_selection_without_auto_apply(scoped, case):
    client, app, _, _ = scoped
    step = case['steps'][0]
    response = questioned(scoped, step['text'], step['expect']['values'][0])
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['status'] == 'ready'
    assert ready['review']['equipment_memory']['scope'] == 'unspecified'
    assert ready['review']['clarification_resolution'] == 'equipment_scope_selection'
    assert confirm(client, ready).status_code == 422
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'
    accepted = confirm(client, ready, equipment_scope='session')
    assert accepted.status_code == 200, accepted.text
    final = result(recommend(client, accepted.json()))
    assert final['status'] == 'ready' and final['equipment'] == step['expect']['values'][0]
    assert final['equipment_source'] == 'current_conversation'
    assert all(set(item['equipment']) <= set(case['steps'][1]['expect']['resources']) for item in final['exercises'])
    assert advice(client)['equipment'] == '在家，徒手'
    with app.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM workouts').fetchone()[0] == 0


@pytest.mark.parametrize('choice,expected', [('session', '在家，徒手'), ('day', '只有弹力带'), ('long_term', '只有弹力带')])
def test_recovered_review_obeys_explicit_scope_restart_and_idempotency(scoped, choice, expected):
    client, app, _, _ = scoped
    response = questioned(scoped, '我现在可用的训练设备是：只有弹力带，请先核对这些器械。', '只有弹力带')
    ready = response.json()
    assert response.status_code == 200 and ready['review']['status'] == 'ready'
    saved = confirm(client, ready, equipment_scope=choice)
    assert saved.status_code == 200, saved.text
    assert confirm(client, ready, equipment_scope=choice).status_code == 200
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert advice(restarted)['equipment'] == expected
    with TestClient(create_app(app.state.settings)) as other:
        register(other, 'isolated_scope_user')
        assert other.get('/api/profile').json()['equipment'] != '只有弹力带'


@pytest.mark.parametrize('suffix', [
    '，把那个动作换掉', '，我想练全身', '，请给我午餐建议', '，有个没说清楚的要求',
    '，肩膀最近疼', '，但这些是朋友的，不能借', '，不要使用我刚说的器械',
])
def test_extra_or_unknown_requests_keep_the_question_even_if_model_omits_flags(scoped, suffix):
    client, _, _, _ = scoped
    response = questioned(scoped, '我只有哑铃' + suffix, '只有哑铃')
    assert response.status_code == 200, response.text
    blocked = response.json()
    assert blocked['review']['status'] == 'needs_input'
    assert confirm(client, blocked, equipment_scope='session').status_code == 409
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'


@pytest.mark.parametrize('clarification,note', [
    ('which_request', None), ('multiple_requests', None), ('unhandled_constraint', None),
    ('which_training', {'unresolved': True}), ('which_training', {'training_caution': True}),
    ('which_training', {'diet_caution': True}),
])
def test_real_missing_inputs_and_safety_flags_are_not_cleared(scoped, clarification, note):
    response = questioned(scoped, '我只有哑铃', '只有哑铃', clarification=clarification, note=note)
    blocked = response.json()
    assert response.status_code == 200 and blocked['review']['status'] == 'needs_input'
    assert confirm(scoped[0], blocked, equipment_scope='long_term').status_code == 409


def test_separate_pending_request_cannot_be_consumed_by_equipment_review(scoped):
    client, _, model, _ = scoped
    current = send(client, conversation(client), '把那个动作换一下').json()
    current = send(client, current, '我只有哑铃').json()
    def edit(data, out):
        turn = data['messages'][-1]
        out.update(scope='strength', command='', clarification='which_training', equipment_memory={
            'source_version': turn['version'], 'source_text': turn['message'], 'value': '只有哑铃', 'scope': 'unspecified'})
    model.edit = edit
    response = understand(client, current, auto_apply=True)
    assert response.status_code == 200 and response.json()['review']['status'] == 'needs_input'
    assert confirm(client, response.json(), equipment_scope='session').status_code == 409


def test_training_changes_are_not_disguised_as_scope_selection(scoped):
    text = '我只有哑铃'
    response = questioned(scoped, text, '只有哑铃', training={'source_text': text, 'minutes': 30})
    assert response.status_code == 502, response.text


def test_legacy_equipment_slot_uses_the_same_scope_repair_and_provenance_checks(scoped):
    client, _, model, _ = scoped
    def edit(data, out):
        out.update(scope='strength', command='', clarification='which_training', training={
            'source_text': data['messages'][-1]['message'], 'equipment': '只有弹力带'})
    model.edit = edit
    current = send(client, conversation(client), '我只有弹力带').json()
    response = understand(client, current, auto_apply=True)
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['pending'] and ready['review']['status'] == 'ready'
    assert ready['review']['equipment_memory']['scope'] == 'unspecified'
    assert confirm(client, ready).status_code == 422
    assert confirm(client, ready, equipment_scope='day').status_code == 200
    assert client.get('/api/profile').json()['equipment'] == '在家，徒手'


def test_profile_change_invalidates_recovered_scope_review(scoped):
    client, _, _, _ = scoped
    response = questioned(scoped, '我只有哑铃', '只有哑铃')
    ready = response.json()
    assert ready['review']['status'] == 'ready'
    assert client.put('/api/profile', json={'equipment': '在家，徒手'}).status_code == 200
    assert confirm(client, ready, equipment_scope='long_term').status_code == 409
