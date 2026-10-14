"""Explicit public-excerpt submission and local pre-review, never a RAG source."""
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

from rag.rag_service import KnowledgeUnavailable
from services.knowledge_feedback import KnowledgeWorkbench, content_signature, encode, signature

POLICY = "public-excerpt-precheck-v1"
ACTIVE = ("pending", "checked", "approved")


def reviewer(connection, user_id):
    return connection.execute("SELECT r.revision FROM knowledge_reviewers r WHERE r.user_id=? AND r.enabled=1 "
        "AND NOT EXISTS(SELECT 1 FROM guest_accounts g WHERE g.user_id=r.user_id)", (user_id,)).fetchone()


def set_reviewer(database, username, enabled):
    """Local administrative CLI only. No registration bootstrap or HTTP grant route."""
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE AND NOT EXISTS "
            "(SELECT 1 FROM guest_accounts WHERE user_id=users.id)", (username,)).fetchone()
        if user is None:
            raise ValueError("An existing non-guest account is required")
        previous = connection.execute("SELECT enabled,revision FROM knowledge_reviewers WHERE user_id=?", (user[0],)).fetchone()
        if previous is None or previous["enabled"] != int(enabled):
            connection.execute("INSERT INTO knowledge_reviewers(user_id,enabled,revision) VALUES (?,?,1) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,revision=revision+1,updated_at=CURRENT_TIMESTAMP",
                (user[0], int(enabled)))
        row = connection.execute("SELECT enabled,revision FROM knowledge_reviewers WHERE user_id=?", (user[0],)).fetchone()
        return {"enabled": bool(row["enabled"]), "revision": row["revision"]}


