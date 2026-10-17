"""Opt-in real public-cache integration; fixed chat response, no human-quality claim.

Uses the two existing, known public cache files only. All accounts, reviewer
actions and candidate builds belong to the temporary pytest application.
The real pending-review protocol is never replaced and must prevent publishing.
"""
import hashlib
import json
import os
import shutil
from uuid import uuid4

import pytest

from config import ROOT
from model.embeddings import QwenEmbeddingProvider
from rag.hybrid import HybridKnowledgeRetriever
from rag.vector_store import VectorStore
from services import knowledge_quality
from test_foundation import application, client
from test_knowledge import library
from test_coach import conversation, send
from test_coach_knowledge import knowledge_chat, answer
from test_knowledge_workbench import proposal, WORK
from test_knowledge_review import grant, submit, check, approve
from test_knowledge_builds import body
from test_knowledge_releases import register_package, check_release
from test_knowledge_publications import accept, switch
from test_knowledge_recovery import Pages
from test_knowledge_loop import ScriptedModel, revise


@pytest.mark.skipif(os.environ.get("FITNESS_TEST_EXISTING_PUBLIC_CACHE") != "1",
                    reason="Explicit local opt-in; real public vectors are not distributed with Git")
def test_existing_real_vectors_build_duplicate_but_unreviewed_protocol_refuses_publication(knowledge_chat, tmp_path, monkeypatch):
    client, app, _ = knowledge_chat
    lexical = app.state.knowledge
    recovery = Pages(lexical, [[], []])
    judgment = ScriptedModel(revise("一周力量训练频率"))
    app.state.knowledge = recovery
    monkeypatch.setattr("api.coach.create_knowledge_model", lambda _: judgment)
    response = answer(client, send(client, conversation(client), "力量训练一般一周做几次").json())
    assert response.status_code == 200, response.text
    data = response.json()
    target = data["turns"][-1]
    assert target["response"]["knowledge"]["status"] == "insufficient"
    assert len(recovery.queries) == 2 and len(judgment.messages) == 1
    app.state.knowledge = lexical
    feedback = client.post(WORK + "/feedback", json={
        "client_id": str(uuid4()), "conversation_id": data["id"], "turn_version": target["version"],
        "answer_hash": target["feedback_target"]["answer_hash"], "kind": "knowledge_issue",
        "comment": "PRIVATE-REVIEW-NOTE: 核对现有公共来源的训练频率。",
    })
    assert feedback.status_code == 201, feedback.text
    source = next(item for item in client.get(WORK + "/sources").json()["items"]
                  if item["source"]["chunk_id"] == "cdc-adults:strength")
    candidate = client.post(WORK + "/candidates", json=proposal(feedback.json(), source))
    assert candidate.status_code == 201, candidate.text
    candidate = candidate.json()
    # Exact filenames are public artifacts, never the private fitness.sqlite3.
    vectors = ROOT.parent / "knowledge-vectors.sqlite3"
    queries = ROOT.parent / "release-evaluation/public-queries-f8fbf4871eb243ae13d16e31.json"
    assert vectors.is_file() and queries.is_file(), "Required existing public caches are absent"
    protocol_bytes = knowledge_quality.PROTOCOL_PATH.read_bytes()
    assert json.loads(protocol_bytes)["review_status"] == "pending_review"
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (vectors, queries)}

    class CacheIdentityOnly(QwenEmbeddingProvider):
        def embed(self, *args, **kwargs):
            pytest.fail("This cache-only integration must never encode a query or document")

    provider = CacheIdentityOnly(app.state.settings, max_calls=0, max_input_chars=0)
    copy = tmp_path / "existing-public-vectors.sqlite3"
    shutil.copyfile(vectors, copy)
    store = VectorStore(copy)
    documents = lexical.export_documents()
    assert len(documents) == 133 and len(store.load(documents, provider)) == 133
    metadata = json.loads(queries.read_text(encoding="utf-8"))
    assert metadata["manifest"]["identity"] == provider.identity
    assert metadata["manifest"]["dimensions"] == provider.dimensions == 1024
    assert len(metadata["vectors"]) == 87
    query_dir = app.state.database.path.parent / "release-evaluation"
    query_dir.mkdir(exist_ok=True)
    shutil.copyfile(queries, query_dir / queries.name)
    app.state.knowledge = HybridKnowledgeRetriever(lexical, provider, store)

    # Synthetic pre-review in a disposable account tests mechanics only.
    # It does not attest an actual human review of the frozen labels.
    grant(app)
    accepted = approve(client, check(client, submit(client, candidate).json()))
    assert accepted.status_code == 200, accepted.text
    assert "PRIVATE-REVIEW-NOTE" not in client.get(WORK + "/review-queue").text
    response = client.post(WORK + "/builds", json=body(accepted.json()))
    assert response.status_code == 201, response.text
    package = response.json()
    assert package["chunk_count"] == 133 and package["new_chunks"] == 0 and package["deduplicated"] == 1
    assert package["embedding"]["identity"] == provider.identity and package["real_provider_calls"] == 0
    assert candidate["content"] == source["source"]["excerpt"]
    item = register_package(client, package)

    def no_rankings(*args, **kwargs):
        pytest.fail("Pending human review must stop before frozen test ranking or query encoding")

    monkeypatch.setattr(knowledge_quality, "encode_queries", no_rankings)
    checked = check_release(client, item)
    assert checked.status_code == 200, checked.text
    checked = checked.json()
    assert not checked["can_approve"] and not checked["report"]["passed"]
    assert checked["report"]["results"] == []
    assert checked["report"]["hybrid_ranking_quality"] == "not_evaluated"
    assert accept(client, checked).status_code == 409
    assert switch(client, {**checked, "approval": {"version": 1}}).status_code == 409
    assert client.get(WORK + "/live").json()["base_active"]
    assert provider.calls == 0
    assert knowledge_quality.PROTOCOL_PATH.read_bytes() == protocol_bytes
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in before.items())
