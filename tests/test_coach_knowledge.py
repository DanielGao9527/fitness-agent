"""Main-chat knowledge routing, using isolated data and provider doubles."""
from dataclasses import replace
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

import api.coach as coach_api
from fastapi.testclient import TestClient
from app import create_app
from model.factory import ModelError
from schemas import Profile
from services.records import RecordService
from services.coach import pending_turns, plan_binding
from test_foundation import application, client, register
from test_knowledge import library, mutate
from test_knowledge_answers import AnswerModel, CountingRetriever, SnapshotRetriever, calls
from test_coach import BASE, conversation, send
from test_coach_reviews import ReviewModel, understand, confirm


@pytest.fixture
def knowledge_chat(client, application, library, monkeypatch):
    application.state.knowledge = library
    application.state.settings = replace(application.state.settings, knowledge_qa_enabled=True,
        model_provider="qwen", qwen_api_key="synthetic-test-key")
    model = AnswerModel()
    monkeypatch.setattr(coach_api, "create_knowledge_model", lambda settings: model, raising=False)
    register(client)
    return client, application, model


def answer(client, data, **changes):
    return client.post(f"{BASE}/{data['id']}/answer-knowledge", json={
        "version": data["version"], "client_id": str(uuid4()), **changes})


@pytest.mark.parametrize("question,source", [
    ("力量训练一般一周做几次", "cdc-adults:strength"),
    ("哪些食物含蛋白质", "phe-eatwell:protein"),
    ("力量训练通常一星期练几天", "cdc-adults:strength"),
    ("蛋白质主要来自哪些食物", "phe-eatwell:protein"),
])
def test_main_chat_general_questions_without_meal_consent(knowledge_chat, question, source):
    client, app, model = knowledge_chat
    data = send(client, conversation(client), question).json()
    assert data["request_kind"] == "knowledge"
    assert not data["pending"] and not data["meal_consent"]["confirmed"]
    result = answer(client, data)
    assert result.status_code == 200, result.text
    saved = result.json()
    value = saved["turns"][-1]["response"]["knowledge"]
    assert value["status"] == "answered" and value["sources"][0]["chunk_id"] == source
    assert saved["action"] == "knowledge"
    assert saved["constraints"] == {} and not saved["meal_plans"] and not saved["training_plans"]
    assert calls(app) == 1 and len(model.messages) == 1


@pytest.mark.parametrize("retriever_type", [CountingRetriever, SnapshotRetriever])
def test_chat_shares_one_candidate_snapshot_and_retry_does_not_search(knowledge_chat, retriever_type):
    client, app, model = knowledge_chat
    retriever = retriever_type(app.state.knowledge)
    app.state.knowledge = retriever
    data = send(client, conversation(client), "力量训练一般一周做几次").json()
    result = answer(client, data)
    assert result.status_code == 200, result.text
    value = result.json()["turns"][-1]["response"]["knowledge"]
    assert value["status"] == "answered"
    assert value["sources"][0]["chunk_id"] == "cdc-adults:strength"
    if retriever_type is SnapshotRetriever:
        assert retriever.snapshots == 1 and retriever.searches == 0
        assert value["retrieval"]["mode"] == "hybrid"
    else:
        assert retriever.searches == 1
    assert "query" not in json.dumps(value["retrieval"])
    # Chat decoration also checks fixed planning sources by ID; those are local reads.
    assert ["cdc-adults:strength"] in retriever.reads
    assert answer(client, data).status_code == 200
    assert retriever.searches == (0 if retriever_type is SnapshotRetriever else 1)
    if retriever_type is SnapshotRetriever:
        assert retriever.snapshots == 1
    assert len(model.messages) == 1 and calls(app) == 1


def test_sensitive_profile_stops_before_optional_embedding(knowledge_chat):
    client, app, model = knowledge_chat
    retriever = SnapshotRetriever(app.state.knowledge)
    app.state.knowledge = retriever
    client.put("/api/profile", json={"preferences": "近期肩膀疼痛"})
    data = send(client, conversation(client), "力量训练一般一周做几次").json()
    result = answer(client, data).json()["turns"][-1]["response"]["knowledge"]
    assert result["status"] == "personal_scope"
    assert retriever.snapshots == 0 and retriever.searches == 0
    assert not model.messages and calls(app) == 0