class KnowledgeReview:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id, self.retriever = database, user_id, retriever
        self.workbench = KnowledgeWorkbench(database, user_id, retriever)

    def permissions(self):
        with self.database.connect() as connection:
            return {"reviewer": bool(reviewer(connection, self.user_id)), "publication_enabled": False}

    def _require_reviewer(self, connection):
        role = reviewer(connection, self.user_id)
        if role is None:
            raise HTTPException(403, "没有公共摘录审核权限，请联系本机管理者。")
        return role["revision"]

    def _catalog(self):
        if not callable(getattr(self.retriever, "all_chunks", None)):
            raise KnowledgeUnavailable("Public catalog unavailable")
        hits = self.retriever.all_chunks()
        payloads = sorted((hit.model_dump(mode="json") for hit in hits), key=lambda hit: hit["chunk_id"])
        lexical = getattr(self.retriever, "lexical", self.retriever)
        engine = getattr(lexical, "tokenizer_identity", None)
        if not engine:
            raise KnowledgeUnavailable("Local precheck unavailable")
        return {hit["chunk_id"]: hit for hit in payloads}, signature({"sources": payloads, "engine": engine, "policy": POLICY})

    @staticmethod
    def gate_binding(row, corpus_hash):
        return signature({"policy": POLICY, "submission_id": row["id"], "candidate_version": row["candidate_version"],
            "content_hash": row["content_hash"], "source_hash": row["source_hash"], "corpus_hash": corpus_hash})

    @staticmethod
    def _event(connection, row, action, actor=None):
        connection.execute("INSERT INTO knowledge_review_events(submission_id,version,action,actor_id) VALUES (?,?,?,?)",
                           (row["id"], row["version"], action, actor))

    def _set(self, connection, row, status, reason="", **fields):
        allowed = {"gate_id", "reviewer_id", "reviewer_revision"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown submission field")
        changes = ",".join(f"{key}=?" for key in fields)
        connection.execute("UPDATE knowledge_submissions SET status=?,reason=?,version=version+1,updated_at=CURRENT_TIMESTAMP"
            + ("," + changes if changes else "") + " WHERE id=?", (status, reason, *fields.values(), row["id"]))
        return self._row(connection, row["id"])

    def _row(self, connection, identifier, *, owned=False):
        query = "SELECT s.* FROM knowledge_submissions s"
        params = [str(identifier)]
        if owned:
            query += " JOIN knowledge_candidates c ON c.id=s.candidate_id WHERE s.id=? AND c.user_id=?"
            params.append(self.user_id)
        else:
            query += " WHERE s.id=?"
        row = connection.execute(query, params).fetchone()
        if row is None:
            raise HTTPException(404, "提交不存在。")
        return dict(row)

    def _invalid_reason(self, connection, row, sources, corpus_hash):
        candidate = connection.execute("SELECT * FROM knowledge_candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        if candidate is None or candidate["version"] != row["candidate_version"] or candidate["status"] != "draft":
            return "candidate_changed"
        owner = KnowledgeWorkbench(self.database, candidate["user_id"], self.retriever)
        try:
            owner._check_origin(connection, candidate["feedback_id"])
        except HTTPException:
            return "answer_changed"
        source = json.loads(row["source_snapshot"])
        current = sources.get(source["chunk_id"])
        if not current or signature(current) != row["source_hash"]:
            return "source_changed"
        if row["gate_id"]:
            gate = connection.execute("SELECT corpus_hash,policy,passed,binding_hash FROM knowledge_review_gates WHERE id=? AND submission_id=?",
                                      (row["gate_id"], row["id"])).fetchone()
            if gate is None or gate["corpus_hash"] != corpus_hash or gate["policy"] != POLICY:
                return "corpus_changed"
            if row["status"] in ("checked", "approved") and (not gate["passed"] or gate["binding_hash"] != self.gate_binding(row, corpus_hash)):
                return "gate_changed"
        elif row["status"] in ("checked", "approved"):
            return "gate_changed"
        if row["status"] == "approved":
            role = reviewer(connection, row["reviewer_id"])
            if role is None or role["revision"] != row["reviewer_revision"]:
                return "reviewer_changed"
        return None

    def _validate(self, connection, row, sources, corpus_hash):
        reason = self._invalid_reason(connection, row, sources, corpus_hash)
        if reason:
            changed = self._set(connection, row, "invalidated", reason)
            self._event(connection, changed, reason)
            # Revocation must survive the rejected stale write, including an ABA restoration.
            connection.commit()
            raise HTTPException(409, "提交的候选、来源、语料或权限已变化，请刷新后重新提交。")

    def _view(self, connection, row, *, owned=False, unavailable=False):
        # Explicit allowlist: no owner identity, feedback, question, model answer or private note.
        result = {key: row[key] for key in ("id", "candidate_version", "content", "content_hash", "source_hash",
            "status", "version", "reason", "created_at", "updated_at")}
        result.update(source=json.loads(row["source_snapshot"]), published=False, used_as_knowledge=False,
                      source_unavailable=unavailable, approval_scope="pre_review_only")
        gate = connection.execute("SELECT id,binding_hash,passed,report,created_at FROM knowledge_review_gates "
            "WHERE id=? AND submission_id=?", (row["gate_id"], row["id"])).fetchone()
        result["gate"] = ({"id": gate["id"], "binding_hash": gate["binding_hash"], "passed": bool(gate["passed"]),
                           "report": json.loads(gate["report"]), "created_at": gate["created_at"]} if gate else None)
        result["events"] = [dict(event) for event in connection.execute(
            "SELECT version,action,created_at FROM knowledge_review_events WHERE submission_id=? ORDER BY version", (row["id"],))]
        if owned:
            result["candidate_id"] = row["candidate_id"]
        return result

    def list(self, *, queue=False):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if queue:
                self._require_reviewer(connection)
            try:
                sources, corpus_hash = self._catalog()
                unavailable = False
            except KnowledgeUnavailable:
                sources, corpus_hash, unavailable = {}, "", True
            query = "SELECT s.* FROM knowledge_submissions s JOIN knowledge_candidates c ON c.id=s.candidate_id"
            rows = connection.execute(query + ("" if queue else " WHERE c.user_id=?") + " ORDER BY s.created_at DESC,s.id",
                                      () if queue else (self.user_id,)).fetchall()
            values = []
            for value in rows:
                row = dict(value)
                if row["status"] in ACTIVE and not unavailable:
                    reason = self._invalid_reason(connection, row, sources, corpus_hash)
                    if reason:
                        row = self._set(connection, row, "invalidated", reason)
                        self._event(connection, row, reason)
                values.append(self._view(connection, row, owned=not queue, unavailable=unavailable))
            return values

    def submit(self, candidate_id, body):
        fingerprint = signature(body.model_dump(mode="json"))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = self.workbench._owned(connection, "knowledge_candidates", candidate_id)
            previous = connection.execute("SELECT * FROM knowledge_submissions WHERE candidate_id=? AND client_id=?",
                                          (str(candidate_id), str(body.client_id))).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise HTTPException(409, "提交标识已用于其他版本。")
                return self._view(connection, dict(previous), owned=True)
            self.workbench._version(candidate, body.version)
            if candidate["status"] != "draft":
                raise HTTPException(409, "已搁置的候选须修订并重新核对后再提交。")
            self.workbench._check_origin(connection, candidate["feedback_id"])
            self.workbench._source(SimpleNamespace(content=candidate["content"], source_hash=candidate["source_hash"],
                source_chunk_id=json.loads(candidate["source_snapshot"])["chunk_id"]))
            if connection.execute("SELECT 1 FROM knowledge_submissions WHERE candidate_id=? AND status IN ('pending','checked','approved')",
                                  (str(candidate_id),)).fetchone():
                raise HTTPException(409, "该候选已有进行中的提交，请先撤回或刷新。")
            if connection.execute("SELECT COUNT(*) FROM knowledge_submissions WHERE candidate_id=?", (str(candidate_id),)).fetchone()[0] >= 20:
                raise HTTPException(409, "该候选已达 20 次提交，请清理旧候选。")
            identifier = str(uuid4())
            connection.execute("INSERT INTO knowledge_submissions(id,candidate_id,candidate_version,client_id,request_hash,content,"
                "content_hash,source_snapshot,source_hash) VALUES (?,?,?,?,?,?,?,?,?)", (identifier, str(candidate_id), candidate["version"],
                str(body.client_id), fingerprint, candidate["content"], candidate["content_hash"], candidate["source_snapshot"], candidate["source_hash"]))
            row = self._row(connection, identifier)
            self._event(connection, row, "submitted", self.user_id)
            return self._view(connection, row, owned=True)

    def withdraw(self, identifier, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, identifier, owned=True)
            self.workbench._version(row, body.version)
            if row["status"] not in ACTIVE:
                raise HTTPException(409, "该提交已结束。")
            row = self._set(connection, row, "withdrawn", "owner_withdrew")
            self._event(connection, row, "withdrawn", self.user_id)
            return self._view(connection, row, owned=True)

    def check(self, identifier, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_reviewer(connection)
            row = self._row(connection, identifier)
            self.workbench._version(row, body.version)
            if row["status"] not in ("pending", "checked"):
                raise HTTPException(409, "仅待审核提交可以运行检查。")
            sources, corpus_hash = self._catalog()
            self._validate(connection, row, sources, corpus_hash)
            source = json.loads(row["source_snapshot"])
            lexical = getattr(self.retriever, "lexical", self.retriever)
            hits = lexical.search(source["title"] + " " + row["content"][:120], limit=20, topic=source["topic"])
            checks = {"public_provenance": source["origin"] == "public_web" and bool(source["url"] and source["license_url"]),
                "literal_excerpt": row["content"] in source["excerpt"], "content_hash": content_signature(row["content"]) == row["content_hash"],
                "source_lookup": source == sources.get(source["chunk_id"]),
                "lexical_source_smoke": any(hit.model_dump(mode="json") == source for hit in hits)}
            if self._catalog()[1] != corpus_hash:
                self._validate(connection, row, *self._catalog())
                raise HTTPException(409, "检查期间资料变化，请刷新后重试。")
            passed = all(checks.values())
            binding = self.gate_binding(row, corpus_hash)
            report = {"policy": POLICY, "checks": checks, "corpus_hash": corpus_hash, "source_count": len(sources),
                "real_provider_calls": 0, "semantic_quality": "not_evaluated", "release_regression": "not_run", "publication_allowed": False}
            gate_id = str(uuid4())
            connection.execute("INSERT INTO knowledge_review_gates(id,submission_id,binding_hash,corpus_hash,policy,passed,report) "
                "VALUES (?,?,?,?,?,?,?)", (gate_id, row["id"], binding, corpus_hash, POLICY, int(passed), encode(report)))
            row = self._set(connection, row, "checked" if passed else "pending", gate_id=gate_id)
            self._event(connection, row, "checked" if passed else "check_failed", self.user_id)
            return self._view(connection, row)

    def decide(self, identifier, body, *, approve):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = self._require_reviewer(connection)
            row = self._row(connection, identifier)
            self.workbench._version(row, body.version)
            if row["status"] not in ("pending", "checked"):
                raise HTTPException(409, "这版提交已结束或已变化，请刷新。")
            if approve:
                sources, corpus_hash = self._catalog()
                self._validate(connection, row, sources, corpus_hash)
                gate = connection.execute("SELECT * FROM knowledge_review_gates WHERE id=? AND submission_id=?",
                                          (str(body.gate_id), row["id"])).fetchone()
                if row["status"] != "checked" or row["gate_id"] != str(body.gate_id) or not gate or not gate["passed"] or gate["binding_hash"] != body.binding_hash:
                    raise HTTPException(409, "必须使用当前版本已通过的检查结果。")
                status, reason = "approved", "manual_pre_review"
            else:
                status, reason = "rejected", body.reason
            row = self._set(connection, row, status, reason, reviewer_id=self.user_id, reviewer_revision=revision)
            self._event(connection, row, status, self.user_id)
            return self._view(connection, row)
