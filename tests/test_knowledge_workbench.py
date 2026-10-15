import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from scripts.migrate_memory import migrate
from services.backups import fingerprints, table_columns, create_backup, restore_backup
from test_foundation import application, client, register, PASSWORD
from test_knowledge import library, mutate
from test_coach import BASE, conversation, send
from test_coach_knowledge import knowledge_chat, answer
from test_knowledge_answers import calls

WORK = "/api/knowledge-workbench"


@pytest.fixture
def workbench(knowledge_chat):
    client, app, model = knowledge_chat
    data = answer(client, send(client, conversation(client), "力量训练一般一周做几次").json()).json()
    target = data["turns"][-1]
    feedback = {"client_id": str(uuid4()), "conversation_id": data["id"], "turn_version": target["version"],
                "answer_hash": target["feedback_target"]["answer_hash"], "kind": "knowledge_issue", "comment": "请核对证据是否直接回答频率。"}
    sources = client.get(WORK + "/sources").json()["items"]
    source = next(item for item in sources if item["source"]["chunk_id"] == "cdc-adults:strength")
    return client, app, model, data, feedback, source


def proposal(feedback, source, **changes):
    return {"client_id": str(uuid4()), "feedback_id": feedback["id"], "content": source["source"]["excerpt"],
            "source_chunk_id": source["source"]["chunk_id"], "source_hash": source["source_hash"], "reviewed": True, **changes}


def prepared(workbench):
    client, app, model, data, body, source = workbench
    result = client.post(WORK + "/feedback", json=body)
    assert result.status_code == 201, result.text
    return client, app, model, data, result.json(), source


def test_feedback_binding_idempotence_and_private_correction_does_not_write_profile(workbench):
    client, app, model, data, body, source = workbench
    before = client.get("/api/profile").json()
    body.update(kind="personal_correction", comment="我现在的器械描述需要更正 <script>alert(1)</script>")
    first = client.post(WORK + "/feedback", json=body)
    assert first.status_code == 201, first.text
    assert client.post(WORK + "/feedback", json=body).json() == first.json()
    assert client.post(WORK + "/feedback", json={**body, "comment": "不同请求"}).status_code == 409
    assert client.post(WORK + "/feedback", json={**body, "client_id": str(uuid4())}).status_code == 409
    item = first.json()
    assert not item["origin_stale"] and not item["used_as_knowledge"]
    assert item["sources"][0]["source_hash"] and item["sources"][0]["source_version"]
    assert client.post(WORK + "/candidates", json=proposal(item, source)).status_code == 409
    assert client.get("/api/profile").json() == before and len(model.messages) == 1
    assert len(client.get(WORK + "/feedback").json()["items"]) == 1


def test_source_quote_candidate_dedup_revision_rejection_and_retrieval_isolation(workbench):
    client, app, model, data, feedback, source = prepared(workbench)
    public_before = app.state.knowledge.export_documents()
    body = proposal(feedback, source)
    created = client.post(WORK + "/candidates", json=body)
    assert created.status_code == 201, created.text
    item = created.json()
    assert not item["published"] and not item["used_as_knowledge"] and item["version"] == 1
    assert client.post(WORK + "/candidates", json=body).json() == item
    assert client.post(WORK + "/candidates", json={**body, "client_id": str(uuid4())}).status_code == 409
    assert client.post(WORK + "/candidates", json={**body, "content": "changed"}).status_code == 409
    endpoint = WORK + "/candidates/" + item["id"]
    rejected = client.post(endpoint + "/reject", json={"version": 1, "note": "先补充分歧说明"}).json()
    assert rejected["status"] == "rejected" and rejected["version"] == 2
    update = {key: body[key] for key in ("content", "source_chunk_id", "source_hash", "reviewed")}
    assert client.put(endpoint, json={**update, "version": 1}).status_code == 409
    revised = client.put(endpoint, json={**update, "version": 2}).json()
    assert revised["status"] == "draft" and revised["version"] == 3 and not revised["review_note"]
    assert [event["action"] for event in revised["events"]] == ["created", "rejected", "revised"]
    assert client.post(WORK + "/candidates", json=body).json()["version"] == 3
    assert app.state.knowledge.export_documents() == public_before and len(model.messages) == calls(app) == 1
    assert client.post(endpoint + "/publish", json={"version": 3}).status_code == 404


