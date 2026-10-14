"""Reviewable, source-bound excerpts from confirmed conversation history."""
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException

from services.coach_memory import _terms, confirmation_revisions, current_equipment_evidence
from services.profile_facts import equipment_revision


def _source(connection, user_id, conversation_id):
    conversation = connection.execute("SELECT id,day FROM coach_conversations WHERE id=? AND user_id=?",
                                      (str(conversation_id), user_id)).fetchone()
    if conversation is None:
        raise HTTPException(404, "对话不存在。")
    rows = connection.execute("SELECT rowid AS turn_rowid,version,message,response FROM coach_turns WHERE conversation_id=? "
                              "ORDER BY version", (str(conversation_id),)).fetchall()
    confirmed = {}
    for row in rows:
        response = json.loads(row["response"])
        if response.get("source") in ("reviewed_understanding", "applied_understanding") and response.get("status") in ("reviewed", "applied"):
            confirmed.update(confirmation_revisions(response))
    profile = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    equipment = json.loads(profile[0]).get("equipment") if profile else None
    revision = equipment_revision(connection, user_id)
    used = [(row["version"], row["message"], row["turn_rowid"]) for row in rows
            if row["version"] in confirmed
            and current_equipment_evidence(row["message"], confirmed[row["version"]], equipment, revision)]
    serialized = json.dumps([(version, message) for version, message, _ in used], ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return conversation["day"], used, fingerprint


def _view(connection, user_id, row):
    try:
        day, _, fingerprint = _source(connection, user_id, row["conversation_id"])
        stale = fingerprint != row["source_hash"]
    except HTTPException:
        day, stale = None, True
    return {"id": row["id"], "conversation_id": row["conversation_id"], "day": day,
            "summary": row["summary"], "source_versions": json.loads(row["source_versions"]),
            "status": row["status"], "version": row["version"], "stale": stale,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_digests(connection, user_id):
    rows = connection.execute("SELECT * FROM coach_memory_digests WHERE user_id=? ORDER BY updated_at DESC,id LIMIT 50",
                              (user_id,)).fetchall()
    return [_view(connection, user_id, row) for row in rows]


def propose_digest(connection, user_id, conversation_id):
    connection.execute("BEGIN IMMEDIATE")
    day, used, fingerprint = _source(connection, user_id, conversation_id)
    if not used:
        raise HTTPException(409, "这段对话还没有已核对的原话，不能生成摘要候选。")
    selected = used[-3:]
    summary = "；".join(f"第{version}条：{message[:140]}" for version, message, _ in selected)[:480]
    versions = json.dumps([version for version, _, _ in selected])
    existing = connection.execute("SELECT * FROM coach_memory_digests WHERE user_id=? AND conversation_id=?",
                                  (user_id, str(conversation_id))).fetchone()
    if existing:
        if existing["source_hash"] == fingerprint:
            return _view(connection, user_id, existing)
        connection.execute("UPDATE coach_memory_digests SET summary=?,source_versions=?,source_hash=?,status='pending',"
                           "version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                           (summary, versions, fingerprint, existing["id"], user_id))
        identifier = existing["id"]
    else:
        identifier = str(uuid4())
        connection.execute("INSERT INTO coach_memory_digests(id,user_id,conversation_id,source_versions,source_hash,summary,status) "
                           "VALUES (?,?,?,?,?,?,'pending')",
                           (identifier, user_id, str(conversation_id), versions, fingerprint, summary))
    row = connection.execute("SELECT * FROM coach_memory_digests WHERE id=? AND user_id=?",
                             (identifier, user_id)).fetchone()
    return _view(connection, user_id, row)


def approve_digest(connection, user_id, identifier, version):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT * FROM coach_memory_digests WHERE id=? AND user_id=?",
                             (str(identifier), user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "摘要候选不存在。")
    if row["version"] != version:
        raise HTTPException(409, "摘要候选已变化，请刷新后再核对。")
    view = _view(connection, user_id, row)
    if view["stale"]:
        raise HTTPException(409, "原对话已变化，请重新生成摘要候选。")
    if row["status"] == "approved":
        return view
    connection.execute("UPDATE coach_memory_digests SET status='approved',version=version+1,"
                       "updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (str(identifier), user_id))
    return _view(connection, user_id, connection.execute(
        "SELECT * FROM coach_memory_digests WHERE id=? AND user_id=?", (str(identifier), user_id)).fetchone())


def delete_digest(connection, user_id, identifier, version):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT version FROM coach_memory_digests WHERE id=? AND user_id=?",
                             (str(identifier), user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "摘要候选不存在。")
    if row["version"] != version:
        raise HTTPException(409, "摘要候选已变化，请刷新后再删除。")
    connection.execute("DELETE FROM coach_memory_digests WHERE id=? AND user_id=?", (str(identifier), user_id))


def recall_digests(connection, user_id, query, *, before_day=None, before_turn_rowid=None,
                   exclude_conversation_id=None, limit=2):
    terms = _terms(query)
    if not terms:
        return []
    rows = connection.execute("SELECT * FROM coach_memory_digests WHERE user_id=? AND status='approved' "
                              "AND (? IS NULL OR conversation_id<>?) ORDER BY updated_at DESC,id LIMIT 50",
                              (user_id, exclude_conversation_id, exclude_conversation_id)).fetchall()
    matches = []
    for row in rows:
        view = _view(connection, user_id, row)
        if view["stale"] or before_day and view["day"] > before_day:
            continue
        _, used, _ = _source(connection, user_id, row["conversation_id"])
        if before_turn_rowid is not None:
            if any(turn_rowid >= before_turn_rowid for _, _, turn_rowid in used):
                continue
        overlap = terms & _terms(view["summary"])
        if overlap:
            matches.append((len(overlap) / len(terms), view["day"], view))
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [{"conversation_id": item["conversation_id"], "day": item["day"],
             "excerpt": item["summary"], "source_versions": item["source_versions"],
             "status": "approved_historical_digest"} for _, _, item in matches[:limit]]
