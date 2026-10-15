"""Authenticated hybrid retrieval integration, with synthetic providers and temporary data."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.coach as coach_api
import api.knowledge as knowledge_api
import model.embeddings as embedding_module
import rag.rag_service as rag_module
from app import create_app
from config import Settings
from model.factory import ModelError, create_knowledge_model
from rag.hybrid import HybridKnowledgeRetriever
from rag.vector_store import VectorStore
from test_coach import BASE, conversation, send
from test_coach_knowledge import answer
from test_foundation import DAY, application, client, meal, register
from test_hybrid_retrieval import TODAY, FakeEmbedding, mutate, setup_retrieval
from test_knowledge_answers import AnswerModel, calls


@pytest.fixture
def hybrid_api(client, application, setup_retrieval, monkeypatch):
    lexical, provider, store = setup_retrieval
    monkeypatch.setattr(rag_module, "business_today", lambda: TODAY)
    store.build(lexical, provider, today=TODAY)
    provider.calls = 0
    provider.seen.clear()
    application.state.settings = replace(application.state.settings,
        knowledge_source_path=lexical.source_path, knowledge_index_path=lexical.index_path,
        knowledge_vector_path=store.path, retrieval_mode="hybrid", knowledge_qa_enabled=True,
        model_provider="qwen", qwen_api_key="synthetic-test-key")
    application.state.knowledge = HybridKnowledgeRetriever(lexical, provider, store)
    model = AnswerModel()
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda _: model)
    monkeypatch.setattr(coach_api, "create_knowledge_model", lambda _: model)
    user = register(client)
    return client, application, lexical, provider, store, model, user


def search(client, query="营养标签 两份", **changes):
    return client.post("/api/knowledge/search", json={"query": query, **changes})


def test_no_key_hybrid_reports_configuration_fallback_and_keeps_records(tmp_path, setup_retrieval):
    lexical, _, store = setup_retrieval
    settings = Settings(
        database_path=tmp_path / "no-key-personal.sqlite3",
        knowledge_source_path=lexical.source_path,
        knowledge_index_path=tmp_path / "no-key-jieba.sqlite3",
        knowledge_vector_path=store.path,
    )
    with TestClient(create_app(settings)) as client:
        register(client)
        catalog = client.get("/api/knowledge").json()
        assert catalog["requested_method"] == "hybrid_rrf"
        assert catalog["vector_status"] == "degraded"
        assert catalog["degraded_reason"] == "embedding_not_configured"
        result = search(client).json()
        assert result["retrieval"]["mode"] == "lexical"
        assert result["retrieval"]["requested_mode"] == "hybrid"
        assert result["retrieval"]["tokenizer"] == "jieba"
        assert result["retrieval"]["reason"] == "embedding_not_configured"
        assert not result["retrieval"]["embedding_called"]
        assert result["semantic_search_enabled"] is False
        assert client.post("/api/meals", json=meal()).status_code == 201
        assert len(client.get(f"/api/meals?day={DAY}").json()) == 1


def test_runtime_retrieval_defaults_are_hybrid_jieba(monkeypatch, tmp_path):
    monkeypatch.delenv("FITNESS_RETRIEVAL_MODE", raising=False)
    monkeypatch.delenv("FITNESS_RETRIEVAL_TOKENIZER", raising=False)
    direct = Settings(database_path=tmp_path / "personal.sqlite3")
    runtime = Settings.from_env()
    assert (direct.retrieval_mode, direct.retrieval_tokenizer) == ("hybrid", "jieba")
    assert (runtime.retrieval_mode, runtime.retrieval_tokenizer) == ("hybrid", "jieba")


def test_explicit_jieba_hybrid_uses_real_app_wiring_and_authenticated_search(
        tmp_path, setup_retrieval, monkeypatch):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    provider.calls = 0
    provider.seen.clear()
    monkeypatch.setattr(embedding_module, "QwenEmbeddingProvider", lambda _: provider)
    common = dict(knowledge_source_path=lexical.source_path,
                  knowledge_vector_path=store.path)
    lexical_settings = Settings(database_path=tmp_path / "baseline-personal.sqlite3",
                                knowledge_index_path=tmp_path / "baseline-bigram.sqlite3",
                                retrieval_mode="lexical", retrieval_tokenizer="bigram", **common)
    hybrid_settings = Settings(database_path=tmp_path / "hybrid-personal.sqlite3",
                               knowledge_index_path=tmp_path / "hybrid-jieba.sqlite3",
                               retrieval_mode="hybrid", retrieval_tokenizer="jieba",
                               qwen_api_key="synthetic-test-key", **common)
    with TestClient(create_app(lexical_settings)) as baseline:
        register(baseline)
        old = search(baseline).json()
    with TestClient(create_app(hybrid_settings)) as enhanced:
        register(enhanced)
        catalog = enhanced.get("/api/knowledge").json()
        new = search(enhanced).json()
        assert catalog["vector_status"] == "ready"
        assert enhanced.get("/api/usage").json()["used"] == 1
    assert old["retrieval"]["mode"] == "lexical"
    assert old["retrieval"]["tokenizer"] == "bigram"
    assert new["retrieval"]["mode"] == "hybrid"
    assert new["retrieval"]["tokenizer"] == "jieba"
    assert new["retrieval"]["embedding_called"] is True
    assert new["semantic_search_enabled"] is True
    assert new["retrieval"]["reason"] is None
    assert old["hits"][0]["chunk_id"] == new["hits"][0]["chunk_id"] == "fda-label:servings"
    assert provider.seen == [("query", ["营养标签 两份"])]


def test_search_reserves_authenticated_usage_and_reports_cloud_call(hybrid_api):
    client, app, _, provider, _, model, _ = hybrid_api
    result = search(client)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["hits"][0]["chunk_id"] == "fda-label:servings"
    assert value["retrieval"]["mode"] == "hybrid"
    assert value["retrieval"]["embedding_called"] and value["model_called"]
    assert provider.seen == [("query", ["营养标签 两份"])]
    assert provider.calls == calls(app) == 1
    assert client.get("/api/usage").json()["used"] == 1
    assert not model.messages and not value["saved"]
    assert "营养标签 两份" not in json.dumps(value["retrieval"], ensure_ascii=False)


@pytest.mark.parametrize("limit_field", ["ai_user_daily_limit", "ai_global_daily_limit"])
@pytest.mark.parametrize("route", ["search", "ask", "coach"])
def test_zero_budget_stops_before_any_cloud_request(hybrid_api, limit_field, route):
    client, app, _, provider, _, model, _ = hybrid_api
    app.state.settings = replace(app.state.settings, **{limit_field: 0})
    if route == "search":
        result = search(client)
    elif route == "ask":
        result = client.post("/api/knowledge/ask", json={"question": "哪些食物含蛋白质"})
    else:
        data = send(client, conversation(client), "哪些食物含蛋白质").json()
        result = answer(client, data)
        assert answer(client, data).json()["turns"][-1]["response"]["knowledge"]["status"] == "failed"
    assert result.status_code == 429, result.text
    assert provider.calls == calls(app) == 0
    assert provider.seen == [] and not model.messages


def test_missing_vector_cache_falls_back_without_provider_or_usage(hybrid_api, tmp_path):
    client, app, lexical, provider, _, model, _ = hybrid_api
    store = VectorStore(tmp_path / "not-built.sqlite3")
    app.state.knowledge = HybridKnowledgeRetriever(lexical, provider, store)
    result = search(client).json()
    assert result["hits"][0]["chunk_id"] == "fda-label:servings"
    assert result["retrieval"]["degraded"]
    assert result["retrieval"]["reason"] == "vector_cache_missing"
    assert not result["retrieval"]["embedding_called"] and not result["model_called"]
    assert provider.calls == calls(app) == 0 and not model.messages
    assert not store.path.exists()


def test_usage_and_context_remain_separate_between_accounts(hybrid_api):
    client, app, _, provider, _, _, user = hybrid_api
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=1, ai_global_daily_limit=10)
    client.put("/api/profile", json={"display_name": "AlicePrivateProfile"})
    client.post("/api/meals", json=meal(name="AlicePrivateMeal"))
    assert search(client).status_code == 200
    assert search(client).status_code == 429
    with TestClient(app) as other:
        bob = register(other, "bob")
        assert bob["id"] != user["id"]
        assert other.get("/api/usage").json()["used"] == 0
        assert search(other, "蛋白质来自哪些食物").status_code == 200
        assert other.get("/api/usage").json()["used"] == 1
        assert other.get(f"/api/meals?day={DAY}").json() == []
        assert search(other, user_id=user["id"]).status_code == 422
    assert client.get("/api/usage").json()["used"] == 1
    assert provider.calls == calls(app) == 2
    assert provider.seen == [("query", ["营养标签 两份"]), ("query", ["蛋白质来自哪些食物"])]
    assert "AlicePrivate" not in json.dumps(provider.seen)


def test_main_chat_uses_one_embedding_and_one_selection_across_retries(hybrid_api):
    client, app, _, provider, _, model, _ = hybrid_api
    data = send(client, conversation(client), "哪些食物含蛋白质").json()
    key = str(uuid4())
    response = answer(client, data, client_id=key)
    assert response.status_code == 200, response.text
    value = response.json()["turns"][-1]["response"]["knowledge"]
    assert value["status"] == "answered"
    assert value["sources"][0]["chunk_id"] == "fda-label:protein"
    assert value["retrieval"]["embedding_called"] and value["model_called"]
    assert provider.calls == len(model.messages) == 1 and calls(app) == 2
    for retry_id in [key, str(uuid4())]:
        assert answer(client, data, client_id=retry_id).status_code == 200
    assert provider.calls == len(model.messages) == 1 and calls(app) == 2


def test_chat_selection_budget_failure_does_not_repeat_embedding(hybrid_api):
    client, app, _, provider, _, model, _ = hybrid_api
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=1)
    data = send(client, conversation(client), "哪些食物含蛋白质").json()
    result = answer(client, data)
    assert result.status_code == 429
    assert result.json()["detail"]["code"] == "KNOWLEDGE_CALL_LIMIT"
    assert provider.calls == calls(app) == 1 and not model.messages
    assert answer(client, data).json()["turns"][-1]["response"]["knowledge"]["status"] == "failed"
    assert provider.calls == calls(app) == 1 and not model.messages


def test_restart_uses_persisted_document_vectors_and_authenticated_quota(hybrid_api, monkeypatch):
    client, app, _, provider, store, _, _ = hybrid_api
    before = store.path.read_bytes()
    restarted_provider = FakeEmbedding(provider.identity)
    monkeypatch.setattr(embedding_module, "QwenEmbeddingProvider", lambda _: restarted_provider)
    restarted_app = create_app(app.state.settings)
    assert isinstance(restarted_app.state.knowledge.governance_base, HybridKnowledgeRetriever)
    with TestClient(restarted_app) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/api/knowledge").json()["vector_status"] == "ready"
        assert restarted_provider.calls == 0
        result = search(restarted)
        assert result.status_code == 200, result.text
        assert result.json()["retrieval"]["mode"] == "hybrid"
        assert restarted_provider.seen == [("query", ["营养标签 两份"])]
        assert restarted.get("/api/usage").json()["used"] == 1
    assert store.path.read_bytes() == before and provider.calls == 0


@pytest.mark.parametrize("during_query", [False, True])
def test_withdrawn_source_never_reaches_api_results(hybrid_api, during_query):
    client, app, lexical, provider, _, _, _ = hybrid_api
    withdraw = lambda: mutate(lexical, lambda corpus: corpus["sources"][0].update(status="withdrawn"))
    if during_query:
        provider.on_call = withdraw
    else:
        withdraw()
    result = search(client)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["hits"] == []
    assert value["retrieval"]["reason"] == ("sources_changed" if during_query else "empty_corpus")
    assert value["model_called"] is during_query
    assert provider.calls == calls(app) == int(during_query)


def test_failed_embedding_is_counted_once_and_public_error_is_sanitized(hybrid_api):
    client, app, _, provider, _, _, _ = hybrid_api
    provider.error = ModelError("MODEL_TIMEOUT", "synthetic-provider-private-detail")
    result = search(client)
    assert result.status_code == 200
    value = result.json()
    assert value["hits"] and value["retrieval"]["reason"] == "embedding_query_failed"
    assert value["model_called"] and value["retrieval"]["embedding_called"]
    assert "synthetic-provider-private-detail" not in result.text
    assert provider.calls == calls(app) == 1


def test_free_context_preview_browse_and_unauthenticated_search_never_embed(hybrid_api):
    client, app, _, provider, _, _, _ = hybrid_api
    result = client.post("/api/agent/context", json={"day": DAY, "message": "营养标签 两份"})
    assert result.status_code == 200, result.text
    assert result.json()["context"]["knowledge"]
    assert not result.json()["model_called"]
    assert search(client, "").json()["retrieval"]["reason"] == "browse"
    with TestClient(app) as anonymous:
        assert search(anonymous).status_code == 401
    assert provider.calls == calls(app) == 0


def test_disabled_legacy_agent_chat_does_not_bypass_embedding_quota(hybrid_api):
    client, app, _, provider, _, _, _ = hybrid_api
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=0, ai_global_daily_limit=0)
    result = client.post("/api/agent/chat", json={"day": DAY, "message": "营养标签 两份"})
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["code"] == "MODEL_NOT_CONFIGURED"
    assert provider.calls == calls(app) == 0


def test_disabled_knowledge_answer_stops_before_paid_embedding(hybrid_api, monkeypatch):
    client, app, _, provider, _, model, _ = hybrid_api
    app.state.settings = replace(app.state.settings, knowledge_qa_enabled=False)
    # Exercise the actual configuration check; the fixture's permissive factory
    # intentionally bypasses configuration for the separate mock-provider tests.
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", create_knowledge_model)
    result = client.post("/api/knowledge/ask", json={"question": "哪些食物含蛋白质"})
    assert result.status_code == 503, result.text
    assert provider.calls == calls(app) == 0 and not model.messages


@pytest.mark.parametrize("route", ["ask", "coach"])
def test_topic_scope_rejection_still_reports_completed_embedding_call(hybrid_api, route):
    client, app, _, provider, _, model, _ = hybrid_api
    client.put("/api/profile", json={"food_allergies": "花生"})
    if route == "ask":
        response = client.post("/api/knowledge/ask", json={"question": "哪些食物含蛋白质"})
        value = response.json()
    else:
        data = send(client, conversation(client), "哪些食物含蛋白质").json()
        response = answer(client, data)
        value = response.json()["turns"][-1]["response"]["knowledge"]
    assert response.status_code == 200, response.text
    assert value["status"] == "personal_scope" and value["sources"] == []
    assert value["model_called"] and value["retrieval"]["embedding_called"]
    assert value["model"] is None
    assert provider.calls == calls(app) == 1 and not model.messages
