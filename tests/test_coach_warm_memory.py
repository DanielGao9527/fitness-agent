"""Explicitly saved warm notes stay private, bounded and subordinate to current facts."""
import sqlite3
import pytest

from fastapi.testclient import TestClient

from app import create_app
from database import Database
from deploy.check_release import check_schema
from scripts.migrate_memory import migrate
from services.coach_memory_digests import recall_digests
from services.backups import create_backup, fingerprints, inspect_connection, restore_backup, verify_backup
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand
from test_coach_memory import _confirmed_training
from test_foundation import application, client, register
from test_meal_plans import plans
from test_knowledge import library


URL = f"{BASE}/memory/entries"


def add(client, kind, content):
    return client.post(URL, json={"kind": kind, "content": content})


def test_warm_note_is_explicit_and_reloaded_for_next_request(reviews):
    client, app, model, _ = reviews
    saved = add(client, "assistant_note", "用户把夹胸称为飞鸟机动作")
    assert saved.status_code == 201, saved.text
    note = saved.json()
    assert add(client, "assistant_note", note["content"]).status_code == 409
    assert client.get(URL).json()["items"] == [note]

    model.edit = lambda _, out: out.update(scope="strength", command="")
    request = send(client, conversation(client), "今天用飞鸟机练胸").json()
    result = understand(client, request)
    assert result.status_code == 200, result.text
    assert model.messages[-1]["warm_memory"]["assistant_notes"][0]["text"] == note["content"]
    assert model.messages[-1]["warm_memory"]["status"] == "user_saved_untrusted_context"
    assert model.messages[-1]["messages"][0]["message"] == "今天用飞鸟机练胸"

    changed = client.put(f"{URL}/{note['id']}", json={"version": 1, "content": "用户把夹胸称为蝴蝶机动作"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    assert client.put(f"{URL}/{note['id']}", json={"version": 1, "content": "旧内容"}).status_code == 409
    later = send(client, conversation(client), "请按蝴蝶机安排胸部力量训练").json()
    later_review = understand(client, later)
    assert later_review.status_code == 200, later_review.text
    assert model.messages[-1]["warm_memory"]["assistant_notes"][0]["text"] == "用户把夹胸称为蝴蝶机动作"

    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(URL).json()["items"][0]["content"] == "用户把夹胸称为蝴蝶机动作"


def test_memory_change_during_model_call_invalidates_old_result(reviews):
    client, _, model, _ = reviews
    note = add(client, "user_preference", "喜欢简短解释").json()
    request = send(client, conversation(client), "请按哑铃安排胸部力量训练").json()
    model.edit = lambda _, out: out.update(scope="strength", command="")
    model.on_call = lambda: client.put(f"{URL}/{note['id']}", json={"version": 1, "content": "喜欢详细解释"})
    result = understand(client, request)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "COACH_CONTEXT_CHANGED"
    assert client.get(URL).json()["items"][0]["content"] == "喜欢详细解释"


def test_memory_owner_delete_and_input_bounds(client, application):
    assert client.get(URL).status_code == 401
    assert client.post(URL, json={"kind": "user_preference", "content": "private"}).status_code == 401
    assert client.get(f"{BASE}/memory/digests").status_code == 401
    register(client)
    assert client.post(URL, json={"kind": "assistant_note", "content": ""}).status_code == 422
    assert client.post(URL, json={"kind": "user_preference", "content": "x", "user_id": 999}).status_code == 422
    note = add(client, "user_preference", "请用中文解释").json()
    with TestClient(application) as other:
        register(other, "bob")
        assert other.get(URL).json()["items"] == []
        assert other.put(f"{URL}/{note['id']}", json={"version": 1, "content": "篡改"}).status_code == 404
        assert other.request("DELETE", f"{URL}/{note['id']}", json={"version": 1}).status_code == 404
    assert client.request("DELETE", f"{URL}/{note['id']}", json={"version": 1}).status_code == 204
    assert client.get(URL).json()["items"] == []
    assert client.request("DELETE", f"{URL}/{note['id']}", json={"version": 1}).status_code == 404


def test_memory_capacity_and_private_backup(client, application, tmp_path):
    register(client)
    for index in range(20):
        result = add(client, "user_preference", f"偏好{index:02d}" + "简" * 54)
        assert result.status_code == 201, result.text
    assert add(client, "user_preference", "超出条数").status_code == 409
    original = client.get(URL).json()["items"]
    snapshot, recovered = tmp_path / "private.sqlite3", tmp_path / "recovered.sqlite3"
    assert create_backup(application.state.database.path, snapshot)["schema"] == 18
    assert restore_backup(snapshot, recovered)["sessions_revoked"]
    with sqlite3.connect(recovered) as connection:
        assert connection.execute("SELECT COUNT(*) FROM coach_memory_entries").fetchone()[0] == len(original)
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_reviewed_digest_is_cold_evidence_not_current_constraint(reviews):
    client, app, model, _ = reviews
    old = _confirmed_training(client, model)
    digest_url = f"{BASE}/memory/digests"
    proposed = client.post(f"{digest_url}/from-conversation/{old['id']}", json={})
    assert proposed.status_code == 201, proposed.text
    candidate = proposed.json()
    assert candidate["status"] == "pending" and not candidate["stale"]
    assert "哑铃" in candidate["summary"] and candidate["source_versions"]
    with app.state.database.connect() as connection:
        owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (old["id"],)).fetchone()[0]
        assert recall_digests(connection, owner, "上次说过哑铃") == []

    with TestClient(app) as other:
        register(other, "bob")
        assert other.get(digest_url).json()["items"] == []
        assert other.post(f"{digest_url}/from-conversation/{old['id']}", json={}).status_code == 404
        assert other.post(f"{digest_url}/{candidate['id']}/approve", json={"version": 1}).status_code == 404

    approved = client.post(f"{digest_url}/{candidate['id']}/approve", json={"version": 1})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert client.post(f"{digest_url}/{candidate['id']}/approve", json={"version": 1}).status_code == 409
    with app.state.database.connect() as connection:
        assert recall_digests(connection, owner, "上次说过哑铃")[0]["conversation_id"] == old["id"]
        source_rowid = connection.execute("SELECT rowid FROM coach_turns WHERE conversation_id=? AND version=1", (old["id"],)).fetchone()[0]
        assert recall_digests(connection, owner, "上次说过哑铃", before_turn_rowid=source_rowid) == []
    now = send(client, conversation(client), "上次说过的哑铃训练，我想继续").json()
    assert now["constraints"] == {}
    model.edit = lambda _, out: out.update(scope="strength", command="")
    assert understand(client, now).status_code == 200
    assert model.messages[-1]["historical_digests"][0]["status"] == "approved_historical_digest"

    assert client.put("/api/profile", json={"equipment": "在家，徒手"}).status_code == 200
    with app.state.database.connect() as connection:
        assert recall_digests(connection, owner, "上次说过哑铃") == []

    assert client.request("DELETE", f"{BASE}/{old['id']}", json={"version": old["version"]}).status_code == 204
    assert client.get(digest_url).json()["items"] == []
    with app.state.database.connect() as connection:
        assert recall_digests(connection, owner, "上次说过哑铃") == []


def test_digest_source_change_requires_fresh_approval(reviews):
    client, app, model, _ = reviews
    old = _confirmed_training(client, model)
    url = f"{BASE}/memory/digests"
    first = client.post(f"{url}/from-conversation/{old['id']}", json={}).json()
    approved = client.post(f"{url}/{first['id']}/approve", json={"version": first["version"]}).json()
    with app.state.database.connect() as connection:
        owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (old["id"],)).fetchone()[0]
        # Simulate a corrected source. A prior approval must never bless new text.
        connection.execute("UPDATE coach_turns SET message='现在改为弹力带' WHERE conversation_id=? AND version=1", (old["id"],))
        assert recall_digests(connection, owner, "哑铃") == []
    assert client.get(url).json()["items"][0]["stale"]
    assert client.post(f"{url}/{approved['id']}/approve", json={"version": approved["version"]}).status_code == 409
    refreshed = client.post(f"{url}/from-conversation/{old['id']}", json={}).json()
    assert refreshed["status"] == "pending" and refreshed["version"] > approved["version"]
    assert "弹力带" in refreshed["summary"]


