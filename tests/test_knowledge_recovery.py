import json
from dataclasses import replace

import httpx
import pytest

import api.knowledge as knowledge_api
from config import Settings
from model.deadlines import Deadline, bounded_timeout, use_deadline
from model.embeddings import QwenEmbeddingProvider
from model.factory import ModelError
from model.qwen import QwenTextModel
from rag.rag_service import RetrievalSnapshot
from services.knowledge_answers import answer_question
from services.knowledge_policy import KnowledgePolicy, rewrite_change
from test_foundation import application, client
from test_knowledge import library
from test_knowledge_answers import ask, calls, qa
from test_knowledge_loop import FIRST, SECOND, QUESTION, REVISED, ScriptedModel, answered, revise


class Pages:
    def __init__(self, base, pages, *, before_call=None, queries=None):
        self.base, self.pages, self.before_call = base, pages, before_call
        self.queries = [] if queries is None else queries

    def for_request(self, before_call):
        return type(self)(self.base, self.pages, before_call=before_call, queries=self.queries)

    def search_snapshot(self, query, limit=6):
        if self.before_call:
            self.before_call()
        ids = self.pages[len(self.queries)]
        self.queries.append(query)
        return RetrievalSnapshot(self.base.get_chunks(ids), {"mode": "hybrid", "embedding_called": bool(self.before_call)})

    def get_chunks(self, ids, **kwargs):
        return self.base.get_chunks(ids, **kwargs)


def setup_recovery(qa, monkeypatch, pages, *outputs):
    client, app, _, _ = qa
    model = ScriptedModel(*outputs)
    retriever = Pages(app.state.knowledge, pages)
    app.state.knowledge = retriever
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda _: model)
    return client, app, model, retriever


def test_zero_then_answer_checks_original_question_and_counts_both_embeddings(qa, monkeypatch):
    client, app, model, retriever = setup_recovery(qa, monkeypatch, [[], [SECOND]], revise(), answered())
    result = ask(client).json()
    assert result["status"] == "answered" and [s["chunk_id"] for s in result["sources"]] == [SECOND]
    assert retriever.queries == [QUESTION, REVISED]
    assert [m["question"] for m in model.messages] == [QUESTION, QUESTION]
    trace = result["retrieval"]
    assert trace["calls"] == {"retrieval": 2, "embedding": 2, "judgment": 2}
    assert trace["max_rounds"] == 2 and trace["time_budget_ms"] == 45000
    assert trace["policy_version"] == "knowledge-loop-v2" and calls(app) == 4
    assert REVISED not in json.dumps(trace, ensure_ascii=False)


def test_second_empty_search_does_not_call_judgment(qa, monkeypatch):
    client, app, model, retriever = setup_recovery(qa, monkeypatch, [[], []], revise())
    value = ask(client).json()
    assert value["status"] == "insufficient" and value["retrieval"]["stop_reason"] == "no_candidates"
    assert len(retriever.queries) == 2 and len(model.messages) == 1 and calls(app) == 3


def test_no_evidence_cannot_be_answered_with_an_invented_citation(qa, monkeypatch):
    client, app, _, retriever = setup_recovery(qa, monkeypatch, [[]], answered())
    result = ask(client)
    assert result.status_code == 502 and result.json()["detail"]["code"] == "MODEL_INVALID_OUTPUT"
    assert len(retriever.queries) == 1 and calls(app) == 2


def test_reordering_identical_evidence_is_not_new_evidence(qa, monkeypatch):
    client, app, model, _ = setup_recovery(qa, monkeypatch, [[FIRST, SECOND], [SECOND, FIRST]], revise())
    value = ask(client).json()
    assert value["retrieval"]["stop_reason"] == "no_new_evidence" and len(model.messages) == 1
    assert calls(app) == 3


@pytest.mark.parametrize("original,revised,reason", [
    ("力量训练频率", "力量训练频率？", "same_query"),
    ("力量训练频率", "成年人每周力量训练频率", "temporal_change"),
    ("每天30分钟活动有什么作用", "每天60分钟活动有什么作用", "numeric_change"),
    ("每天活动的作用", "每周活动的作用", "temporal_change"),
    ("不吃肉的蛋白质来源", "吃肉的蛋白质来源", "negation_change"),
    ("营养标签2000千卡", "营养标签千卡", "numeric_change"),
    ("每天30分钟活动", "每天30小时活动", "numeric_change"),
    ("训练后补水", "训练前补水", "temporal_change"),
    ("力量训练", "？！", "same_query"),
])
def test_limited_rewrite_invariants(original, revised, reason):
    assert rewrite_change(original, revised) == reason


