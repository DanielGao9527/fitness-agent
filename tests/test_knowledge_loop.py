"""Deterministic E3 replay: bounded search revisions never change source authority."""
import json
from dataclasses import replace

import api.knowledge as knowledge_api
from rag.rag_service import RetrievalSnapshot
from services.knowledge_answers import answer_question
from test_foundation import application, client
from test_knowledge import library, mutate
from test_knowledge_answers import ask, calls, qa


FIRST = "cdc-adults:aerobic"
SECOND = "cdc-adults:strength"
QUESTION = "力量训练频率"
REVISED = "成年人力量训练的频率"


class ScriptedModel:
    def __init__(self, *outputs, on_call=None):
        self.outputs = list(outputs)
        self.messages = []
        self.on_call = on_call

    def generate(self, *, system_prompt, message):
        assert "JSON Schema" in system_prompt
        self.messages.append(json.loads(message))
        if self.on_call:
            self.on_call(len(self.messages))
        return json.dumps(self.outputs.pop(0), ensure_ascii=False)


class TwoStageRetriever:
    def __init__(self, base, *, second=SECOND, queries=None, before_call=None):
        self.base, self.second = base, second
        self.queries = [] if queries is None else queries
        self.before_call = before_call

    def for_request(self, before_call):
        return type(self)(self.base, second=self.second, queries=self.queries, before_call=before_call)

    def search_snapshot(self, query, limit=6, *, topic="all", today=None):
        if self.before_call:
            self.before_call()  # Fake the authenticated query-embedding attempt.
        self.queries.append(query)
        key = FIRST if len(self.queries) == 1 else self.second
        hits = self.base.get_chunks([key])
        return RetrievalSnapshot(hits, {"mode": "hybrid", "requested_mode": "hybrid",
            "tokenizer": "jieba", "embedding_called": True, "degraded": False,
            "reason": None, "rankings": [{"chunk_id": key, "dense_rank": 1}]})

    def get_chunks(self, ids):
        return self.base.get_chunks(ids)


def revise(query=REVISED):
    return {"status": "insufficient", "chunk_ids": [],
            "retry_query": query, "retry_reason": "evidence_gap"}


def answered(key=SECOND):
    return {"status": "answered", "chunk_ids": [key]}


def install(qa, monkeypatch, *outputs, second=SECOND):
    client, app, _, _ = qa
    model = ScriptedModel(*outputs)
    retriever = TwoStageRetriever(app.state.knowledge, second=second)
    app.state.knowledge = retriever
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda _: model)
    return client, app, model, retriever


def test_insufficient_evidence_gets_one_bounded_revision_and_auditable_trace(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch, revise(), answered())
    result = ask(client, QUESTION)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["status"] == "answered"
    assert [source["chunk_id"] for source in value["sources"]] == [SECOND]
    assert retriever.queries == [QUESTION, REVISED]
    assert [message["question"] for message in model.messages] == [QUESTION, QUESTION]
    assert [attempt["candidate_ids"] for attempt in value["retrieval"]["attempts"]] == [[FIRST], [SECOND]]
    assert value["retrieval"]["attempts"][0]["rewrite_reason"] == "evidence_gap"
    assert value["retrieval"]["stop_reason"] == "answered"
    assert REVISED not in result.text and QUESTION not in json.dumps(value["retrieval"], ensure_ascii=False)
    assert calls(app) == 4  # Two simulated embeddings and two evidence judgments.


def test_unsafe_revision_is_rejected_without_second_search(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch, revise("肩受伤了怎么练"))
    value = ask(client, QUESTION).json()
    assert value["status"] == "insufficient" and not value["sources"]
    assert value["retrieval"]["stop_reason"] == "rewrite_rejected"
    assert retriever.queries == [QUESTION] and len(model.messages) == 1
    assert calls(app) == 2


def test_punctuation_only_revision_does_not_spend_second_embedding(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch, revise("力量训练频率？"))
    value = ask(client, QUESTION).json()
    assert value["retrieval"]["stop_reason"] == "rewrite_rejected"
    assert retriever.queries == [QUESTION] and len(model.messages) == 1
    assert calls(app) == 2


def test_same_candidates_stop_without_second_judgment(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch, revise(), second=FIRST)
    value = ask(client, QUESTION).json()
    assert value["status"] == "insufficient"
    assert value["retrieval"]["stop_reason"] == "no_new_evidence"
    assert retriever.queries == [QUESTION, REVISED] and len(model.messages) == 1
    assert calls(app) == 3


def test_second_revision_cannot_start_a_third_round(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch, revise(), revise("再换一个说法"))
    value = ask(client, QUESTION).json()
    assert value["status"] == "insufficient"
    assert value["retrieval"]["stop_reason"] == "retry_budget_exhausted"
    assert retriever.queries == [QUESTION, REVISED] and len(model.messages) == 2
    assert calls(app) == 4


def test_second_judgment_obeys_remaining_account_budget(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch, revise(), answered())
    app.state.settings = replace(app.state.settings, ai_user_daily_limit=3)
    result = ask(client, QUESTION)
    assert result.status_code == 429
    assert retriever.queries == [QUESTION, REVISED]
    assert len(model.messages) == 1 and calls(app) == 3


def test_invalid_retry_contract_never_starts_second_search(qa, monkeypatch):
    client, app, model, retriever = install(qa, monkeypatch,
        {"status": "answered", "chunk_ids": [FIRST],
         "retry_query": REVISED, "retry_reason": "evidence_gap"})
    result = ask(client, QUESTION)
    assert result.status_code == 502
    assert result.json()["detail"]["code"] == "MODEL_INVALID_OUTPUT"
    assert retriever.queries == [QUESTION] and len(model.messages) == 1


def test_changed_source_after_second_judgment_is_not_presented(qa, monkeypatch):
    client, app, _, _ = qa
    retriever = TwoStageRetriever(app.state.knowledge)
    app.state.knowledge = retriever
    model = ScriptedModel(revise(), answered(), on_call=lambda count: (
        mutate(retriever.base, lambda corpus: next(source for source in corpus["sources"]
            if source["id"] == "cdc-adults").update(status="withdrawn")) if count == 2 else None))
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda _: model)
    result = ask(client, QUESTION)
    assert result.status_code == 409
    assert SECOND not in result.text


def test_second_topic_is_checked_by_coach_boundary(qa):
    _, app, _, _ = qa
    retriever = TwoStageRetriever(app.state.knowledge, second="fda-label:energy")
    model = ScriptedModel(revise())

    class Records:
        def get_profile(self):
            return {}

    value = answer_question(QUESTION, retriever, Records(), lambda: model, "synthetic",
        candidate_boundary=lambda topics: "禁忌需先核对" if "nutrition" in topics else "")
    assert value["status"] == "context_pending"
    assert value["answer"] == "禁忌需先核对" and not value["sources"]
    assert value["retrieval"]["stop_reason"] == "context_pending"
    assert len(model.messages) == 1
