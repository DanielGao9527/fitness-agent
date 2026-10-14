"""Versions and provenance for confirmed profile facts; values stay in profiles.

Only equipment is managed in this increment. Audit events contain no old/new
private text. Callers own the transaction and the authenticated user identity.
"""
import json

from fastapi import HTTPException


def equipment_revision(connection, user_id):
    row = connection.execute(
        "SELECT revision FROM profile_fact_events WHERE user_id=? AND field='equipment' "
        "ORDER BY revision DESC LIMIT 1", (user_id,)).fetchone()
    return row[0] if row else 0


def fact_revisions(connection, user_id):
    return {"equipment": equipment_revision(connection, user_id)}


def write_profile(connection, user_id, payload, *, equipment_explicit=False,
                  source="profile_edit", conversation_id=None, turn_version=None,
                  expected_equipment_revision=None):
    if not connection.in_transaction:
        raise RuntimeError("Profile writes require a caller-owned transaction")
    if source not in ("profile_edit", "conversation_review"):
        raise ValueError("Unsupported personal fact source")
    revision = equipment_revision(connection, user_id)
    if (equipment_explicit and expected_equipment_revision is not None
            and revision != expected_equipment_revision):
        raise HTTPException(409, "器械档案已在其他页面或对话中更正，请重新载入档案后核对；本次未保存。")
    if source == "conversation_review":
        owned = connection.execute(
            "SELECT 1 FROM coach_turns t JOIN coach_conversations c ON c.id=t.conversation_id "
            "WHERE c.id=? AND c.user_id=? AND t.version=?",
            (conversation_id, user_id, turn_version)).fetchone()
        if not owned:
            raise HTTPException(404, "个人事实的来源对话不存在。")
    elif conversation_id is not None or turn_version is not None:
        raise ValueError("Profile edits cannot claim a conversation source")
    previous = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    previous = json.loads(previous[0]) if previous else {}
    if equipment_explicit and (revision == 0 or previous.get("equipment", "") != payload["equipment"]):
        revision += 1
        connection.execute(
            "INSERT INTO profile_fact_events(user_id,field,revision,action,source,conversation_id,turn_version) "
            "VALUES (?,'equipment',?,?,?,?,?)",
            (user_id, revision, "set" if payload["equipment"] else "clear", source, conversation_id, turn_version))
    connection.execute(
        "INSERT INTO profiles(user_id,payload) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
        (user_id, json.dumps(payload, ensure_ascii=False)))
    if equipment_explicit and not payload["equipment"]:
        from services.equipment_memory import revoke_day
        revoke_day(connection, user_id)
    return revision


def current_fact(connection, user_id):
    row = connection.execute(
        "SELECT field,revision,action,source,conversation_id,turn_version,created_at "
        "FROM profile_fact_events WHERE user_id=? AND field='equipment' ORDER BY revision DESC LIMIT 1",
        (user_id,)).fetchone()
    if row is None:
        return {"field": "equipment", "revision": 0, "status": "unset", "source": None}
    return {**dict(row), "status": "active" if row["action"] == "set" else "cleared",
            "scope": "long_term", "source_available": row["conversation_id"] is not None
            if row["source"] == "conversation_review" else True}
