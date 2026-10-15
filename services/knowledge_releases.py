"""Persistent pre-release registry. Quality acceptance and online activation stay closed."""
import hashlib
import json
import sqlite3

from fastapi import HTTPException

from model.embeddings import QwenEmbeddingProvider
from rag.catalog import Corpus
from rag.rag_service import KnowledgeUnavailable
from rag.vector_store import VectorCacheUnavailable
from services.business_time import business_today
from services.knowledge_build_gate import PROTOCOL_PATH
from services.knowledge_builds import KnowledgeBuilds, digest
from services.knowledge_feedback import encode, signature
from services.knowledge_review import reviewer

POLICY = "knowledge-release-readiness-v1"
ACTIVE = ("registered", "blocked")
PENDING = ("independent_semantic_quality", "hybrid_ranking_quality", "activation_and_rollback")


class StalePackage(ValueError):
    pass


class KnowledgeReleases:
    def __init__(self, database, user_id, retriever, settings, lock):
        self.database, self.user_id, self.lock = database, user_id, lock
        self.builds = KnowledgeBuilds(database, user_id, retriever, settings, lock)
        self.review = self.builds.review

    @staticmethod
    def _row(connection, identifier):
        row = connection.execute("SELECT * FROM knowledge_releases WHERE id=?", (str(identifier),)).fetchone()
        if row is None:
            raise HTTPException(404, "登记版本不存在。")
        return dict(row)

    def _set(self, connection, row, status, reason, report=None, *, actor=None):
        connection.execute("UPDATE knowledge_releases SET status=?,reason=?,report=?,actor_id=?,version=version+1,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?", (status, reason, encode(report or {}),
            actor, row["id"], row["version"]))
        return self._row(connection, row["id"])

    def _invalidate(self, connection, row, reason):
        # A pre-review validation may already have committed a cascading invalidation.
        current = self._row(connection, row["id"])
        if current["status"] in ACTIVE:
            current = self._set(connection, current, "invalidated", reason)
        connection.commit()  # Observed revocation must survive the rejected request and ABA.
        return current

    def _package(self, connection, identifier, expected_hash):
        """Verify the immutable bundle and its live source/approval dependencies in one DB transaction."""
        directory = self.builds._child(str(identifier))
        if digest(directory / "manifest.json") != expected_hash:
            raise StalePackage("package_changed")
        try:
            manifest, report = self.builds._verify(directory)
        except sqlite3.Error:
            raise StalePackage("package_changed") from None
        if manifest["date"] != business_today().isoformat() or report["protocol_sha256"] != digest(PROTOCOL_PATH):
            raise StalePackage("protocol_or_date_changed")
        try:
            raw = self.builds.lexical.source_path.read_bytes()
            corpus = Corpus.model_validate_json(raw)
        except (OSError, ValueError):
            raise KnowledgeUnavailable("Current public corpus unavailable") from None
        sources, corpus_hash = self.review._catalog()
        if hashlib.sha256(raw).hexdigest() != manifest["base_source_hash"] or corpus_hash != manifest["review_corpus_hash"]:
            raise StalePackage("sources_changed")
        requested = [{"id": entry["id"], "version": entry["version"]} for entry in manifest["selection"]]
        if not 1 <= len(requested) <= 20 or len({item["id"] for item in requested}) != len(requested):
            raise StalePackage("package_changed")
        selected = self.builds._selection(connection, requested, sources, corpus_hash)
        generated, aliases, selection = self.builds._corpus(corpus, selected, business_today(), str(identifier))
        provider = getattr(self.builds.retriever, "provider", None) or QwenEmbeddingProvider(self.builds.settings)
        if (selection != manifest["selection"] or len(aliases) != manifest["new_chunks"]
                or (directory / "sources.json").read_bytes() != generated.model_dump_json().encode()
                or manifest["tokenizer_identity"] != self.builds.lexical.tokenizer_identity
                or manifest["embedding"] != {"identity": provider.identity, "dimensions": provider.dimensions}):
            raise StalePackage("package_changed")
        # Recheck after reading all artifacts; the shared lock also serializes local build operations.
        if (digest(directory / "manifest.json") != expected_hash or raw != self.builds.lexical.source_path.read_bytes()
                or self.review._catalog()[1] != corpus_hash or manifest["date"] != business_today().isoformat()):
            raise StalePackage("sources_changed")
        return manifest, report

    def _current(self, connection, row):
        role = reviewer(connection, row["registrant_id"])
        if role is None or role["revision"] != row["registrant_revision"]:
            raise StalePackage("registrant_changed")
        manifest, report = self._package(connection, row["id"], row["manifest_hash"])
        members = {(value["submission_id"], value["submission_version"]) for value in connection.execute(
            "SELECT submission_id,submission_version FROM knowledge_release_members WHERE release_id=?", (row["id"],))}
        if members != {(entry["id"], entry["version"]) for entry in manifest["selection"]}:
            raise StalePackage("submission_changed")
        return manifest, report

    def _refresh(self, connection, row):
        if row["status"] not in ACTIVE:
            return row, False
        try:
            self._current(connection, row)
            return row, False
        except KnowledgeUnavailable:
            return row, True  # Temporary outage is not evidence that consent was revoked.
        except HTTPException as error:
            if error.status_code not in (404, 409):
                raise
            return self._invalidate(connection, row, "submission_changed"), False
        except (OSError, ValueError, KeyError, TypeError, VectorCacheUnavailable) as error:
            reason = str(error) if isinstance(error, StalePackage) else "package_changed"
            return self._invalidate(connection, row, reason), False

    @staticmethod
    def _view(connection, row, *, unavailable=False):
        # No actor, owner, feedback, conversation, private notes, or caller-supplied text.
        result = {key: row[key] for key in ("id", "manifest_hash", "status", "version", "reason", "created_at", "updated_at")}
        result.update(published=False, publication_allowed=False, source_unavailable=unavailable,
                      pending_checks=list(PENDING), report=json.loads(row["report"]))
        result["events"] = [dict(event) for event in connection.execute(
            "SELECT version,action,created_at FROM knowledge_release_events WHERE release_id=? ORDER BY version", (row["id"],))]
        return result

    def list(self):
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.review._require_reviewer(connection)
            rows = connection.execute("SELECT * FROM knowledge_releases ORDER BY created_at DESC,id").fetchall()
            values = []
            for value in rows:
                row, unavailable = self._refresh(connection, dict(value))
                # _refresh may commit a durable invalidation; reacquire before the next row.
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                self.review._require_reviewer(connection)
                values.append(self._view(connection, row, unavailable=unavailable))
            return values

    def register(self, body):
        identifier = str(body.id)
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = self.review._require_reviewer(connection)
            previous = connection.execute("SELECT * FROM knowledge_releases WHERE id=?", (identifier,)).fetchone()
            if previous:
                row = dict(previous)
                if row["manifest_hash"] != body.manifest_hash:
                    raise HTTPException(409, "此版本已绑定其他构建内容，请刷新核对。")
                row, unavailable = self._refresh(connection, row)
                return self._view(connection, row, unavailable=unavailable)
            if connection.execute("SELECT COUNT(*) FROM knowledge_releases").fetchone()[0] >= 20:
                raise HTTPException(409, "已达 20 个登记版本，请由本机管理员归档。")
            try:
                manifest, _ = self._package(connection, identifier, body.manifest_hash)
            except StalePackage:
                raise HTTPException(409, "构建内容、出处或检查协议已变化，不能登记。") from None
            if self.review._require_reviewer(connection) != revision:
                raise HTTPException(409, "登记期间审核权限已变化。")
            connection.execute("INSERT INTO knowledge_releases(id,manifest_hash,registrant_id,registrant_revision,status,reason,actor_id) "
                "VALUES (?,?,?,?,'registered','registered',?)", (identifier, body.manifest_hash, self.user_id, revision, self.user_id))
            connection.executemany("INSERT INTO knowledge_release_members(release_id,submission_id,submission_version) VALUES (?,?,?)",
                [(identifier, entry["id"], entry["version"]) for entry in manifest["selection"]])
            connection.execute("INSERT INTO knowledge_release_events(release_id,version,action,report,actor_id) VALUES (?,1,'registered','{}',?)",
                               (identifier, self.user_id))
            return self._view(connection, self._row(connection, identifier))

    def check(self, identifier, body):
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.review._require_reviewer(connection)
            row = self._row(connection, identifier)
            self.review.workbench._version(row, body.version)
            row, unavailable = self._refresh(connection, row)
            if unavailable:
                raise KnowledgeUnavailable("Public sources temporarily unavailable")
            if row["status"] not in ACTIVE:
                raise HTTPException(409, "该登记已撤回或失效，不能继续检查或发布。")
            report = {"policy": POLICY, "manifest_sha256": row["manifest_hash"], "build_protocol_sha256": digest(PROTOCOL_PATH),
                "engineering_checks": {"package_integrity": True, "source_composition": True, "live_pre_reviews": True,
                                       "cache_coverage": True, "development_lexical_regression": True},
                "independent_semantic_quality": "not_evaluated", "hybrid_ranking_quality": "not_evaluated",
                "blockers": list(PENDING), "publication_allowed": False, "real_provider_calls": 0}
            report["binding_hash"] = signature({"release_id": row["id"], "release_version": row["version"] + 1, "report": report})
            row = self._set(connection, row, "blocked", "quality_acceptance_missing", report, actor=self.user_id)
            return self._view(connection, row)

    def withdraw(self, identifier, body):
        with self.lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.review._require_reviewer(connection)
            row = self._row(connection, identifier)
            self.review.workbench._version(row, body.version)
            if row["status"] not in ACTIVE:
                raise HTTPException(409, "该登记已结束。")
            row = self._set(connection, row, "withdrawn", "reviewer_withdrew", actor=self.user_id)
            return self._view(connection, row)
