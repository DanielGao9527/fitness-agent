"""Manual approval and transactional live pointer. Only complete, current quality receipts pass."""
import json

from fastapi import HTTPException

from rag.rag_service import KnowledgeUnavailable
from services import knowledge_quality
from services.knowledge_feedback import signature
from services.knowledge_releases import KnowledgeReleases, ACTIVE
from services.knowledge_review import reviewer


class KnowledgePublications(KnowledgeReleases):
    def validate(self, connection, identifier):
        row = self._row(connection, identifier)
        row, unavailable = self._refresh(connection, row)
        if unavailable:
            raise KnowledgeUnavailable("Publication sources temporarily unavailable")
        approval = connection.execute("SELECT * FROM knowledge_publications WHERE release_id=?", (row["id"],)).fetchone()
        if not approval or approval["state"] != "approved" or row["status"] not in ACTIVE:
            raise HTTPException(409, "该版本尚未批准或已经失效。")
        role = reviewer(connection, approval["approver_id"])
        report = json.loads(row["report"])
        valid = role is not None and role["revision"] == approval["approver_revision"] and row["version"] == approval["release_version"]
        valid = valid and signature(report) == approval["quality_hash"]
        try:
            valid = valid and knowledge_quality.current(report, self.cache_directory, self.builds.retriever)
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            self._invalidate(connection, row, "quality_or_approval_changed")
            raise HTTPException(409, "质量凭据或批准已变化，旧版本已失效。")
        manifest, _ = self._current(connection, row)
        return dict(approval), manifest

    def approve(self, identifier, body):
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = self.review._require_reviewer(connection)
            row = self._row(connection, identifier)
            self.review.workbench._version(row, body.version)
            row, unavailable = self._refresh(connection, row)
            if unavailable:
                raise KnowledgeUnavailable("Public sources temporarily unavailable")
            report = json.loads(row["report"])
            binding = report.get("binding_hash")
            unsigned = {key: value for key, value in report.items() if key != "binding_hash"}
            expected = signature({"release_id": row["id"], "release_version": row["version"], "report": unsigned})
            try:
                accepted = (row["status"] in ACTIVE and binding == body.binding_hash == expected
                    and report.get("manifest_sha256") == row["manifest_hash"]
                    and knowledge_quality.current(report, self.cache_directory, self.builds.retriever))
            except (OSError, ValueError, KeyError, TypeError):
                accepted = False
            if not accepted:
                raise HTTPException(409, "必须使用本版本有效、完整通过的正式质量报告；当前未批准。")
            prior = connection.execute("SELECT * FROM knowledge_publications WHERE release_id=?", (row["id"],)).fetchone()
            if prior:
                self.validate(connection, row["id"])
                return self._view(connection, row)
            connection.execute("INSERT INTO knowledge_publications(release_id,release_version,quality_hash,approver_id,approver_revision,state) "
                "VALUES (?,?,?,?,?,'approved')", (row["id"], row["version"], signature(report), self.user_id, revision))
            return self._view(connection, row)

    @staticmethod
    def head(connection):
        row = dict(connection.execute("SELECT revision,release_id FROM knowledge_live WHERE id=1").fetchone())
        row["base_active"] = row["release_id"] is None
        return row

    def live(self):
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.review._require_reviewer(connection)
            head = self.head(connection)
            if head["release_id"]:
                try:
                    self.validate(connection, head["release_id"])
                except KnowledgeUnavailable:
                    head["source_unavailable"] = True
                except HTTPException as error:
                    if error.status_code != 409:
                        raise
                head = {**head, **self.head(connection)}
            head["events"] = [dict(row) for row in connection.execute(
                "SELECT revision,release_id,previous_id,action,created_at FROM knowledge_live_events ORDER BY revision DESC LIMIT 50")]
            return head

    def switch(self, body):
        fingerprint = signature({"actor_id": self.user_id, "request": body.model_dump(mode="json")})
        target = str(body.release_id) if body.release_id else None
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.review._require_reviewer(connection)
            previous = connection.execute("SELECT revision,request_hash FROM knowledge_live_events WHERE client_id=?", (str(body.client_id),)).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise HTTPException(409, "切换标识已用于其他操作。")
                return {**self.head(connection), "applied_revision": previous["revision"], "reused": True}
            head = self.head(connection)
            if head["revision"] != body.expected_revision:
                raise HTTPException(409, "在线版本已变化，请刷新后重新核对。")
            if body.action == "activate" and target is None:
                raise HTTPException(422, "请选择已批准版本。")
            if target:
                if body.action == "rollback" and not connection.execute("SELECT 1 FROM knowledge_live_events WHERE release_id=?", (target,)).fetchone():
                    raise HTTPException(409, "只能回滚到曾上线且仍有效的版本。")
                approval, _ = self.validate(connection, target)
                if approval["version"] != body.publication_version:
                    raise HTTPException(409, "发布批准已变化，请重新核对。")
            if head["release_id"] == target:
                return {**head, "applied_revision": head["revision"], "reused": True}
            connection.execute("UPDATE knowledge_live SET release_id=?,revision=revision+1,action=?,actor_id=?,client_id=?,request_hash=? WHERE id=1",
                (target, body.action, self.user_id, str(body.client_id), fingerprint))
            return {**self.head(connection), "applied_revision": head["revision"] + 1, "reused": False}
