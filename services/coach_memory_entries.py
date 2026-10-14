"""User-confirmed warm context with exact provenance and versioned writes."""
import hashlib
from uuid import uuid4

from fastapi import HTTPException

MAX_ENTRIES = 20
MAX_TOTAL_CHARS = 1200
CATEGORY_KINDS = {"communication_preference": "user_preference", "personal_term": "assistant_note"}


def memory_revision(connection, user_id):
    row = connection.execute("SELECT revision FROM coach_memory_revisions WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else 0


def _bump(connection, user_id):
    connection.execute("INSERT INTO coach_memory_revisions(user_id,revision) VALUES (?,1) "
                       "ON CONFLICT(user_id) DO UPDATE SET revision=coach_memory_revisions.revision+1", (user_id,))


def entries(connection, user_id):
    rows = connection.execute(
        "SELECT e.*,c.day AS source_day,c.user_id AS source_owner,t.message AS source_message "
        "FROM coach_memory_entries e "
        "LEFT JOIN coach_conversations c ON c.id=e.source_conversation_id AND c.user_id=e.user_id "
        "LEFT JOIN coach_turns t ON t.conversation_id=c.id AND t.version=e.source_turn_version "
        "WHERE e.user_id=? ORDER BY e.created_at,e.id", (user_id,)).fetchall()
    result = []
    for row in rows:
        item = {key: row[key] for key in ("id", "kind", "content", "category", "version", "created_at", "updated_at", "source_mode")}
        reason = None
        if row["source_mode"] == "conversation":
            if row["source_owner"] != user_id or row["source_message"] is None:
                reason = "source_deleted"
            elif row["source_revoked"] or hashlib.sha256(row["source_message"].encode()).hexdigest() != row["source_text_hash"]:
                reason = "source_changed"
            item["source"] = {"conversation_id": row["source_conversation_id"],
                              "version": row["source_turn_version"], "day": row["source_day"]}
        else:
            item["source"] = None
        item.update(stale=reason is not None, stale_reason=reason)
        result.append(item)
    return result


def _bounded(connection, user_id, replacement=None):
    current = entries(connection, user_id)
    if replacement:
        current = [{**item, "content": replacement[1]} if item["id"] == replacement[0] else item
                   for item in current]
    if len(current) > MAX_ENTRIES or sum(len(item["content"]) for item in current) > MAX_TOTAL_CHARS:
        raise HTTPException(409, "个人记忆已达容量上限，请先精简或删除旧条目。")


def _insert(connection, user_id, kind, content, category, *, source=None):
    if category != "general_note" and CATEGORY_KINDS.get(category) != kind:
        raise HTTPException(422, "记忆类型与用途不一致。")
    current = entries(connection, user_id)
    if any(item["kind"] == kind and item["content"] == content for item in current):
        raise HTTPException(409, "这条记忆已经保存。")
    if category == "communication_preference" and any(item["category"] == category for item in current):
        raise HTTPException(409, "已有明确的交流偏好，请修改或删除原条目后再保存，避免同时使用冲突的说明。")
    identifier = str(uuid4())
    origin = (str(source["conversation_id"]), source["version"], source["hash"]) if source else (None, None, None)
    connection.execute(
        "INSERT INTO coach_memory_entries(id,user_id,kind,content,category,source_mode,"
        "source_conversation_id,source_turn_version,source_text_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (identifier, user_id, kind, content, category, "conversation" if source else "manual", *origin))
    _bounded(connection, user_id)
    _bump(connection, user_id)
    return next(item for item in entries(connection, user_id) if item["id"] == identifier)


def create_entry(connection, user_id, kind, content, *, category="general_note"):
    connection.execute("BEGIN IMMEDIATE")
    return _insert(connection, user_id, kind, content, category)


def create_from_turn(connection, user_id, body):
    connection.execute("BEGIN IMMEDIATE")
    conversation = connection.execute("SELECT id,version FROM coach_conversations WHERE id=? AND user_id=?",
                                      (str(body.conversation_id), user_id)).fetchone()
    if conversation is None:
        raise HTTPException(404, "来源对话不存在。")
    if conversation["version"] != body.conversation_version or memory_revision(connection, user_id) != body.memory_revision:
        raise HTTPException(409, "对话或个人记忆已变化，请刷新来源后重新核对。")
    turn = connection.execute("SELECT message FROM coach_turns WHERE conversation_id=? AND version=?",
                              (conversation["id"], body.source_version)).fetchone()
    if turn is None or body.content not in turn["message"]:
        raise HTTPException(422, "记忆内容必须是所选本人原话中的连续摘录，不能改写或引用助手回答。")
    return _insert(connection, user_id, CATEGORY_KINDS[body.category], body.content, body.category,
                   source={"conversation_id": conversation["id"], "version": body.source_version,
                           "hash": hashlib.sha256(turn["message"].encode()).hexdigest()})


def update_entry(connection, user_id, identifier, version, content):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT version,kind FROM coach_memory_entries WHERE id=? AND user_id=?",
                             (str(identifier), user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "记忆不存在。")
    if row["version"] != version:
        raise HTTPException(409, "记忆已变化，请刷新后再修改。")
    if any(item["id"] != str(identifier) and item["kind"] == row["kind"] and item["content"] == content
           for item in entries(connection, user_id)):
        raise HTTPException(409, "这条记忆已经保存。")
    _bounded(connection, user_id, (str(identifier), content))
    # A user edit is new manually confirmed content, never a new source quotation.
    connection.execute("UPDATE coach_memory_entries SET content=?,version=version+1,updated_at=CURRENT_TIMESTAMP,"
                       "source_mode='manual',source_conversation_id=NULL,source_turn_version=NULL,source_text_hash=NULL,source_revoked=0 "
                       "WHERE id=? AND user_id=?", (content, str(identifier), user_id))
    _bump(connection, user_id)
    return next(item for item in entries(connection, user_id) if item["id"] == str(identifier))


def delete_entry(connection, user_id, identifier, version):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT version FROM coach_memory_entries WHERE id=? AND user_id=?",
                             (str(identifier), user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "记忆不存在。")
    if row["version"] != version:
        raise HTTPException(409, "记忆已变化，请刷新后再删除。")
    connection.execute("DELETE FROM coach_memory_entries WHERE id=? AND user_id=?", (str(identifier), user_id))
    # Retain the counter even when no entries remain: create -> delete is not version 0.
    _bump(connection, user_id)


def warm_memory(connection, user_id):
    """Only live, explicitly confirmed entries are eligible for new interpretation."""
    current = [item for item in entries(connection, user_id) if not item["stale"]]
    def context(item):
        return {"id": item["id"], "text": item["content"], "version": item["version"],
                "category": item["category"], "source": item["source"]}
    return {"user_preferences": [context(item) for item in current if item["kind"] == "user_preference"],
            "assistant_notes": [context(item) for item in current if item["kind"] == "assistant_note"],
            "revision": memory_revision(connection, user_id),
            "status": "user_saved_untrusted_context"}