@pytest.mark.parametrize("message", ["今天安排力量训练", "帮我安排晚餐", "把晚餐豆腐换成牛肉",
    "我每周应该练多少次力量", "给晚餐增加蛋白质食材"])
def test_personal_requests_never_execute_knowledge(knowledge_chat, message):
    client, app, model = knowledge_chat
    data = send(client, conversation(client), message).json()
    assert data["request_kind"] != "knowledge"
    assert answer(client, data).status_code == 409
    assert not model.messages and calls(app) == 0


def test_knowledge_does_not_repeat_or_invalidate_training(knowledge_chat):
    client, app, _ = knowledge_chat
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    client.put('/api/profile', json={"equipment": "哑铃，训练凳", "training_split": "ppl"})
    first = send(client, conversation(client), "今天练什么").json()
    planned = client.post(f"{BASE}/{first['id']}/recommend-training", json={"version": first['version'], "client_id": str(uuid4())})
    assert planned.status_code == 200, planned.text
    original = planned.json()["turns"][0]["response"]["training_recommendation"]
    data = send(client, first, "力量训练一般一周做几次").json()
    saved = answer(client, data).json()
    assert saved['intent'] == 'training' and saved['action'] == 'knowledge'
    assert saved['workflow_version'] == first['version']
    assert saved['turns'][0]['response']['training_recommendation'] == original
    for version in [first['version'], data['version']]:
        response = client.post(f"{BASE}/{data['id']}/recommend-training", json={"version": version, "client_id": str(uuid4())})
        assert response.status_code == 409
    next_turn = send(client, saved, "练腿").json()
    assert next_turn['action'] == 'training' and next_turn['training_context']['focus'] == '腿'


def test_meal_binding_keeps_its_original_version(knowledge_chat):
    client, app, _ = knowledge_chat
    first = send(client, conversation(client), "下一餐吃什么").json()
    with app.state.database.connect() as connection:
        user = connection.execute("SELECT id FROM users").fetchone()[0]
        original = {"coach_id": first['id'], "coach_version": first['version']}
        before = plan_binding(connection, user, original, first['day'], 'meal')
    data = send(client, first, "哪些食物含蛋白质").json()
    assert answer(client, data).status_code == 200
    with app.state.database.connect() as connection:
        assert plan_binding(connection, user, original, first['day'], 'meal') == before
    assert client.post(f"{BASE}/{data['id']}/recommend-meals", json={"version": data['version'], "client_id": str(uuid4())}).status_code == 409
    next_turn = send(client, data, "把豆腐换成牛肉").json()
    assert next_turn['action'] == 'meal'
    assert next_turn['meal_context']['group_choices']['protein'] == ['beef']


def test_pending_notes_not_consumed_by_local_knowledge(knowledge_chat):
    client, app, model = knowledge_chat
    retriever = SnapshotRetriever(app.state.knowledge)
    app.state.knowledge = retriever
    first = send(client, conversation(client), "芒果过敏，还没说完").json()
    data = send(client, first, "哪些食物含蛋白质").json()
    result = answer(client, data).json()
    assert result['pending'] and result['turns'][-1]['response']['knowledge']['status'] == 'context_pending'
    assert result['turns'][0]['message'] == first['turns'][0]['message']
    with app.state.database.connect() as connection:
        assert [item['version'] for item in pending_turns(connection, data['id'])] == [1]
    assert understand(client, result, auto_apply=True).status_code == 409
    assert calls(app) == 0 and not model.messages
    assert retriever.snapshots == 0 and retriever.searches == 0


def test_model_classified_knowledge_only_resolves_current_message(knowledge_chat, monkeypatch):
    client, app, _ = knowledge_chat
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    understanding = ReviewModel()
    understanding.edit = lambda _, out: out.update(scope='knowledge', command='')
    monkeypatch.setattr(coach_api, 'create_coach_model', lambda _: understanding)
    first = send(client, conversation(client), '未知食材过敏，稍后补充').json()
    data = send(client, first, '解释一下蛋白质供能的概念').json()
    assert data['pending'] and data['request_kind'] != 'knowledge', 'exercise model fallback, not a local rule'
    reviewed = understand(client, data, auto_apply=True)
    assert reviewed.status_code == 200, reviewed.text
    result = reviewed.json()
    assert result['action'] == 'knowledge' and result['pending']
    assert result['turns'][-1]['response']['reviewed_versions'] == [2]
    assert answer(client, result).json()['pending']
    with app.state.database.connect() as connection:
        assert [item['version'] for item in pending_turns(connection, data['id'])] == [1]
    followup = send(client, result, '指的是芒果').json()
    understanding.edit = lambda _, out: out.update(scope='unclear', command='', clarification='which_request')
    assert understand(client, followup).status_code == 200
    assert [item['version'] for item in understanding.messages[-1]['messages']] == [1, 3]


