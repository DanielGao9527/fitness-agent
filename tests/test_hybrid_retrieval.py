"""Synthetic vectors verify retrieval mechanics, never semantic-model quality."""
import json
import sqlite3
from datetime import date

import pytest
from fastapi import HTTPException

from model.factory import ModelError
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import LocalKnowledgeRetriever, search_terms
from rag.vector_store import VectorCacheUnavailable, VectorStore
from rag.vector_store import unit_vector


TODAY = date(2026, 9, 24)


class FakeEmbedding:
    dimensions = 2

    def __init__(self, identity="test-embedding-a"):
        self.identity = identity
        self.calls = 0
        self.input_tokens = 0
        self.seen = []
        self.error = None
        self.on_call = None

    def embed(self, texts, *, text_type, before_call=None):
        if before_call:
            before_call()
        self.calls += 1
        self.input_tokens += sum(len(text) for text in texts)
        self.seen.append((text_type, list(texts)))
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return [[1.0, 0.0] if text_type == "query" or "包装" in text else [0.0, 1.0]
                for text in texts]


def test_cache_only_copy_reuses_identical_text_and_never_calls_provider(setup_retrieval,tmp_path):
    lexical,provider,source=setup_retrieval
    source.build(lexical,provider,today=TODAY)
    before=source.path.read_bytes();calls=provider.calls
    mutate(lexical,lambda corpus:corpus['sources'][0]['sections'].append(
        {**corpus['sources'][0]['sections'][0],'id':'identical-alias'}))
    output=VectorStore(tmp_path/'candidate-vectors.sqlite3')
    result=output.copy_cached(lexical,source,provider,today=TODAY)
    assert result['chunk_count']==3 and result['content_alias_reuse']==1 and result['calls']==0
    assert result['embedded_count']==0 and provider.calls==calls and source.path.read_bytes()==before
    copied=output.load(lexical.export_documents(today=TODAY),provider)
    assert copied['fda-label:identical-alias']==copied['fda-label:servings']


def test_cache_only_copy_does_not_reuse_whole_paragraph_vector_for_shorter_text(setup_retrieval,tmp_path):
    lexical,provider,source=setup_retrieval
    source.build(lexical,provider,today=TODAY)
    output=VectorStore(tmp_path/'candidate-vectors.sqlite3')
    output.copy_cached(lexical,source,provider,today=TODAY)
    before=output.path.read_bytes();calls=provider.calls
    mutate(lexical,lambda corpus:corpus['sources'][0]['sections'][0].update(summary='阅读包装'))
    with pytest.raises(VectorCacheUnavailable,match='incomplete'):
        output.copy_cached(lexical,source,provider,today=TODAY)
    assert output.path.read_bytes()==before and provider.calls==calls


def test_cache_only_copy_rejects_model_mismatch_and_corruption_before_writing(setup_retrieval,tmp_path):
    lexical,provider,source=setup_retrieval
    source.build(lexical,provider,today=TODAY)
    output=VectorStore(tmp_path/'candidate-vectors.sqlite3')
    with pytest.raises(VectorCacheUnavailable,match='identity'):
        output.copy_cached(lexical,source,FakeEmbedding('another-model'),today=TODAY)
    assert not output.path.exists()
    with sqlite3.connect(source.path) as connection:
        connection.execute("UPDATE vectors SET vector_hash=?",('0'*64,))
    with pytest.raises(VectorCacheUnavailable,match='invalid'):
        output.copy_cached(lexical,source,provider,today=TODAY)
    assert not output.path.exists()


