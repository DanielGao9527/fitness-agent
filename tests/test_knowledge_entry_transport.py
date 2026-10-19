"""Local entry acceptance: transport faults and opt-in real cached DEV vectors.

The model response is always an engineering double. No cloud calls, human
quality approval, held-out ranking scores, or changes to the real user database.
"""
import hashlib
import json
import os
import shutil
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

import api.coach as coach_api
import api.knowledge as knowledge_api
from app import create_app
from config import ROOT, Settings
from fastapi.testclient import TestClient
from model.embeddings import QwenEmbeddingProvider
from model.qwen import QwenTextModel
from rag.vector_store import VectorStore
from scripts.evaluate_retrieval import CachedQueries, encode_queries, load_cases
from test_coach import BASE, conversation, send
from test_coach_knowledge import answer
from test_foundation import application, client, register
from test_hybrid_api import hybrid_api
from test_hybrid_retrieval import setup_retrieval
from test_knowledge import library
from test_knowledge_answers import calls


def facts(app):
    tables = ('profiles', 'meals', 'workouts', 'body_measurements', 'coach_memory_entries',
        'profile_fact_events', 'training_record_revisions', 'knowledge_reviewers', 'knowledge_live')
    with app.state.database.connect() as con:
        return {t: [tuple(row) for row in con.execute('SELECT * FROM "'+t+'" ORDER BY rowid')] for t in tables}