def test_fallback_knowledge_without_consent_can_answer(knowledge_chat, monkeypatch):
    client, app, model = knowledge_chat
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    understanding = ReviewModel()
    understanding.edit = lambda _, out: out.update(scope='knowledge', command='')
    monkeypatch.setattr(coach_api, 'create_coach_model', lambda _: understanding)
    data = send(client, conversation(client), '解释一下蛋白质供能的概念').json()
    reviewed = understand(client, data, auto_apply=True).json()
    assert reviewed['action'] == 'knowledge' and not reviewed['pending']
    assert not reviewed['meal_consent']['confirmed']
    assert answer(client, reviewed).status_code == 200
    assert len(model.messages) == 1


@pytest.mark.parametrize('scope', ['knowledge', 'meal'])
def test_mixed_message_cannot_silently_drop_a_request(knowledge_chat, monkeypatch, scope):
    client, app, _ = knowledge_chat
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    understanding = ReviewModel()
    understanding.edit = lambda _, out: out.update(scope=scope, command='' if scope == 'knowledge' else '下一餐吃什么')
    monkeypatch.setattr(coach_api, 'create_coach_model', lambda _: understanding)
    message = '哪些食物有蛋白质，我对花生过敏，再帮我配晚餐'
    data = send(client, conversation(client), message).json()
    assert data['pending'] and data['request_kind'] != 'knowledge'
    result = understand(client, data, auto_apply=True).json()
    assert result['pending'] and result['review']['status'] == 'needs_input'
    assert result['review']['clarification'] == 'multiple_requests'
    assert result['turns'][-1]['message'] == message
    assert confirm(client, result).status_code == 409


@pytest.mark.parametrize('profile,question,status', [
    ({'food_allergies': '花生过敏'}, '哪些食物含蛋白质', 'personal_scope'),
    ({'preferences': '近期肩膀疼痛'}, '力量训练一般一周做几次', 'personal_scope'),
    ({'food_allergies': '花生过敏'}, '力量训练一般一周做几次', 'answered'),
])
def test_existing_profile_scope_guards_are_not_relaxed(knowledge_chat, profile, question, status):
    client, _, _ = knowledge_chat
    client.put('/api/profile', json=profile)
    data = send(client, conversation(client), question).json()
    result = answer(client, data).json()['turns'][-1]['response']['knowledge']
    assert result['status'] == status


@pytest.mark.parametrize('failure,expected', [
    (ModelError('MODEL_TIMEOUT', '合成超时', 504), 504),
    ('{"status":"answered","chunk_ids":["made-up"]}', 502),
    ('{"status":"answered","chunk_ids":["fda-label:energy"]}', 502),
])
def test_failed_calls_are_persisted_and_not_retried(knowledge_chat, failure, expected):
    client, app, model = knowledge_chat
    model.output = failure
    data = send(client, conversation(client), '力量训练一般一周做几次').json()
    key = str(uuid4())
    assert answer(client, data, client_id=key).status_code == expected
    for request_id in [key, str(uuid4())]:
        result = answer(client, data, client_id=request_id)
        assert result.status_code == 200
        assert result.json()['turns'][-1]['response']['knowledge']['status'] == 'failed'
    assert len(model.messages) == 1 and calls(app) == 1


def test_unavailable_and_quota_are_distinct_and_records_still_work(knowledge_chat):
    client, app, model = knowledge_chat
    app.state.settings = replace(app.state.settings, knowledge_qa_enabled=False)
    data = send(client, conversation(client), '哪些食物含蛋白质').json()
    assert answer(client, data).json()['detail']['code'] == 'KNOWLEDGE_NOT_CONFIGURED'
    assert calls(app) == 0
    app.state.settings = replace(app.state.settings, knowledge_qa_enabled=True, ai_global_daily_limit=1)
    second = send(client, data, '哪些食物含蛋白质').json()
    assert answer(client, second).status_code == 200
    third = send(client, second, '力量训练一般一周做几次').json()
    assert answer(client, third).status_code == 429
    assert calls(app) == 1
    from test_foundation import meal
    assert client.post('/api/meals', json=meal()).status_code == 201


