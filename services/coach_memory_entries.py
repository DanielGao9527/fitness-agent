"""Small, user-confirmed conversational memory; never a plan constraint."""
from uuid import uuid4

from fastapi import HTTPException

MAX_ENTRIES = 20
MAX_TOTAL_CHARS = 1200


def entries(connection, user_id):
    return [dict(row) for row in connection.execute(
        "SELECT id,kind,content,version,created_at,updated_at FROM coach_memory_entries "
        "WHERE user_id=? ORDER BY created_at,id", (user_id,)).fetchall()]


def _bounded(connection, user_id, replacement=None):
    current = entries(connection, user_id)
    if replacement:
        current = [{**item, "content": replacement[1]} if item["id"] == replacement[0] else item
                   for item in current]
    if len(current) > MAX_ENTRIES or sum(len(item["content"]) for item in current) > MAX_TOTAL_CHARS:
        raise HTTPException(409, "个人记忆已达容量上限，请先精简或删除旧条目。")


def create_entry(connection, user_id, kind, content):
    connection.execute("BEGIN IMMEDIATE")
    if any(item["kind"] == kind and item["content"] == content for item in entries(connection, user_id)):
        raise HTTPException(409, "这条记忆已经保存。")
    identifier = str(uuid4())
    connection.execute("INSERT INTO coach_memory_entries(id,user_id,kind,content) VALUES (?,?,?,?)",
                       (identifier, user_id, kind, content))
    _bounded(connection, user_id)
    return next(item for item in entries(connection, user_id) if item["id"] == identifier)


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
    connection.execute("UPDATE coach_memory_entries SET content=?,version=version+1,updated_at=CURRENT_TIMESTAMP "
                       "WHERE id=? AND user_id=?", (content, str(identifier), user_id))
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


def warm_memory(connection, user_id):
    """Render untrusted, explicitly saved notes with their owner-scoped IDs."""
    current = entries(connection, user_id)
    return {"user_preferences": [{"id": item["id"], "text": item["content"], "version": item["version"]}
                                 for item in current if item["kind"] == "user_preference"],
            "assistant_notes": [{"id": item["id"], "text": item["content"], "version": item["version"]}
                                for item in current if item["kind"] == "assistant_note"],
            "status": "user_saved_untrusted_context"}