def test_v14_memory_migration_preserves_rows_and_backup(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    db = Database(source)
    db.initialize()
    with db.connect() as connection:
        connection.execute("DROP TABLE coach_day_equipment")
        connection.execute("DROP TABLE profile_fact_events")
        connection.execute("DROP TABLE coach_memory_digests")
        connection.execute("DROP TRIGGER IF EXISTS coach_memory_source_change")
        connection.execute("DROP TRIGGER IF EXISTS coach_memory_source_delete")
        connection.execute("DROP TABLE coach_memory_revisions")
        connection.execute("DROP TABLE coach_memory_entries")
        connection.execute("PRAGMA user_version=14")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'fixture','synthetic')")
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES (1,'{}')")
    before = fingerprints(source)
    with pytest.raises(ValueError, match="migration"):
        check_schema(Database, inspect_connection, source)
    backup = tmp_path / "approved.sqlite3"
    result = migrate(source, backup)
    assert result["schema"] == 18 and result["tables"] == 25
    assert result["old_tables_preserved"]
    assert verify_backup(backup)["schema"] == 14
    assert all(fingerprints(source)[name] == value for name, value in before.items())
    assert source.with_name(source.name + ".pre-v15.bak").exists()
    check_schema(Database, inspect_connection, source)

    restored = tmp_path / "restored.sqlite3"
    fresh_backup = tmp_path / "new.sqlite3"
    assert create_backup(source, fresh_backup)["schema"] == 18
    assert restore_backup(fresh_backup, restored)["schema"] == 18
    with sqlite3.connect(restored) as connection:
        assert inspect_connection(connection)["tables"] == 25
    assert fingerprints(restored)["coach_memory_entries"] == fingerprints(source)["coach_memory_entries"]