@pytest.mark.parametrize("change,code", [
    ({"reviewed": False}, 422), ({"reviewed": 1}, 422), ({"user_id": 2}, 422),
    ({"scope": "个人处方"}, 422), ({"status": "approved"}, 422), ({"content": "私人原话并不是公共出处"}, 422),
    ({"source_hash": "0" * 64}, 409), ({"source_chunk_id": "unknown:source"}, 409),
])
def test_candidate_rejects_unreviewed_private_unbound_and_untrusted_fields(workbench, change, code):
    client, _, _, _, feedback, source = prepared(workbench)
    assert client.post(WORK + "/candidates", json=proposal(feedback, source, **change)).status_code == code
    assert client.get(WORK + "/candidates").json()["items"] == []


def test_auth_ownership_origin_hash_and_cross_user_isolation(workbench):
    client, app, _, data, body, source = workbench
    assert client.post(WORK + "/feedback", json={**body, "answer_hash": "0" * 64}).status_code == 409
    assert client.post(WORK + "/feedback", json={**body, "user_id": 99}).status_code == 422
    feedback = client.post(WORK + "/feedback", json=body).json()
    candidate = client.post(WORK + "/candidates", json=proposal(feedback, source)).json()
    with TestClient(app) as other:
        assert other.get(WORK + "/feedback").status_code == 401
        register(other, "bob")
        assert other.get(WORK + "/feedback").json()["items"] == []
        assert other.get(WORK + "/candidates").json()["items"] == []
        assert other.post(WORK + "/feedback", json={**body, "client_id": str(uuid4())}).status_code == 404
        assert other.post(WORK + "/candidates", json=proposal(feedback, source)).status_code == 404
        assert other.request("DELETE", WORK + "/feedback/" + feedback["id"], json={"version": 1}).status_code == 404
        assert other.post(WORK + "/candidates/" + candidate["id"] + "/reject", json={"version": 1, "note": "伪造审阅"}).status_code == 404


def test_answer_change_and_public_source_withdrawal_invalidate_drafts(workbench):
    client, app, _, data, feedback, source = prepared(workbench)
    candidate = client.post(WORK + "/candidates", json=proposal(feedback, source)).json()
    mutate(app.state.knowledge, lambda corpus: next(s for s in corpus["sources"] if s["id"] == "cdc-adults").update(status="withdrawn"))
    item = client.get(WORK + "/candidates").json()["items"][0]
    assert item["source_stale"] and not item["origin_stale"]
    assert client.post(WORK + "/candidates", json=proposal(feedback, source)).status_code == 409
    with app.state.database.connect() as connection:
        connection.execute("UPDATE coach_turns SET message='已更正的问题' WHERE conversation_id=? AND version=?", (data["id"], 1))
    assert client.get(WORK + "/feedback").json()["items"][0]["origin_stale"]
    assert client.get(WORK + "/candidates").json()["items"][0]["origin_stale"]
    assert client.request("DELETE", WORK + "/candidates/" + candidate["id"], json={"version": 1}).status_code == 204


def test_restart_concurrency_and_delete_cascades_without_business_writes(workbench):
    client, app, _, data, body, source = workbench
    def submit(_):
        return client.post(WORK + "/feedback", json=body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(submit, range(2)))
    assert all(v.status_code == 201 for v in values) and values[0].json()["id"] == values[1].json()["id"]
    feedback = values[0].json()
    candidate = client.post(WORK + "/candidates", json=proposal(feedback, source)).json()
    restarted = create_app(app.state.settings)
    restarted.state.knowledge = app.state.knowledge
    with TestClient(restarted) as fresh:
        login = fresh.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert fresh.get(WORK + "/candidates").json()["items"][0]["id"] == candidate["id"]
    assert client.request("DELETE", BASE + "/" + data["id"], json={"version": data["version"]}).status_code == 204
    for path in ("/feedback", "/candidates"):
        assert client.get(WORK + path).json()["items"] == []
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_candidate_events").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_account_erasure_removes_feedback_candidates_and_events(workbench):
    client, app, _, _, feedback, source = prepared(workbench)
    client.post(WORK + "/candidates", json=proposal(feedback, source))
    with app.state.database.connect() as connection:
        # Match existing cleanup: remove per-user usage before deleting its owner.
        connection.execute("DELETE FROM ai_usage WHERE user_id=(SELECT id FROM users WHERE username='alice')")
        connection.execute("DELETE FROM users WHERE username='alice'")
        for name in ("knowledge_feedback", "knowledge_candidates", "knowledge_candidate_events"):
            assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert client.get(WORK + "/feedback").status_code == 401