def test_equivalent_period_and_full_width_numbers_are_allowed():
    assert rewrite_change("每天３０分钟活动", "每日30分钟身体活动") is None


def test_anaerobic_term_is_not_misread_as_a_negative_restriction():
    assert rewrite_change("无氧训练频率", "力量训练的频率") is None
    assert rewrite_change("无乳糖蛋白质来源", "蛋白质来源") == "negation_change"


def test_rejected_numeric_rewrite_spends_no_second_retrieval(qa, monkeypatch):
    client, app, model, retriever = setup_recovery(qa, monkeypatch, [[FIRST]], revise("活动60分钟益处"))
    value = ask(client, "活动30分钟益处").json()
    assert value["retrieval"]["attempts"][0]["rewrite_rejection"] == "numeric_change"
    assert len(retriever.queries) == len(model.messages) == 1 and calls(app) == 2


def test_no_hit_recovery_respects_remaining_quota(qa, monkeypatch):
    client, app, model, retriever = setup_recovery(qa, monkeypatch, [[], [SECOND]], revise(), answered())
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=2)
    assert ask(client).status_code == 429
    assert len(retriever.queries) == len(model.messages) == 1 and calls(app) == 2


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Records:
    def get_profile(self):
        return {}


def test_late_model_output_is_not_displayed_or_retried(qa):
    _, app, _, _ = qa
    clock = Clock()
    model = ScriptedModel(answered(), on_call=lambda _: setattr(clock, "value", 46))
    retriever = Pages(app.state.knowledge, [[SECOND]])
    audit = {}
    with pytest.raises(ModelError) as error:
        answer_question(QUESTION, retriever, Records(), lambda: model, "synthetic",
                        deadline=Deadline(clock=clock), audit=audit)
    assert error.value.code == "KNOWLEDGE_TIME_BUDGET"
    assert audit["elapsed_ms"] == 46000 and audit["stop_reason"] == "KNOWLEDGE_TIME_BUDGET"
    assert len(model.messages) == len(retriever.queries) == 1


def test_deadline_before_first_search_has_no_work(qa):
    _, app, _, _ = qa
    clock = Clock()
    deadline = Deadline(clock=clock)
    clock.value = 46
    retriever = Pages(app.state.knowledge, [])
    with pytest.raises(ModelError):
        answer_question(QUESTION, retriever, Records(), lambda: pytest.fail("no model"), "synthetic", deadline=deadline)
    assert not retriever.queries


def test_context_deadline_is_restored_after_error():
    clock = Clock()
    with pytest.raises(ModelError):
        with use_deadline(Deadline(3, clock=clock)):
            assert bounded_timeout(20) == 3
            clock.value = 4
            bounded_timeout(20)
    assert bounded_timeout(20) == 20


@pytest.mark.parametrize("provider_kind", ["text", "embedding"])
def test_http_deadline_caps_network_timeout_and_rejects_slow_body(tmp_path, provider_kind):
    clock = Clock()
    captured = []

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            clock.value = 4
            yield b'{}'

    def handle(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, stream=SlowBody())

    settings = Settings(database_path=tmp_path / "unused.sqlite3", qwen_api_key="test-only", embedding_dimensions=64)
    transport = httpx.MockTransport(handle)
    with pytest.raises(ModelError) as error:
        with use_deadline(Deadline(3, clock=clock)):
            if provider_kind == "text":
                QwenTextModel(settings, transport=transport).generate(system_prompt="test", message="synthetic")
            else:
                QwenEmbeddingProvider(settings, transport=transport).embed(["synthetic"], text_type="query")
    assert error.value.code == "KNOWLEDGE_TIME_BUDGET"
    assert len(captured) == 1 and all(0 < value <= 3 for value in captured[0].values())


def test_single_round_policy_keeps_zero_hit_baseline_free(qa):
    _, app, _, _ = qa
    value = answer_question(QUESTION, Pages(app.state.knowledge, [[]]), Records(),
        lambda: pytest.fail("baseline no-hit must not judge"), "synthetic", policy=KnowledgePolicy(max_rounds=1))
    assert value["status"] == "insufficient" and not value["model_called"]