@pytest.mark.parametrize('change', ['message', 'profile', 'source', 'delete'])
def test_changes_while_waiting_never_publish_old_results(knowledge_chat, change):
    client, app, model = knowledge_chat
    data = send(client, conversation(client), '力量训练一般一周做几次').json()
    if change == 'message':
        model.on_call = lambda: send(client, data, '今天练什么')
    elif change == 'profile':
        with app.state.database.connect() as connection:
            user = connection.execute('SELECT id FROM users').fetchone()[0]
        model.on_call = lambda: RecordService(app.state.database, user).save_profile(Profile(weight_kg=85))
    elif change == 'source':
        model.on_call = lambda: mutate(app.state.knowledge, lambda corpus: next(source for source in corpus['sources'] if source['id'] == 'cdc-adults').update(status='withdrawn'))
    else:
        model.on_call = lambda: client.request('DELETE', f"{BASE}/{data['id']}", json={'version': data['version']})
    response = answer(client, data)
    assert response.status_code == 409, response.text
    saved = client.get(f"{BASE}/{data['id']}")
    if change == 'delete':
        assert saved.status_code == 404
    else:
        assert saved.json()['turns'][0]['response']['knowledge']['sources'] == []
    assert calls(app) == 1


def test_source_withdrawal_hides_history_without_new_calls(knowledge_chat):
    client, app, model = knowledge_chat
    data = send(client, conversation(client), '力量训练一般一周做几次').json()
    assert answer(client, data).status_code == 200
    mutate(app.state.knowledge, lambda corpus: next(source for source in corpus['sources'] if source['id'] == 'cdc-adults').update(status='withdrawn'))
    saved = client.get(f"{BASE}/{data['id']}").json()['turns'][-1]['response']['knowledge']
    assert saved['status'] == 'sources_changed' and not saved['sources']
    assert len(model.messages) == 1


def test_no_evidence_and_index_unavailable(knowledge_chat):
    client, app, model = knowledge_chat
    from rag.rag_service import EmptyKnowledgeRetriever, KnowledgeUnavailable
    app.state.knowledge = EmptyKnowledgeRetriever()
    data = send(client, conversation(client), '哪些食物含蛋白质').json()
    assert answer(client, data).json()['turns'][-1]['response']['knowledge']['status'] == 'insufficient'
    class Broken(EmptyKnowledgeRetriever):
        def search(self, *args, **kwargs):
            raise KnowledgeUnavailable('测试索引不可用')
    app.state.knowledge = Broken()
    data = send(client, data, '哪些食物含蛋白质').json()
    assert answer(client, data).json()['detail']['code'] == 'KNOWLEDGE_UNAVAILABLE'
    assert not model.messages and calls(app) == 0


def test_concurrency_idempotence_and_restart(knowledge_chat):
    client, app, model = knowledge_chat
    initial = conversation(client)
    key = str(uuid4())
    data = send(client, initial, '哪些食物含蛋白质', client_id=key).json()
    assert send(client, initial, '哪些食物含蛋白质', client_id=key).json()['version'] == 1
    entered, release = Event(), Event()
    def wait():
        entered.set()
        assert release.wait(10)
    model.on_call = wait
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(answer, client, data, client_id=key)
        assert entered.wait(10)
        try:
            duplicate = answer(client, data, client_id=key).json()
            assert duplicate['turns'][-1]['response']['knowledge']['status'] == 'generating'
        finally:
            release.set()
        assert first.result().status_code == 200
    assert len(model.messages) == 1 and calls(app) == 1
    restarted_app = create_app(app.state.settings)
    with TestClient(restarted_app) as restarted:
        restarted_app.state.knowledge = app.state.knowledge
        restarted.cookies.update(client.cookies)
        saved = restarted.get(f"{BASE}/{data['id']}").json()
        assert saved['turns'][-1]['response']['knowledge']['status'] == 'answered'
        assert answer(restarted, saved, client_id=key).status_code == 200
    assert len(model.messages) == 1


