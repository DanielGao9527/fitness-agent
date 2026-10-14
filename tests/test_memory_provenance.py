"""Warm context provenance, confirmation, revocation and private schema upgrade."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from deploy.check_release import check_schema
from schema_fixtures import strip_memory_v18
from scripts.migrate_memory import migrate
from services.backups import fingerprints, inspect_connection, table_columns, verify_backup
from services.coach_memory_entries import warm_memory
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand
from test_foundation import application, client, register
from test_knowledge import library
from test_meal_plans import plans

URL = f"{BASE}/memory/entries"
QUOTE = "以后请用简明中文说明，遇到陌生词先解释。"


def source(client, text=QUOTE):
    return send(client, conversation(client), text).json()


def body(client, data, **changes):
    return {"category": "communication_preference", "content": data["turns"][-1]["message"],
            "conversation_id": data["id"], "conversation_version": data["version"],
            "source_version": data["turns"][-1]["version"], "reviewed": True,
            "memory_revision": client.get(URL).json()["revision"], **changes}


def add_source(client, data, **changes):
    return client.post(URL + "/from-turn", json=body(client, data, **changes))


def test_source_confirmation_provenance_and_restarted_context(reviews):
    client, app, model, meal_model = reviews
    profile = client.get("/api/profile").json()
    data = source(client)
    assert client.get(URL).json()["items"] == []
    note = add_source(client, data)
    assert note.status_code == 201, note.text
    note = note.json()
    assert note["source"] == {"conversation_id": data["id"], "version": 1, "day": data["day"]}
    assert note["category"] == "communication_preference" and not note["stale"]
    assert client.get(URL).json()["revision"] == 1
    assert model.messages == [] and meal_model.messages == []
    assert client.get("/api/profile").json() == profile
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(URL).json()["items"] == [note]
        model.edit = lambda _, result: result.update(scope="strength", command="")
        request = send(restarted, conversation(restarted), "请按哑铃安排胸部力量训练").json()
        assert understand(restarted, request).status_code == 200
        saved = model.messages[-1]["warm_memory"]["user_preferences"][0]
        assert saved["text"] == QUOTE and saved["source"] == note["source"]
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"reviewed": False}, {"reviewed": "true"}, {"reviewed": 1}, {"memory_revision": True},
    {"source_version": True}, {"category": "equipment"}, {"content": ""},
    {"content": "x" * 201}, {"user_id": 1}, {"source_version": 99},
    {"content": "请用中文说明遇到陌生词先解释"}, {"content": "助手给出的建议"},
])
def test_invalid_or_unreviewed_source_never_writes(client, changes):
    register(client)
    data = source(client)
    response = add_source(client, data, **changes)
    assert response.status_code == 422, response.text
    assert client.get(URL).json() == {"items": [], "revision": 0, "used_as_current_constraint": False}


def test_source_and_memory_revision_reject_stale_or_repeated_confirmation(client):
    register(client)
    data = source(client)
    original = body(client, data)
    latest = send(client, data, "补充：解释时先给结论。").json()
    assert client.post(URL + "/from-turn", json=original).status_code == 409
    current = body(client, latest, content=QUOTE, source_version=1)
    first = client.post(URL + "/from-turn", json=current)
    assert first.status_code == 201, first.text
    assert client.post(URL + "/from-turn", json=current).status_code == 409
    note = first.json()
    assert client.request("DELETE", f"{URL}/{note['id']}", json={"version": note["version"]}).status_code == 204
    assert client.get(URL).json()["items"] == []
    assert client.get(URL).json()["revision"] == 2
    assert client.post(URL + "/from-turn", json=original).status_code == 409
    assert client.post(URL + "/from-turn", json=current).status_code == 409


def test_empty_list_aba_invalidates_inflight_understanding(reviews):
    client, _, model, _ = reviews
    request = send(client, conversation(client), "请按哑铃安排胸部力量训练").json()
    model.edit = lambda _, result: result.update(scope="strength", command="")
    def change_and_clear():
        saved = client.post(URL, json={"kind": "user_preference", "content": "简明解释"}).json()
        assert client.request("DELETE", f"{URL}/{saved['id']}", json={"version": 1}).status_code == 204
    model.on_call = change_and_clear
    response = understand(client, request)
    assert response.status_code == 409, response.text
    assert client.get(URL).json()["revision"] == 2
    assert client.get(URL).json()["items"] == []


def test_source_correction_aba_does_not_revive_an_old_note(client, application):
    register(client)
    data = source(client)
    note = add_source(client, data).json()
    with application.state.database.connect() as connection:
        owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (data["id"],)).fetchone()[0]
        connection.execute("UPDATE coach_turns SET message='原话已更正' WHERE conversation_id=? AND version=1", (data["id"],))
        assert warm_memory(connection, owner)["user_preferences"] == []
        connection.execute("UPDATE coach_turns SET message=? WHERE conversation_id=? AND version=1", (QUOTE, data["id"]))
        assert warm_memory(connection, owner)["user_preferences"] == []
    stale = client.get(URL).json()["items"][0]
    assert stale["stale_reason"] == "source_changed" and stale["version"] == note["version"] + 2
    assert client.get(URL).json()["revision"] == 3
    assert client.put(f"{URL}/{note['id']}", json={"version": 1, "content": "新偏好"}).status_code == 409
    corrected = client.put(f"{URL}/{note['id']}", json={"version": stale["version"], "content": "请按步骤解释"})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["source_mode"] == "manual" and corrected.json()["source"] is None
    assert not corrected.json()["stale"]


def test_source_delete_stays_revoked_after_restart_and_explicit_edit(client, application):
    register(client)
    data = source(client)
    note = add_source(client, data).json()
    assert client.request("DELETE", f"{BASE}/{data['id']}", json={"version": data["version"]}).status_code == 204
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        stale = restarted.get(URL).json()["items"][0]
        assert stale["stale_reason"] == "source_deleted" and stale["source"]["conversation_id"] is None
        assert restarted.get(URL).json()["revision"] == 2
        response = restarted.put(f"{URL}/{note['id']}", json={"version": stale["version"], "content": "请按步骤解释"})
        assert response.status_code == 200
        assert response.json()["source_mode"] == "manual" and not response.json()["stale"]


def test_foreign_sources_and_entry_operations_remain_isolated(client, application):
    register(client)
    data = source(client)
    original = body(client, data)
    note = client.post(URL + "/from-turn", json=original).json()
    with TestClient(application) as other:
        register(other, "bob")
        assert other.post(URL + "/from-turn", json=original).status_code == 404
        assert other.put(f"{URL}/{note['id']}", json={"version": 1, "content": "篡改"}).status_code == 404
        assert other.request("DELETE", f"{URL}/{note['id']}", json={"version": 1}).status_code == 404
        assert other.get(URL).json()["revision"] == 0


def test_one_communication_slot_and_concurrent_confirmation(client):
    register(client)
    data = source(client)
    candidate = body(client, data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(URL + "/from-turn", json=candidate), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(client.get(URL).json()["items"]) == 1
    assert client.post(URL, json={"kind": "user_preference", "category": "communication_preference", "content": "详细解释"}).status_code == 409
    assert client.post(URL, json={"kind": "assistant_note", "category": "communication_preference", "content": "类型错误"}).status_code == 422


def test_source_memory_capacity_failure_rolls_back_entry_and_revision(client):
    register(client)
    for index in range(20):
        assert client.post(URL, json={"kind": "assistant_note", "content": f"术语{index}"}).status_code == 201
    data = source(client, "上肢A表示胸部和三头训练。")
    response = add_source(client, data, category="personal_term")
    assert response.status_code == 409
    assert len(client.get(URL).json()["items"]) == 20 and client.get(URL).json()["revision"] == 20


def test_account_erasure_cascades_source_notes_without_touching_another_owner(client, application):
    register(client)
    data = source(client)
    assert add_source(client, data).status_code == 201
    with TestClient(application) as other:
        register(other, "bob")
        assert other.post(URL, json={"kind": "assistant_note", "content": "Bob 的测试术语"}).status_code == 201
        with application.state.database.connect() as connection:
            owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (data["id"],)).fetchone()[0]
            connection.execute("DELETE FROM users WHERE id=?", (owner,))
            assert connection.execute("SELECT COUNT(*) FROM coach_memory_entries WHERE user_id=?", (owner,)).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM coach_memory_revisions WHERE user_id=?", (owner,)).fetchone()[0] == 0
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert client.get(URL).status_code == 401
        assert other.get(URL).json()["items"][0]["content"] == "Bob 的测试术语"
        assert other.get(URL).json()["revision"] == 1


def test_v17_nonempty_memory_upgrade_preserves_all_original_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        strip_memory_v18(connection)
        connection.execute("PRAGMA user_version=17")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'fixture','synthetic')")
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES(1,'{}')")
        connection.execute("INSERT INTO coach_memory_entries(id,user_id,kind,content) VALUES(?,1,'user_preference',?)",
                           (str(uuid4()), "保留这条旧笔记"))
    before = fingerprints(path)
    columns = table_columns(path)
    with pytest.raises(ValueError, match="migration"):
        check_schema(Database, inspect_connection, path)
    backup = tmp_path / "verified.sqlite3"
    result = migrate(path, backup)
    assert result["schema"] == 18 and result["tables"] == 25 and result["old_tables_preserved"]
    assert fingerprints(path, columns=columns) == before
    assert fingerprints(backup) == before and verify_backup(backup)["schema"] == 17
    with database.connect() as connection:
        note = dict(connection.execute("SELECT * FROM coach_memory_entries").fetchone())
        assert note["content"] == "保留这条旧笔记" and note["category"] == "general_note"
        assert warm_memory(connection, 1)["revision"] == 1
    database.initialize()
    check_schema(Database, inspect_connection, path)
    with sqlite3.connect(str(path) + ".pre-v18.bak") as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 17