@pytest.mark.parametrize('route', ['ask', 'coach'])
@pytest.mark.parametrize('exception', [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_authenticated_entry_transport_timeout_is_explicit_nonwriting_and_not_retried(
        hybrid_api, monkeypatch, route, exception):
    client, app, _, provider, _, _, _ = hybrid_api
    dispatched = []

    def fail(request):
        dispatched.append(request)
        raise exception('PRIVATE-TRANSPORT-DETAIL', request=request)

    model = QwenTextModel(app.state.settings, transport=httpx.MockTransport(fail))
    monkeypatch.setattr(knowledge_api, 'create_knowledge_model', lambda _: model)
    monkeypatch.setattr(coach_api, 'create_knowledge_model', lambda _: model)
    question = '哪些食物含蛋白质'
    data = send(client, conversation(client), question).json() if route == 'coach' else None
    before = facts(app)
    result = answer(client, data) if route == 'coach' else client.post('/api/knowledge/ask', json={'question': question})
    assert result.status_code == 504 and result.json()['detail']['code'] == 'MODEL_TIMEOUT'
    assert 'PRIVATE-TRANSPORT-DETAIL' not in result.text and len(dispatched) == 1
    assert facts(app) == before and provider.calls == 1 and calls(app) == 2
    if route == 'coach':
        saved = client.get(f"{BASE}/{data['id']}").json()
        outcome = saved['turns'][-1]['response']['knowledge']
        assert saved['turns'][-1]['message'] == question
        assert outcome['status'] == 'failed' and outcome['sources'] == []
        for key in [str(uuid4()), str(uuid4())]:
            assert answer(client, data, client_id=key).status_code == 200
        assert len(dispatched) == 1 and provider.calls == 1 and calls(app) == 2


@pytest.fixture
def real_dev_entry(tmp_path, monkeypatch):
    if os.environ.get('FITNESS_TEST_EXISTING_PUBLIC_CACHE') != '1':
        pytest.skip('Explicit opt-in: public vector caches are not distributed with Git')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *a, **kw: pytest.fail('REAL_OUTBOUND_FORBIDDEN'))
    source = tmp_path / 'sources.json'
    source.write_bytes((ROOT / 'data/knowledge/sources.json').read_bytes())
    vectors = ROOT.parent / 'knowledge-vectors.sqlite3'
    query_dir = ROOT / 'artifacts/retrieval-eval'
    assert vectors.is_file() and (query_dir / 'public-queries-6492c73989bc10bb274f894b.json').is_file()
    before_cache = hashlib.sha256(vectors.read_bytes()).hexdigest()
    copy = tmp_path / 'public-vectors.sqlite3'; shutil.copyfile(vectors, copy)
    settings = Settings(database_path=tmp_path / 'isolated.sqlite3', knowledge_source_path=source,
        knowledge_index_path=tmp_path / 'index.sqlite3', knowledge_vector_path=copy,
        retrieval_mode='hybrid', retrieval_tokenizer='jieba', model_provider='qwen',
        qwen_api_key='synthetic-not-a-real-key', qwen_model='qwen3.6-plus', knowledge_qa_enabled=True)
    app = create_app(settings)
    identity = QwenEmbeddingProvider(settings, max_calls=0, max_input_chars=0)
    base = app.state.knowledge.governance_base
    documents = base.export_documents()
    assert len(VectorStore(copy).load(documents, identity)) == len(documents) == 133
    rows = [r for r in load_cases(ROOT / 'evals/fitness_qa.jsonl', {d.hit.chunk_id for d in documents}) if r['split'] == 'dev']
    encoded, usage = encode_queries(rows, identity, query_dir, live=False)
    assert usage['cache_hit'] and usage['calls'] == 0
    replay = CachedQueries(identity, dict(zip((r['question'] for r in rows), encoded)))
    base.provider = replay
    case = next(r for r in rows if r['id'] == 'fitness-qa-073')
    state = {'requests': [], 'withdraw': False}

    def respond(request):
        message = json.loads(request.content)['messages'][1]['content']
        assert 'PRIVATE-PROFILE-MARKER' not in message
        evidence = json.loads(message)['evidence']
        assert any(e['chunk_id'] == 'acsm-2026:programming' for e in evidence)
        state['requests'].append(request)
        if state['withdraw']:
            corpus = json.loads(source.read_text(encoding='utf-8'))
            next(s for s in corpus['sources'] if s['id'] == 'acsm-2026')['status'] = 'withdrawn'
            source.write_text(json.dumps(corpus, ensure_ascii=False), encoding='utf-8')
        content = json.dumps({'status': 'answered', 'chunk_ids': ['acsm-2026:programming']})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})

    model = QwenTextModel(settings, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(knowledge_api, 'create_knowledge_model', lambda _: model)
    monkeypatch.setattr(coach_api, 'create_knowledge_model', lambda _: model)
    with TestClient(app) as client:
        assert client.post('/api/knowledge/ask', json={'question': case['question']}).status_code == 401
        register(client)
        assert client.put('/api/profile', json={'display_name': 'PRIVATE-PROFILE-MARKER'}).status_code == 200
        yield client, app, case['question'], state
    assert hashlib.sha256(vectors.read_bytes()).hexdigest() == before_cache
    assert identity.calls == 0


@pytest.mark.parametrize('route', ['ask', 'coach'])
def test_real_cached_dev_hybrid_entry_and_source_recheck_without_cloud(real_dev_entry, route):
    client, app, question, state = real_dev_entry
    data = send(client, conversation(client), question).json() if route == 'coach' else None
    before = facts(app)
    response = answer(client, data) if route == 'coach' else client.post('/api/knowledge/ask', json={'question': question})
    assert response.status_code == 200, response.text
    value = response.json()['turns'][-1]['response']['knowledge'] if route == 'coach' else response.json()
    assert value['status'] == 'answered' and not value['saved']
    assert value['retrieval']['mode'] == 'hybrid' and not value['retrieval']['degraded']
    assert value['sources'][0]['chunk_id'] == 'acsm-2026:programming'
    assert value['sources'][0]['url'].startswith('https://')
    assert facts(app) == before and len(state['requests']) == calls(app) == 1
    if route == 'coach':
        assert answer(client, data).status_code == 200
        with TestClient(app) as other:
            register(other, 'bob')
            assert other.get(f"{BASE}/{data['id']}").status_code == 404
        assert len(state['requests']) == 1 and calls(app) == 1


def test_real_cached_dev_source_withdrawal_after_selection_blocks_output(real_dev_entry):
    client, app, question, state = real_dev_entry
    state['withdraw'] = True
    before = facts(app)
    response = client.post('/api/knowledge/ask', json={'question': question})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'KNOWLEDGE_CHANGED'
    assert facts(app) == before and len(state['requests']) == calls(app) == 1
