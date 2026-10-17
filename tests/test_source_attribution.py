"""Source corrections exercise the real extract/guard/cache paths, without cloud calls.

These are contract regressions, not human qrels or a new semantic quality score.
"""
import json
from datetime import date

import pytest

from rag.hybrid import HybridKnowledgeRetriever
from rag.vector_store import VectorCacheUnavailable, VectorStore
from test_foundation import application, client
from test_hybrid_retrieval import FakeEmbedding
from test_knowledge import library, mutate
from test_knowledge_answers import qa, ask
from test_training_program import facts
from services.training_recommendations import build


TODAY = date(2026, 10, 17)


@pytest.mark.parametrize("question,chunk_id,removed,expected", [
    ("引体向上的动作要点", "r118-pullup:basics", "下拉备选", "确认单杠固定"),
    ("滑轮器械三头肌下压的动作要点", "mayo-cable-triceps:basics", "适配直杆", "文字转录未明确附件种类"),
])
def test_displayed_extract_corrects_attribution_without_model_rewriting(qa, question, chunk_id, removed, expected):
    client, app, model, _ = qa
    model.output = json.dumps({"status": "answered", "chunk_ids": [chunk_id]})
    response = ask(client, question)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "answered" and not data["saved"]
    source, = data["sources"]
    assert removed not in source["excerpt"] and expected in source["excerpt"]
    assert "项目" in source["scope"]
    offered = next(hit for hit in model.messages[0]["evidence"] if hit["chunk_id"] == chunk_id)
    assert (source["excerpt"], source["scope"]) == (offered["excerpt"], offered["scope"])
    if chunk_id == "mayo-cable-triceps:basics":
        assert "直杆" not in source["title"]
        assert "不代表Mayo指定该附件" in source["scope"]


def test_provenance_change_during_answer_rejects_old_selected_extract(qa):
    client, app, model, _ = qa
    chunk_id = "r118-goblet-squat:basics"
    model.output = json.dumps({"status": "answered", "chunk_ids": [chunk_id]})

    def change_scope():
        mutate(app.state.knowledge, lambda data: next(
            item for item in data["sources"] if item["id"] == "r118-goblet-squat"
        ).update(scope="适用范围刚刚发生变动，旧引用不可继续使用。"))

    model.on_call = change_scope
    response = ask(client, "哑铃高脚杯深蹲的动作要点")
    assert response.status_code == 409
    assert "KNOWLEDGE_CHANGED" in response.text


@pytest.mark.parametrize("venue,focus,present,absent", [
    ("单杠", "背", "pullup", "lat-pulldown"),
    ("龙门架，可调滑轮，直杆把手", "三头", "cable-triceps", "rope-pushdown"),
    ("龙门架，可调滑轮，没有直杆把手", "三头", None, "cable-triceps"),
    ("杠铃，深蹲架，没有保护杠", "腿", None, "barbell-squat"),
    ("腿举机", "腿", None, "leg-press-calf"),
])
def test_corrected_cues_preserve_equipment_and_protection_guards(library, venue, focus, present, absent):
    current = facts()
    current["profile"]["equipment"] = venue
    current["request"] = {"focus": focus}
    evidence = {hit.chunk_id: hit.model_dump() for hit in library.all_chunks(today=TODAY)}
    result = build(current, evidence)
    listed = result["exercises"] + [
        alternative for item in result["exercises"] for alternative in item.get("alternatives", [])
    ]
    assert absent not in {item["id"] for item in listed}
    if present:
        assert present in {item["id"] for item in listed}
    if present == "pullup":
        action = next(item for item in listed if item["id"] == present)
        assert "下拉备选" not in action["cue"]
        assert "停止本动作" in action["cue"]
    if present == "cable-triceps":
        action = next(item for item in listed if item["id"] == present)
        assert "本项目选择直杆变式" in action["cue"]
        assert "原资料文字未指定附件" in action["cue"]


def test_content_correction_rejects_legacy_vectors_and_falls_back_before_query_call(library, tmp_path):
    # Only the two corrected source texts are restored in this synthetic old cache.
    # Neither frozen fitness QA nor its labels are loaded or changed.
    original = library.source_path.read_bytes()

    def legacy(data):
        for source in data["sources"]:
            if source["id"] == "r118-pullup":
                source["sections"][0]["summary"] = "仅在能控制全程时采用；确认单杠固定，避免摆荡，无法完成就选下拉备选。"
            if source["id"] == "mayo-cable-triceps":
                source["title"] = source["sections"][0]["title"] = "绳索直杆下压"
                source["sections"][0]["summary"] = "使用适配直杆的滑轮器械，肘部贴近躯干，缓慢下压和回程，不前后摇摆。"
                source["sections"][0]["keywords"] = ["三头", "下压", "绳索", "直杆"]

    mutate(library, legacy)
    before = {doc.hit.chunk_id: doc.content_hash for doc in library.export_documents(today=TODAY)}
    provider = FakeEmbedding()
    store = VectorStore(tmp_path / "vectors.sqlite3")
    store.build(library, provider, today=TODAY)
    cached_bytes, calls = store.path.read_bytes(), provider.calls
    library.source_path.write_bytes(original)
    after = {doc.hit.chunk_id: doc.content_hash for doc in library.export_documents(today=TODAY)}
    assert before.keys() == after.keys()
    assert {key for key in before if before[key] != after[key]} == {
        "r118-pullup:basics", "mayo-cable-triceps:basics"}
    with pytest.raises(VectorCacheUnavailable, match="vector_cache_incomplete"):
        store.load(library.export_documents(today=TODAY), provider)
    snapshot = HybridKnowledgeRetriever(library, provider, store).search_snapshot(
        "三头 下压", today=TODAY)
    assert snapshot.metadata["mode"] == "lexical" and snapshot.metadata["degraded"]
    assert snapshot.metadata["reason"] == "vector_cache_incomplete"
    assert not snapshot.metadata["embedding_called"] and provider.calls == calls
    assert store.path.read_bytes() == cached_bytes
    assert any(hit.chunk_id == "mayo-cable-triceps:basics" for hit in snapshot.hits)


def test_metadata_only_attribution_invalidates_snapshot_but_keeps_exact_text_vectors(library, tmp_path):
    provider = FakeEmbedding()
    store = VectorStore(tmp_path / "vectors.sqlite3")
    before = library.export_documents(today=TODAY)
    metadata = library.snapshot_metadata(before, today=TODAY)
    store.build(library, provider, today=TODAY)
    cached_bytes, calls = store.path.read_bytes(), provider.calls
    mutate(library, lambda data: data["sources"][0].update(scope="更正后的项目规则归因；原动作文本不变。"))
    after = library.export_documents(today=TODAY)
    assert [doc.content_hash for doc in before] == [doc.content_hash for doc in after]
    assert metadata["corpus_hash"] != library.snapshot_metadata(after, today=TODAY)["corpus_hash"]
    assert len(store.load(after, provider)) == len(after)
    assert provider.calls == calls and store.path.read_bytes() == cached_bytes