def test_v18_upgrade_preserves_every_original_field_and_verified_backup(tmp_path):
    from schema_fixtures import strip_governance_v19
    path = tmp_path / "before.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        strip_governance_v19(connection)
        connection.execute("PRAGMA user_version=18")
    columns, before = table_columns(path), fingerprints(path)
    backup = tmp_path / "before-v19.sqlite3"
    result = migrate(path, backup)
    assert result["schema"] == 22 and result["tables"] == 38 and result["old_tables_preserved"]
    assert fingerprints(path, columns=columns) == before and fingerprints(backup) == before
    with sqlite3.connect(str(path) + ".pre-v19.bak") as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 18
    fresh, restored = tmp_path / "after.sqlite3", tmp_path / "restored.sqlite3"
    create_backup(path, fresh)
    assert restore_backup(fresh, restored)["schema"] == 22


def test_unavailable_sources_preserve_private_management_and_block_new_content(workbench):
    from rag.rag_service import KnowledgeUnavailable
    client, app, _, _, feedback, source = prepared(workbench)
    candidate = client.post(WORK + "/candidates", json=proposal(feedback, source)).json()
    class Broken:
        def all_chunks(self):
            raise KnowledgeUnavailable("synthetic unavailable")
        def get_chunks(self, ids):
            raise KnowledgeUnavailable("synthetic unavailable")
    app.state.knowledge = Broken()
    assert client.get(WORK + "/sources").status_code == 503
    assert client.get(WORK + "/feedback").status_code == 200
    item = client.get(WORK + "/candidates").json()["items"][0]
    assert item["source_unavailable"] and item["source_stale"]
    assert client.post(WORK + "/candidates", json=proposal(feedback, source)).status_code == 503
    endpoint = WORK + "/candidates/" + candidate["id"]
    assert client.post(endpoint + "/reject", json={"version": 1, "note": "等待来源恢复"}).status_code == 200
    assert client.request("DELETE", WORK + "/feedback/" + feedback["id"], json={"version": 1}).status_code == 204
    assert client.get(WORK + "/candidates").json()["items"] == []


def test_unverified_training_notes_are_not_public_candidate_sources(workbench):
    client, app, _, _, feedback, source = prepared(workbench)
    mutate(app.state.knowledge, lambda corpus: next(s for s in corpus["sources"] if s["id"] == "cdc-adults").update(
        origin="curated_text", url="", license_url=""))
    assert all(item["source"]["origin"] == "public_web" for item in client.get(WORK + "/sources").json()["items"])
    assert client.post(WORK + "/candidates", json=proposal(feedback, source)).status_code == 409


def test_feedback_capacity_is_explicit_and_does_not_partially_insert(workbench):
    from services.knowledge_feedback import signature
    client, app, _, data, body, _ = workbench
    with app.state.database.connect() as connection:
        owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (data["id"],)).fetchone()[0]
        for number in range(100):
            connection.execute("INSERT INTO knowledge_feedback(id,user_id,client_id,request_hash,conversation_id,turn_version,"
                "answer_hash,source_snapshot,kind,comment) VALUES (?,?,?,?,?,1,?,'[]','knowledge_issue','capacity fixture')",
                (str(uuid4()), owner, str(uuid4()), signature(number), data["id"], signature(number)))
    result = client.post(WORK + "/feedback", json=body)
    assert result.status_code == 409
    assert len(client.get(WORK + "/feedback").json()["items"]) == 100


def test_concurrent_candidate_retry_creates_one_entry_and_one_event(workbench):
    client, app, _, _, feedback, source = prepared(workbench)
    body = proposal(feedback, source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(WORK + "/candidates", json=body), range(2)))
    assert all(result.status_code == 201 for result in results)
    assert results[0].json()["id"] == results[1].json()["id"]
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_candidates").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_candidate_events").fetchone()[0] == 1


def test_cross_site_write_cannot_create_feedback(workbench):
    client, _, _, _, body, _ = workbench
    assert client.post(WORK + "/feedback", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get(WORK + "/feedback").json()["items"] == []