def test_auth_ownership_and_input_validation(knowledge_chat):
    client, app, model = knowledge_chat
    data = send(client, conversation(client), '哪些食物含蛋白质').json()
    with TestClient(app) as other:
        assert answer(other, data).status_code == 401
        register(other, 'bob')
        assert answer(other, data).status_code == 404
    assert answer(client, data, user_id=2).status_code == 422
    assert answer(client, data, version=0).status_code == 422
    assert send(client, data, 'x' * 2001).status_code == 422
    assert answer(client, data, version=2).status_code == 409
    assert calls(app) == 0 and not model.messages


def test_confirmed_conversation_constraints_survive_knowledge_and_restart(knowledge_chat, monkeypatch):
    client, app, _ = knowledge_chat
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    understanding = ReviewModel()
    monkeypatch.setattr(coach_api, 'create_coach_model', lambda _: understanding)
    data = send(client, conversation(client), '花生过敏，晚饭怎么安排').json()
    reviewed = understand(client, data).json()
    accepted = confirm(client, reviewed).json()
    assert 'peanuts' in accepted['constraints']['food_avoid']
    data = send(client, accepted, '哪些食物含蛋白质').json()
    result = answer(client, data).json()
    assert result['turns'][-1]['response']['knowledge']['status'] == 'context_pending'
    assert result['constraints'] == accepted['constraints']
    app2 = create_app(app.state.settings)
    with TestClient(app2) as restarted:
        app2.state.knowledge = app.state.knowledge
        restarted.cookies.update(client.cookies)
        restored = restarted.get(f"{BASE}/{data['id']}").json()
        assert restored['constraints'] == accepted['constraints']
        pending = send(restarted, restored, '还有一种不能吃的东西，待会说明').json()
        knowledge = send(restarted, pending, '力量训练一般一周做几次').json()
        assert knowledge['pending']
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restored = restarted.get(f"{BASE}/{data['id']}").json()
        assert restored['pending'] and restored['constraints'] == accepted['constraints']
        with app.state.database.connect() as connection:
            assert [item['version'] for item in pending_turns(connection, data['id'])] == [3]


def test_interrupted_attempt_is_not_restarted_after_process_restart(knowledge_chat):
    client, app, model = knowledge_chat
    data = send(client, conversation(client), '哪些食物含蛋白质').json()
    with app.state.database.connect() as connection:
        value = dict(data['turns'][-1]['response'])
        value['knowledge'] = {'request_id': str(uuid4()), 'status': 'generating', 'sources': [], 'answer': '未确认完成，不自动重试'}
        connection.execute('UPDATE coach_turns SET response=? WHERE conversation_id=? AND version=?', (json.dumps(value), data['id'], data['version']))
    app2 = create_app(app.state.settings)
    with TestClient(app2) as restarted:
        app2.state.knowledge = app.state.knowledge
        restarted.cookies.update(client.cookies)
        result = answer(restarted, data)
        assert result.status_code == 200
        assert result.json()['turns'][-1]['response']['knowledge']['status'] == 'generating'
    assert not model.messages and calls(app) == 0


@pytest.mark.parametrize('text', ['哪些食物含蛋白质，不要花生', '力量训练一般每周几次，这次改为练胸'])
def test_general_question_with_extra_condition_is_not_read_only(knowledge_chat, text):
    client, _, model = knowledge_chat
    data = send(client, conversation(client), text).json()
    assert data['request_kind'] != 'knowledge'
    assert answer(client, data).status_code == 409
    assert not model.messages


@pytest.mark.parametrize('uncertain', ['clarification', 'unresolved'])
def test_knowledge_interpretation_uncertainty_is_not_silently_confirmed(knowledge_chat, monkeypatch, uncertain):
    client, app, _ = knowledge_chat
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    understanding = ReviewModel()
    def edit(_, out):
        out.update(scope='knowledge', command='')
        if uncertain == 'clarification':
            out['clarification'] = 'which_request'
        else:
            out['notes'][-1]['unresolved'] = True
    understanding.edit = edit
    monkeypatch.setattr(coach_api, 'create_coach_model', lambda _: understanding)
    data = send(client, conversation(client), '解释一下蛋白质供能的概念').json()
    result = understand(client, data, auto_apply=True).json()
    assert result['pending'] and result['review']['status'] == 'needs_input'
    assert result['review']['clarification'] == 'which_request'
    assert confirm(client, result).status_code == 409
