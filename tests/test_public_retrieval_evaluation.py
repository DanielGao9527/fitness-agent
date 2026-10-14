"""Public pilot adapter parity; synthetic vectors test contracts, not model quality."""
import json

import pytest

from config import ROOT, Settings
from model.embeddings import EmbeddingError
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import LocalKnowledgeRetriever
from rag.vector_store import VectorStore
from scripts import evaluate_t2_pilot as pilot_script
from scripts.evaluate_retrieval import CachedQueries
from test_hybrid_retrieval import TODAY, FakeEmbedding


@pytest.fixture(params=["bigram", "jieba"])
def aligned_retrievers(request, tmp_path):
    # Match the indexed document universe as well as each document's text. The
    # production index includes approved future/expired rows in BM25 statistics;
    # filtering only the pilot export would otherwise compare different corpora.
    raw = json.loads((ROOT / "data/knowledge/sources.json").read_text(encoding="utf-8"))
    raw["sources"] = [source for source in raw["sources"] if source["status"] == "approved"
                      and source["reviewed_on"] <= TODAY.isoformat() <= source["review_due"]]
    # Equal synthetic sections give both indexes more than twenty guaranteed
    # matches, so truncation and deterministic tie-breaking are actually exercised.
    raw["sources"][0]["sections"].extend([
        {"id": f"ranking-fixture-{index:02d}", "title": "Synthetic ranking fixture",
         "locator": "synthetic test only", "summary": "alphafixture betafixture",
         "keywords": ["alphafixture", "betafixture"]} for index in range(25)])
    source = tmp_path / "public-sources.json"
    source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    production = LocalKnowledgeRetriever(source, tmp_path / "production.sqlite3", tokenizer=request.param)
    documents = production.export_documents(today=TODAY)
    corpus = [{"id": doc.hit.chunk_id, "text": doc.text} for doc in documents]
    assert len(corpus) > 20 and all(len(row["text"]) <= 6000 for row in corpus)
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps({"corpus": corpus}, ensure_ascii=False), encoding="utf-8")
    adapter = pilot_script.PilotLexical(path, corpus, tokenizer=request.param)
    try:
        yield production, adapter
    finally:
        adapter.connection.close()


@pytest.mark.parametrize("query", [
    "力量训练一般一周做几次", "哪些食物含蛋白质", "营养标签 两份",
    "跑步时只能说几个字就要换气，这算什么强度", "ＭＥＴ met",
    "火星飞船引擎", "请问怎么", "??? OR NOT : *",
    " ".join(["营养标签", "蛋白质", *[f"token{i}" for i in range(140)]]),
])
def test_pilot_and_production_rank_identical_texts_the_same_at_top_twenty(aligned_retrievers, query):
    production, adapter = aligned_retrievers
    assert adapter.terms(query) == production.terms(query)
    actual = adapter.search_snapshot(query, limit=20)
    expected = production.search_snapshot(query, limit=20, today=TODAY)
    assert [hit.chunk_id for hit in actual.hits] == [hit.chunk_id for hit in expected.hits]
    assert actual.metadata["tokenizer"] == expected.metadata["tokenizer"]
    assert [row["lexical_rank"] for row in actual.metadata["rankings"]] == list(range(1, len(actual.hits) + 1))
    assert len(actual.hits) <= 20


def test_pilot_top_twenty_matches_production_when_more_candidates_match(aligned_retrievers):
    production, adapter = aligned_retrievers
    query = "alphafixture betafixture"
    actual = adapter.search_snapshot(query, limit=20)
    expected = production.search_snapshot(query, limit=20, today=TODAY)
    assert len(actual.hits) == len(expected.hits) == 20
    assert [hit.chunk_id for hit in actual.hits] == [hit.chunk_id for hit in expected.hits]
    assert actual.hits[-1].chunk_id.endswith("ranking-fixture-19")


@pytest.mark.parametrize("replay", [False, True])
def test_production_vector_store_and_hybrid_run_through_pilot_adapter(
        aligned_retrievers, tmp_path, replay):
    production, adapter = aligned_retrievers
    provider = FakeEmbedding()
    store = VectorStore(tmp_path / "pilot-vectors.sqlite3")
    built = store.build(adapter, provider, batch_size=10)
    assert built["embedded_count"] == len(adapter.documents)
    # The same ID/text/content hash contract lets production load vectors built
    # through the public adapter without embedding either document set twice.
    assert set(store.load(production.export_documents(today=TODAY), provider)) == set(adapter.hits)
    before = provider.calls
    query = "营养标签 两份"
    query_provider = CachedQueries(provider, {query: [1.0, 0.0]}) if replay else provider
    actual = HybridKnowledgeRetriever(adapter, query_provider, store).search_snapshot(query, limit=20)
    expected = HybridKnowledgeRetriever(production, query_provider, store).search_snapshot(query, limit=20, today=TODAY)
    assert actual.metadata["mode"] == expected.metadata["mode"] == "hybrid"
    assert not actual.metadata["degraded"] and not expected.metadata["degraded"]
    assert actual.metadata["embedding_called"] is (not replay)
    assert [hit.chunk_id for hit in actual.hits] == [hit.chunk_id for hit in expected.hits]
    assert actual.metadata["rankings"] == expected.metadata["rankings"]
    assert provider.calls == before + (0 if replay else 2)
    if not replay:
        assert provider.seen[-2:] == [("query", [query]), ("query", [query])]
    assert not adapter.index_path.exists(), "The public lexical adapter must stay in memory"