@pytest.fixture
def setup_retrieval(tmp_path):
    corpus = {"version": "test-v1", "sources": [{
        "id": "fda-label", "title": "营养标签", "original_title": "Public test fixture",
        "publisher": "FDA", "url": "https://www.fda.gov/fixture", "topic": "nutrition",
        "source_version": "fixture-v1", "reviewed_on": "2026-09-17", "review_due": "2027-03-16",
        "status": "approved", "scope": "Public synthetic fixture", "license": "fixture",
        "license_url": "https://www.fda.gov/policy", "notice": "Synthetic summary for tests",
        "sections": [
            {"id": "servings", "title": "标签份数", "locator": "1", "summary": "阅读包装核对每份大小。",
             "keywords": ["营养标签", "份数"]},
            {"id": "protein", "title": "蛋白质来源", "locator": "2", "summary": "公开食物分类示例。",
             "keywords": ["蛋白质", "豆类"]},
        ]}]}
    source = tmp_path / "sources.json"
    source.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    lexical = LocalKnowledgeRetriever(source, tmp_path / "fts.sqlite3")
    provider = FakeEmbedding()
    store = VectorStore(tmp_path / "vectors.sqlite3")
    return lexical, provider, store


def mutate(lexical, callback):
    corpus = json.loads(lexical.source_path.read_text(encoding="utf-8"))
    callback(corpus)
    lexical.source_path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")


def test_default_lexical_snapshot_preserves_existing_search(setup_retrieval):
    lexical, provider, _ = setup_retrieval
    baseline = lexical.search("营养标签 两份", today=TODAY)
    snapshot = lexical.search_snapshot("营养标签 两份", today=TODAY)
    assert snapshot.hits == baseline and baseline[0].chunk_id == "fda-label:servings"
    assert lexical.terms("ＭＥＴ 营养标签") == search_terms("ＭＥＴ 营养标签")
    assert snapshot.metadata["mode"] == "lexical"
    assert "营养标签 两份" not in json.dumps(snapshot.metadata, ensure_ascii=False)
    assert not provider.calls


def test_export_has_all_active_chunks_and_same_fields_as_lexical(setup_retrieval):
    lexical, _, _ = setup_retrieval
    def extend(corpus):
        template = corpus["sources"][0]["sections"][0]
        corpus["sources"][0]["sections"] = [{**template, "id": f"chunk-{index:02d}"} for index in range(25)]
    mutate(lexical, extend)
    assert len(lexical.search("", 20, today=TODAY)) == 20
    documents = lexical.export_documents(today=TODAY)
    assert len(documents) == len(lexical.all_chunks(today=TODAY)) == 25
    assert documents[0].text == "标签份数 阅读包装核对每份大小。 营养标签 份数"
    assert not lexical.all_chunks(topic="training", today=TODAY)


def test_missing_cache_falls_back_before_any_paid_query(setup_retrieval):
    lexical, provider, store = setup_retrieval
    hybrid = HybridKnowledgeRetriever(lexical, provider, store)
    snapshot = hybrid.search_snapshot("营养标签 两份", today=TODAY)
    assert snapshot.hits == lexical.search("营养标签 两份", today=TODAY)
    assert snapshot.metadata["degraded"] and snapshot.metadata["reason"] == "vector_cache_missing"
    assert snapshot.metadata["embedding_called"] is False
    assert provider.calls == 0 and not store.path.exists()


def test_explicit_build_cache_reuse_restart_and_rrf(setup_retrieval):
    lexical, provider, store = setup_retrieval
    build = store.build(lexical, provider, today=TODAY, batch_size=1)
    assert build["chunk_count"] == 2 and build["embedded_count"] == 2 and build["calls"] == 2
    repeated = store.build(lexical, provider, today=TODAY)
    assert repeated["cache_hits"] == 2 and repeated["calls"] == 0
    restarted = VectorStore(store.path)
    hybrid = HybridKnowledgeRetriever(lexical, provider, restarted)
    snapshot = hybrid.search_snapshot("整包怎么看", today=TODAY)
    assert not lexical.search("整包怎么看", today=TODAY)
    assert snapshot.hits[0].chunk_id == "fda-label:servings"
    assert snapshot.metadata["mode"] == "hybrid" and not snapshot.metadata["degraded"]
    assert snapshot.metadata["embedding_called"] is True
    assert snapshot.metadata["rankings"][0]["dense_rank"] == 1
    assert snapshot.metadata["rankings"][0]["rrf_score"] == pytest.approx(1 / 61)
    assert provider.seen[-1] == ("query", ["整包怎么看"])
    with sqlite3.connect(store.path) as connection:
        assert set(row[0] for row in connection.execute("SELECT length(vector) FROM vectors")) == {8}
        dump = "\n".join(connection.iterdump())
    assert "整包怎么看" not in dump


