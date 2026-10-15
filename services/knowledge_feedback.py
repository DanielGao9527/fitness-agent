"""Owner-private feedback and source-bound excerpt drafts; never a retrieval source."""
import hashlib
import json
import unicodedata
from uuid import uuid4

from fastapi import HTTPException
from rag.rag_service import KnowledgeUnavailable


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def signature(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def answer_signature(message, knowledge):
    return signature({"message": message, "knowledge": knowledge})


def content_signature(content):
    return signature(" ".join(unicodedata.normalize("NFKC", content).split()))


class KnowledgeWorkbench:
    def __init__(self, database, user_id, retriever):
        retriever = getattr(retriever, "governance_base", retriever)
        self.database, self.user_id, self.retriever = database, user_id, retriever

    def _owned(self, connection, table, identifier):
        if table not in ("knowledge_feedback", "knowledge_candidates"):
            raise ValueError("Unknown workbench table")
        row = connection.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?",
                                 (str(identifier), self.user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "条目不存在。")
        return dict(row)

    def _origin(self, connection, conversation_id, version):
        row = connection.execute("SELECT t.message,t.response FROM coach_turns t JOIN coach_conversations c "
            "ON c.id=t.conversation_id WHERE c.user_id=? AND c.id=? AND t.version=?",
            (self.user_id, str(conversation_id), version)).fetchone()
        if row is None:
            raise HTTPException(404, "来源对话不存在。")
        knowledge = json.loads(row["response"]).get("knowledge", {})
        if knowledge.get("status") not in ("answered", "insufficient"):
            raise HTTPException(409, "仅能反馈已完成的一般知识回答或证据不足结果。")
        return row["message"], knowledge

    def _feedback(self, connection, row):
        try:
            question, knowledge = self._origin(connection, row["conversation_id"], row["turn_version"])
            stale = answer_signature(question, knowledge) != row["answer_hash"]
        except HTTPException:
            question, stale = "", True
        return {key: row[key] for key in ("id", "conversation_id", "turn_version", "kind", "comment", "version", "created_at")} | {
            "question": question, "origin_stale": stale, "sources": json.loads(row["source_snapshot"]),
            "used_as_knowledge": False}

    def feedback(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT * FROM knowledge_feedback WHERE user_id=? ORDER BY created_at DESC,id",
                                      (self.user_id,)).fetchall()
            return [self._feedback(connection, dict(row)) for row in rows]

    def create_feedback(self, body):
        fingerprint = signature(body.model_dump(mode="json"))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM knowledge_feedback WHERE user_id=? AND client_id=?",
                                      (self.user_id, str(body.client_id))).fetchone()
            if row:
                if row["request_hash"] != fingerprint:
                    raise HTTPException(409, "反馈标识已用于其他内容，请重新核对。")
                return self._feedback(connection, dict(row))
            question, knowledge = self._origin(connection, body.conversation_id, body.turn_version)
            if answer_signature(question, knowledge) != body.answer_hash:
                raise HTTPException(409, "原问题或回答已变化，请刷新后再反馈。")
            duplicate = connection.execute("SELECT id FROM knowledge_feedback WHERE user_id=? AND conversation_id=? "
                "AND turn_version=? AND answer_hash=? AND kind=?", (self.user_id, str(body.conversation_id),
                body.turn_version, body.answer_hash, body.kind)).fetchone()
            if duplicate:
                raise HTTPException(409, "这条回答已有同类反馈，请先查看或删除旧反馈。")
            if connection.execute("SELECT COUNT(*) FROM knowledge_feedback WHERE user_id=?", (self.user_id,)).fetchone()[0] >= 100:
                raise HTTPException(409, "反馈已达 100 条，请先清理旧条目。")
            sources = [{"chunk_id": source["chunk_id"], "source_version": source["source_version"],
                        "source_hash": signature(source)} for source in knowledge.get("sources", [])]
            identifier = str(uuid4())
            connection.execute("INSERT INTO knowledge_feedback(id,user_id,client_id,request_hash,conversation_id,turn_version,"
                "answer_hash,source_snapshot,kind,comment) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, self.user_id, str(body.client_id), fingerprint, str(body.conversation_id), body.turn_version,
                 body.answer_hash, encode(sources), body.kind, body.comment))
            return self._feedback(connection, self._owned(connection, "knowledge_feedback", identifier))

    def public_sources(self):
        if not callable(getattr(self.retriever, "all_chunks", None)):
            raise KnowledgeUnavailable("Public catalog unavailable")
        return [{"source": hit.model_dump(mode="json"), "source_hash": signature(hit.model_dump(mode="json"))}
                for hit in self.retriever.all_chunks()
                if hit.origin == "public_web" and hit.url and hit.license_url]

    def _source(self, body):
        hits = self.retriever.get_chunks([body.source_chunk_id])
        if not hits or hits[0].origin != "public_web" or not hits[0].url or not hits[0].license_url:
            raise HTTPException(409, "公共出处已失效或未核实，请重新选择有效资料。")
        source = hits[0].model_dump(mode="json")
        if signature(source) != body.source_hash:
            raise HTTPException(409, "公共资料已变化，请重新核对来源与摘录。")
        if body.content not in source["excerpt"]:
            raise HTTPException(422, "候选必须是所选公共摘要中的连续摘录，不能粘贴私人原话或自由改写。")
        return source

    def _candidate(self, connection, row):
        source = json.loads(row["source_snapshot"])
        unavailable = False
        try:
            current = self.retriever.get_chunks([source["chunk_id"]])
        except KnowledgeUnavailable:
            current, unavailable = [], True
        source_stale = not current or signature(current[0].model_dump(mode="json")) != row["source_hash"]
        feedback = self._owned(connection, "knowledge_feedback", row["feedback_id"])
        origin_stale = self._feedback(connection, feedback)["origin_stale"]
        events = [dict(event) for event in connection.execute("SELECT version,action,content_hash,source_hash,note,created_at "
            "FROM knowledge_candidate_events WHERE candidate_id=? ORDER BY version", (row["id"],)).fetchall()]
        return {key: row[key] for key in ("id", "feedback_id", "content", "status", "review_note", "version", "created_at", "updated_at")} | {
            "source": source, "source_hash": row["source_hash"], "source_stale": bool(source_stale), "source_unavailable": unavailable,
            "origin_stale": origin_stale, "events": events, "published": False, "used_as_knowledge": False}

    def candidates(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT * FROM knowledge_candidates WHERE user_id=? ORDER BY updated_at DESC,id",
                                      (self.user_id,)).fetchall()
            return [self._candidate(connection, dict(row)) for row in rows]

    def _check_origin(self, connection, feedback_id):
        feedback = self._owned(connection, "knowledge_feedback", feedback_id)
        if feedback["kind"] != "knowledge_issue":
            raise HTTPException(409, "个人纠正只能留在本人档案/记忆流程，不能作为公共知识候选。")
        if self._feedback(connection, feedback)["origin_stale"]:
            raise HTTPException(409, "反馈关联的回答已变化，请先重新核对原回答。")

    def _duplicate(self, connection, content_hash, source_hash, exclude=""):
        if connection.execute("SELECT id FROM knowledge_candidates WHERE user_id=? AND content_hash=? AND source_hash=? AND id<>?",
                              (self.user_id, content_hash, source_hash, str(exclude))).fetchone():
            raise HTTPException(409, "相同来源版本的这段摘录已存在，请查看已有候选。")

    @staticmethod
    def _event(connection, row, action):
        connection.execute("INSERT INTO knowledge_candidate_events(candidate_id,version,action,content_hash,source_hash,note) "
            "VALUES (?,?,?,?,?,?)", (row["id"], row["version"], action, row["content_hash"], row["source_hash"], row["review_note"]))

    def create_candidate(self, body):
        fingerprint = signature(body.model_dump(mode="json"))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM knowledge_candidates WHERE user_id=? AND client_id=?",
                                      (self.user_id, str(body.client_id))).fetchone()
            if row:
                if row["request_hash"] != fingerprint:
                    raise HTTPException(409, "候选标识已用于其他内容，请重新核对。")
                return self._candidate(connection, dict(row))
            self._check_origin(connection, body.feedback_id)
            source = self._source(body)
            digest = content_signature(body.content)
            self._duplicate(connection, digest, body.source_hash)
            if connection.execute("SELECT COUNT(*) FROM knowledge_candidates WHERE user_id=?", (self.user_id,)).fetchone()[0] >= 50:
                raise HTTPException(409, "候选已达 50 条，请先清理旧条目。")
            identifier = str(uuid4())
            connection.execute("INSERT INTO knowledge_candidates(id,user_id,feedback_id,client_id,request_hash,content,source_snapshot,"
                "source_hash,content_hash) VALUES (?,?,?,?,?,?,?,?,?)", (identifier, self.user_id, str(body.feedback_id),
                str(body.client_id), fingerprint, body.content, encode(source), body.source_hash, digest))
            row = self._owned(connection, "knowledge_candidates", identifier)
            self._event(connection, row, "created")
            return self._candidate(connection, row)

    def revise(self, identifier, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned(connection, "knowledge_candidates", identifier)
            self._version(row, body.version)
            self._check_origin(connection, row["feedback_id"])
            source = self._source(body)
            digest = content_signature(body.content)
            self._duplicate(connection, digest, body.source_hash, identifier)
            connection.execute("UPDATE knowledge_candidates SET content=?,source_snapshot=?,source_hash=?,content_hash=?,status='draft',"
                "review_note='',version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                (body.content, encode(source), body.source_hash, digest, str(identifier), self.user_id))
            row = self._owned(connection, "knowledge_candidates", identifier)
            self._event(connection, row, "revised")
            return self._candidate(connection, row)

    def reject(self, identifier, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned(connection, "knowledge_candidates", identifier)
            self._version(row, body.version)
            connection.execute("UPDATE knowledge_candidates SET status='rejected',review_note=?,version=version+1,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (body.note, str(identifier), self.user_id))
            row = self._owned(connection, "knowledge_candidates", identifier)
            self._event(connection, row, "rejected")
            return self._candidate(connection, row)

    @staticmethod
    def _version(row, version):
        if row["version"] != version:
            raise HTTPException(409, "条目已变化，请刷新后重新核对。")

    def delete(self, table, identifier, version):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._version(self._owned(connection, table, identifier), version)
            connection.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (str(identifier), self.user_id))