def test_public_pilot_uses_the_same_document_prefix_for_every_method(tmp_path, monkeypatch, capsys):
    path = tmp_path / "pilot.json"
    full_text = "包装营养标签 " + "公开文本 " * 1500 + " ONLY_AFTER_PREFIX"
    original = {"source": {"dataset": "synthetic-contract-fixture"}, "sampling": {"seed": 0},
                "corpus": [{"id": "1", "text": full_text}, {"id": "2", "text": "蛋白质来源公开食物分类"}],
                "queries": [{"id": "q1", "text": "营养标签 包装", "relevant_ids": ["1"]},
                            {"id": "q2", "text": "蛋白质来源", "relevant_ids": ["2"]}]}
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    unchanged = path.read_bytes()
    provider = FakeEmbedding()
    settings = Settings(database_path=tmp_path / "unused-private.sqlite3")
    monkeypatch.setattr(pilot_script.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(pilot_script, "QwenEmbeddingProvider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(pilot_script.sys, "argv", ["evaluate_t2_pilot.py", "--pilot", str(path), "--live"])
    pilot_script.main()
    report = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["preprocessing"] == report["preprocessing"]
    assert report["preprocessing"]["max_document_characters"] == 6000
    assert report["preprocessing"]["truncated_documents"] == 1
    assert report["preprocessing"]["same_text_for_all_methods"]
    assert set(report["metrics"]) == {"A_bigram", "B_jieba", "C_dense", "D_bigram_rrf", "E_jieba_rrf"}
    assert len(report["runs"]) == 10 and report["queries"] == 2
    assert provider.seen[0] == ("document", [full_text[:6000], original["corpus"][1]["text"]])
    assert provider.seen[1] == ("query", [row["text"] for row in original["queries"]])
    assert provider.calls == 2, "Ranking replays must not re-encode public queries"
    assert path.read_bytes() == unchanged and not settings.database_path.exists()
    assert not (tmp_path / "pilot-lexical.sqlite3").exists()


def test_public_batch_checkpoint_reuses_after_restart_without_provider_or_quota(tmp_path):
    provider = FakeEmbedding()
    cached = pilot_script.CachedPublicBatches(provider, tmp_path / "batches")
    reserved = []
    expected = cached.embed(["公开包装资料", "另一段公开资料"], text_type="document", before_call=lambda: reserved.append(1))
    assert provider.calls == 1 and reserved == [1]
    restarted_provider = FakeEmbedding(provider.identity)
    restarted_provider.error = AssertionError("A cache hit must not contact the provider")
    restarted = pilot_script.CachedPublicBatches(restarted_provider, cached.directory)
    result = restarted.embed(["公开包装资料", "另一段公开资料"], text_type="document",
                             before_call=lambda: pytest.fail("A cache hit must not consume quota"))
    assert result == expected and restarted.reused_batches == 1
    assert restarted.calls == restarted_provider.calls == 0


@pytest.mark.parametrize("change", ["role", "model", "text", "order"])
def test_public_batch_checkpoint_does_not_cross_identity_or_input_boundaries(tmp_path, change):
    provider = FakeEmbedding()
    cached = pilot_script.CachedPublicBatches(provider, tmp_path / "batches")
    texts = ["第一段公开资料", "第二段公开资料"]
    cached.embed(texts, text_type="document")
    assert provider.calls == 1
    role = "document"
    if change == "role":
        role = "query"
    elif change == "model":
        provider = FakeEmbedding("different-public-model")
        cached = pilot_script.CachedPublicBatches(provider, cached.directory)
    elif change == "text":
        texts = [texts[0], "已经修改的公开资料"]
    else:
        texts = list(reversed(texts))
    before = provider.calls
    cached.embed(texts, text_type=role)
    assert provider.calls == before + 1 and cached.reused_batches == 0
    assert len(list(cached.directory.glob("*.json"))) == 2


@pytest.mark.parametrize("change", ["vector", "checksum"])
def test_public_batch_checkpoint_rejects_checksum_damage_without_auto_reencoding(tmp_path, change):
    provider = FakeEmbedding()
    cached = pilot_script.CachedPublicBatches(provider, tmp_path / "batches")
    cached.embed(["公开资料"], text_type="document")
    path = next(cached.directory.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if change == "vector":
        payload["vectors"][0] = [1.0, 0.0]
    else:
        payload["checksum"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    corrupted = path.read_bytes()
    with pytest.raises(ValueError, match="corrupt"):
        cached.embed(["公开资料"], text_type="document")
    assert provider.calls == 1 and cached.reused_batches == 0
    assert path.read_bytes() == corrupted


def test_public_batch_failure_preserves_prior_checkpoint_and_does_not_retry(tmp_path):
    provider = FakeEmbedding()
    cached = pilot_script.CachedPublicBatches(provider, tmp_path / "batches")
    expected = cached.embed(["已完成的公开资料"], text_type="document")
    first_path = next(cached.directory.glob("*.json"))
    first_bytes = first_path.read_bytes()
    provider.error = EmbeddingError("MODEL_TIMEOUT", "synthetic public timeout", 504)
    with pytest.raises(EmbeddingError, match="synthetic public timeout"):
        cached.embed(["尚未完成的公开资料"], text_type="document")
    assert provider.calls == 2, "One failed request must not automatically retry"
    assert list(cached.directory.glob("*.json")) == [first_path]
    assert first_path.read_bytes() == first_bytes and not list(cached.directory.glob("*.tmp"))
    resumed_provider = FakeEmbedding(provider.identity)
    resumed = pilot_script.CachedPublicBatches(resumed_provider, cached.directory)
    assert resumed.embed(["已完成的公开资料"], text_type="document") == expected
    assert resumed_provider.calls == 0
    resumed.embed(["尚未完成的公开资料"], text_type="document")
    assert resumed_provider.calls == 1 and len(list(cached.directory.glob("*.json"))) == 2