def test_rrf_adds_rank_contributions_and_deduplicates(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    snapshot = HybridKnowledgeRetriever(lexical, provider, store).search_snapshot("营养标签 两份", today=TODAY)
    first = snapshot.metadata["rankings"][0]
    assert first["chunk_id"] == "fda-label:servings"
    assert first["lexical_rank"] == first["dense_rank"] == 1
    assert first["rrf_score"] == pytest.approx(2 / 61)
    assert len(snapshot.hits) == len({hit.chunk_id for hit in snapshot.hits})


def test_model_identity_separates_builds(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    second = FakeEmbedding("test-embedding-b")
    snapshot = HybridKnowledgeRetriever(lexical, second, store).search_snapshot("标签份数", today=TODAY)
    assert snapshot.metadata["reason"] == "embedding_identity_mismatch" and not second.calls
    store.build(lexical, second, today=TODAY)
    assert store.load(lexical.export_documents(today=TODAY), provider)
    assert store.load(lexical.export_documents(today=TODAY), second)


def test_changed_content_needs_explicit_rebuild(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    initial_calls = provider.calls
    mutate(lexical, lambda corpus: corpus["sources"][0]["sections"][0].update(summary="包装修订后的公开内容。"))
    snapshot = HybridKnowledgeRetriever(lexical, provider, store).search_snapshot("标签份数", today=TODAY)
    assert snapshot.metadata["reason"] == "vector_cache_incomplete"
    assert provider.calls == initial_calls
    assert store.build(lexical, provider, today=TODAY)["embedded_count"] == 1


@pytest.mark.parametrize("action", ["withdraw", "expire", "remove"])
def test_source_loss_filters_cached_vectors_without_build(setup_retrieval, action):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    before = provider.calls
    def change(corpus):
        if action == "withdraw":
            corpus["sources"][0]["status"] = "withdrawn"
        elif action == "expire":
            corpus["sources"][0].update(reviewed_on="2025-10-01", review_due="2026-03-01")
        else:
            corpus["sources"] = []
    mutate(lexical, change)
    hybrid = HybridKnowledgeRetriever(lexical, provider, store)
    assert not hybrid.search("标签份数", today=TODAY)
    assert not hybrid.get_chunks(["fda-label:servings"], today=TODAY)
    assert provider.calls == before


def test_source_withdrawn_during_query_cannot_be_returned(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    provider.on_call = lambda: mutate(lexical, lambda corpus: corpus["sources"][0].update(status="withdrawn"))
    snapshot = HybridKnowledgeRetriever(lexical, provider, store).search_snapshot("标签份数", today=TODAY)
    assert not snapshot.hits and snapshot.metadata["reason"] == "sources_changed"


def test_withdrawal_during_failed_provider_also_filters_fallback(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    provider.on_call = lambda: mutate(lexical, lambda corpus: corpus["sources"][0].update(status="withdrawn"))
    provider.error = ModelError("MODEL_TIMEOUT", "synthetic failure")
    snapshot = HybridKnowledgeRetriever(lexical, provider, store).search_snapshot("标签份数", today=TODAY)
    assert not snapshot.hits and snapshot.metadata["reason"] == "sources_changed"
    assert snapshot.metadata["embedding_called"] is True


def test_catalog_reports_local_cache_readiness_without_provider_calls(setup_retrieval):
    lexical, provider, store = setup_retrieval
    hybrid = HybridKnowledgeRetriever(lexical, provider, store)
    missing = hybrid.catalog(today=TODAY)
    assert missing["requested_method"] == "hybrid_rrf" and missing["vector_status"] == "degraded"
    assert missing["degraded_reason"] == "vector_cache_missing" and provider.calls == 0
    store.build(lexical, provider, today=TODAY)
    before = provider.calls
    assert hybrid.catalog(today=TODAY)["vector_status"] == "ready"
    assert provider.calls == before


def test_large_finite_vector_does_not_overflow_during_normalization():
    normalized = unit_vector([1e308, 1e308], 2)
    assert sum(value * value for value in normalized) == pytest.approx(1)


def test_corrupt_cache_does_not_call_provider(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE vectors SET vector=?", (b"broken",))
    before = provider.calls
    snapshot = HybridKnowledgeRetriever(lexical, provider, store).search_snapshot("标签份数", today=TODAY)
    assert snapshot.metadata["reason"] == "vector_cache_invalid" and snapshot.hits
    assert provider.calls == before


def test_private_database_cannot_be_replaced(setup_retrieval):
    lexical, provider, store = setup_retrieval
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE private (value TEXT)")
        connection.execute("INSERT INTO private VALUES ('synthetic-private')")
    before = store.path.read_bytes()
    with pytest.raises(VectorCacheUnavailable):
        store.build(lexical, provider, today=TODAY)
    assert store.path.read_bytes() == before and not provider.calls


def test_failed_build_preserves_previous_model_and_index(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    before = store.path.read_bytes()
    second = FakeEmbedding("new-model")
    second.error = ModelError("MODEL_TIMEOUT", "synthetic failure")
    with pytest.raises(ModelError):
        store.build(lexical, second, today=TODAY)
    assert store.path.read_bytes() == before
    assert store.load(lexical.export_documents(today=TODAY), provider)


def test_document_change_during_build_does_not_publish(setup_retrieval):
    lexical, provider, store = setup_retrieval
    provider.on_call = lambda: mutate(lexical, lambda corpus: corpus["sources"][0].update(status="withdrawn"))
    with pytest.raises(VectorCacheUnavailable, match="sources_changed"):
        store.build(lexical, provider, today=TODAY)
    assert not store.path.exists()


def test_per_request_quota_callback_is_isolated_and_denial_propagates(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    hybrid = HybridKnowledgeRetriever(lexical, provider, store)
    seen = []
    first = hybrid.for_request(lambda: seen.append("first"))
    def denied():
        seen.append("second")
        raise HTTPException(429, "quota")
    second = hybrid.for_request(denied)
    first.search("标签份数", today=TODAY)
    before = provider.calls
    with pytest.raises(HTTPException) as error:
        second.search("标签份数", today=TODAY)
    assert error.value.status_code == 429 and provider.calls == before
    assert seen == ["first", "second"] and hybrid.before_call is None


def test_query_provider_failure_has_safe_fallback_metadata(setup_retrieval):
    lexical, provider, store = setup_retrieval
    store.build(lexical, provider, today=TODAY)
    provider.error = ModelError("MODEL_TIMEOUT", "never expose this request body")
    snapshot = HybridKnowledgeRetriever(lexical, provider, store).search_snapshot("标签份数", today=TODAY)
    assert snapshot.hits and snapshot.metadata["reason"] == "embedding_query_failed"
    assert "never expose" not in json.dumps(snapshot.metadata)
    assert snapshot.metadata["embedding_called"] is True


@pytest.mark.parametrize("vector", [[0.0, 0.0], [float("nan"), 1.0], [1.0], [True, 1]])
def test_invalid_vectors_cannot_publish_a_build(setup_retrieval, vector):
    lexical, provider, store = setup_retrieval
    provider.embed = lambda texts, **kwargs: [vector for _ in texts]
    with pytest.raises(ValueError):
        store.build(lexical, provider, today=TODAY)
    assert not store.path.exists()


def test_jieba_is_optional_and_both_sides_use_same_tokenizer(setup_retrieval):
    pytest.importorskip("jieba")
    lexical, _, _ = setup_retrieval
    tokenizer = LocalKnowledgeRetriever(lexical.source_path, lexical.index_path.with_name("jieba.sqlite3"), tokenizer="jieba")
    assert "营养" in tokenizer.terms("营养标签")
    assert tokenizer.search("营养标签 两份", today=TODAY)[0].chunk_id == "fda-label:servings"
    assert tokenizer.search_snapshot("营养标签 两份", today=TODAY).metadata["tokenizer"] == "jieba"
