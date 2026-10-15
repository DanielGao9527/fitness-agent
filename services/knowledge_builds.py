"""Isolated, content-bound public knowledge packages. Never activates a retriever."""
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import date
from uuid import UUID, uuid4

from fastapi import HTTPException

from model.embeddings import QwenEmbeddingProvider
from rag.catalog import Corpus
from rag.rag_service import INDEX_APP_ID, LocalKnowledgeRetriever, KnowledgeUnavailable
from rag.vector_store import VECTOR_APP_ID, VectorStore, VectorCacheUnavailable
from services.business_time import business_today
from services.knowledge_feedback import encode, signature, content_signature
from services.knowledge_review import KnowledgeReview
from services.knowledge_build_gate import regression, POLICY, PROTOCOL_PATH

FILES = ("sources.json", "knowledge.sqlite3", "knowledge-vectors.sqlite3", "regression.json")
BUILD_POLICY = "knowledge-package-cache-only-v1"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class FrozenKnowledge(LocalKnowledgeRetriever):
    """Read a built index without sync/rebuilding it during package verification."""
    def __init__(self, directory, tokenizer):
        super().__init__(directory / "sources.json", directory / "knowledge.sqlite3", tokenizer=tokenizer)
        self.corpus = Corpus.model_validate_json(self.source_path.read_bytes())

    def sync(self):
        return self.corpus


class KnowledgeBuilds:
    def __init__(self, database, user_id, retriever, settings, lock):
        retriever = getattr(retriever, "governance_base", retriever)
        self.database, self.user_id, self.retriever = database, user_id, retriever
        self.settings, self.lock = settings, lock
        self.review = KnowledgeReview(database, user_id, retriever)
        self.lexical = getattr(retriever, "lexical", retriever)
        self.root = database.path.resolve().parent / "knowledge-builds"

    def _child(self, name):
        parent = self.database.path.resolve().parent
        root = self.root.resolve()
        path = (root / name).resolve()
        if self.root.is_symlink() or not root.is_relative_to(parent) or path.parent != root:
            raise ValueError("Knowledge package path escapes the private directory")
        return path

    def _selection(self, connection, requested, sources, corpus_hash):
        values = []
        for target in requested:
            row = self.review._row(connection, target["id"])
            self.review.workbench._version(row, target["version"])
            if row["status"] != "approved":
                raise HTTPException(409, "只能构建仍有效、已人工预审的提交。")
            self.review._validate(connection, row, sources, corpus_hash)
            candidate = connection.execute("SELECT content,content_hash,source_hash FROM knowledge_candidates WHERE id=?",
                                           (row["candidate_id"],)).fetchone()
            source = json.loads(row["source_snapshot"])
            if (source["origin"] != "public_web" or not source["url"] or not source["license_url"]
                    or row["content"] not in source["excerpt"] or content_signature(row["content"]) != row["content_hash"]
                    or candidate is None or any(candidate[key] != row[key] for key in ("content", "content_hash", "source_hash"))):
                raise HTTPException(409, "候选正文或出处无法完整核对，未构建。")
            values.append(row)
        return values

    def _snapshot(self, requested):
        if not isinstance(self.lexical, LocalKnowledgeRetriever):
            raise KnowledgeUnavailable("Local corpus snapshot unavailable")
        try:
            raw = self.lexical.source_path.read_bytes()
            corpus = Corpus.model_validate_json(raw)
        except (OSError, ValueError):
            raise KnowledgeUnavailable("Current public source unavailable") from None
        sources, corpus_hash = self.review._catalog()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = self.review._require_reviewer(connection)
            selected = self._selection(connection, requested, sources, corpus_hash)
        if raw != self.lexical.source_path.read_bytes():
            raise HTTPException(409, "快照期间公共资料变化，请刷新后重试。")
        return corpus, hashlib.sha256(raw).hexdigest(), corpus_hash, revision, selected

    @staticmethod
    def _corpus(corpus, selected, today, identifier):
        data = corpus.model_dump(mode="json")
        data["version"] = "knowledge-build-" + identifier
        data["sources"] = [source for source in data["sources"] if source["status"] == "approved"
            and source["reviewed_on"] <= today.isoformat() <= source["review_due"]]
        aliases, selection, seen = {}, [], {}
        for row in selected:
            source = json.loads(row["source_snapshot"])
            original_id = source["chunk_id"]
            entry = next(item for item in data["sources"] if item["id"] == source["source_id"])
            section = next(item for item in entry["sections"] if item["id"] == original_id.split(":", 1)[1])
            # An identical full summary adds no new facts or duplicated ranking slots.
            key = (original_id, row["content_hash"])
            duplicate = key in seen
            chunk_id = seen.get(key, original_id if row["content"] == section["summary"] else None)
            if chunk_id is None:
                section_id = "review-" + UUID(row["id"]).hex
                entry["sections"].append({**section, "id": section_id, "summary": row["content"]})
                chunk_id = entry["id"] + ":" + section_id
                aliases[chunk_id] = original_id
            seen[key] = chunk_id
            selection.append({"id": row["id"], "version": row["version"], "candidate_version": row["candidate_version"], "gate_id": row["gate_id"],
                "source_chunk_id": original_id, "source_hash": row["source_hash"], "content_hash": row["content_hash"],
                "built_chunk_id": chunk_id, "deduplicated": chunk_id == original_id or duplicate})
        return Corpus.model_validate(data), aliases, selection

    def _verify(self, directory):
        if directory.is_symlink():
            raise ValueError("Symlink package is not supported")
        path = directory / "manifest.json"
        if path.is_symlink() or path.stat().st_size > 128 * 1024:
            raise ValueError("Invalid knowledge manifest")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (manifest["id"] != directory.name or manifest["policy"] != BUILD_POLICY
                or manifest["status"] != "built_not_published" or manifest["publication_allowed"] is not False
                or set(manifest["files"]) != set(FILES)):
            raise ValueError("Invalid knowledge package metadata")
        for name in FILES:
            artifact = directory / name
            if artifact.is_symlink() or artifact.stat().st_size > 64 * 1024 * 1024 or digest(artifact) != manifest["files"][name]:
                raise ValueError("Knowledge package changed")
        frozen = FrozenKnowledge(directory, manifest["tokenizer"])
        today = date.fromisoformat(manifest["date"])
        if frozen.tokenizer_identity != manifest["tokenizer_identity"]:
            raise ValueError("Tokenizer changed")
        for name, app_id in (("knowledge.sqlite3", INDEX_APP_ID), ("knowledge-vectors.sqlite3", VECTOR_APP_ID)):
            with closing(sqlite3.connect((directory / name).as_uri() + "?mode=ro", uri=True)) as connection:
                if (connection.execute("PRAGMA application_id").fetchone()[0] != app_id
                        or connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]):
                    raise ValueError("Invalid package database")
        provider = type("CacheIdentity", (), manifest["embedding"])()
        VectorStore(directory / "knowledge-vectors.sqlite3").load(frozen.export_documents(today=today), provider)
        with closing(sqlite3.connect(frozen.index_path.as_uri()+"?mode=ro", uri=True)) as connection:
            expected = hashlib.sha256(frozen.tokenizer_identity.encode()+b"\0"+frozen.source_path.read_bytes()).hexdigest()
            if connection.execute("SELECT value FROM catalog WHERE key='digest'").fetchone() != (expected,):
                raise ValueError("Source/index mismatch")
        report = json.loads((directory / "regression.json").read_text(encoding="utf-8"))
        if not report["passed"] or report["policy"] != POLICY or report["publication_allowed"] is not False:
            raise ValueError("Regression did not pass")
        return manifest, report

    def _view(self, manifest, report, *, stale=False, unavailable=False):
        stale = stale or report["protocol_sha256"] != digest(PROTOCOL_PATH)
        return {"id": manifest["id"], "manifest_hash": digest(self._child(manifest["id"]) / "manifest.json"),
            "date": manifest["date"], "status": "built_not_published", "published": False,
            "publication_allowed": False, "currently_stale": stale, "source_unavailable": unavailable,
            "selected_submissions": len(manifest["selection"]), "new_chunks": manifest["new_chunks"],
            "deduplicated": sum(item["deduplicated"] for item in manifest["selection"]),
            "chunk_count": manifest["vectors"]["chunk_count"], "embedding": manifest["embedding"],
            "real_provider_calls": 0, "regression": {key: report[key] for key in ("cases", "comparable", "not_comparable", "regressions", "passed")},
            "pending_checks": ["independent_semantic_quality", "hybrid_ranking_quality", "activation_and_rollback"]}

    def _failure(self, identifier, request_hash, code, report=None):
        path = self._child("failed-" + identifier + ".json")
        if path.exists():
            return  # Keep the first failed attempt; do not overwrite prior evidence.
        value = {"id": identifier, "request_hash": request_hash, "date": business_today().isoformat(),
            "status": "failed_not_published", "code": code, "real_provider_calls": 0,
            "publication_allowed": False, "regression": report if code == "development_regression" else None}
        with path.open("x", encoding="utf-8") as stream:
            stream.write(encode(value))

    def failures(self):
        with self.database.connect() as connection:
            self.review._require_reviewer(connection)
        self._child("check")
        if not self.root.exists():
            return []
        values = []
        for path in sorted(self.root.glob("failed-*.json"), key=lambda item: item.name):
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                identifier = str(UUID(value["id"]))
                if path.name != "failed-" + identifier + ".json" or value["status"] != "failed_not_published":
                    continue
                report = value["regression"]
                values.append({"id": identifier, "date": value["date"], "code": value["code"],
            "regressions": report["regressions"] if report else None,
                    "published": False, "publication_allowed": False})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return values

    def list(self):
        with self.database.connect() as connection:
            self.review._require_reviewer(connection)
        self._child("check")  # Validate the root even when it does not exist yet.
        if not self.root.exists():
            return []
        values = []
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            try:
                identifier = str(UUID(directory.name))
            except ValueError:
                continue
            if identifier != directory.name or not directory.is_dir():
                continue
            try:
                manifest, report = self._verify(self._child(identifier))
                requested = [{"id": item["id"], "version": item["version"]} for item in manifest["selection"]]
                unavailable = False
                try:
                    _, base_hash, corpus_hash, _, _ = self._snapshot(requested)
                    stale = (base_hash != manifest["base_source_hash"] or corpus_hash != manifest["review_corpus_hash"]
                             or business_today().isoformat() < manifest["date"])
                except KnowledgeUnavailable:
                    stale, unavailable = True, True
                except HTTPException as error:
                    if error.status_code == 403:
                        raise
                    stale = True
                values.append(self._view(manifest, report, stale=stale, unavailable=unavailable))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, VectorCacheUnavailable):
                values.append({"id": identifier, "status": "invalid_package", "published": False, "publication_allowed": False})
        return values

    def build(self, body):
        request_hash = signature(body.model_dump(mode="json"))
        identifier = str(body.client_id)
        requested = [{"id": str(item.id), "version": item.version} for item in body.submissions]
        with self.lock:
            corpus, base_hash, corpus_hash, role_revision, selected = self._snapshot(requested)
            destination = self._child(identifier)
            if destination.exists():
                manifest, report = self._verify(destination)
                if manifest["request_hash"] != request_hash:
                    raise HTTPException(409, "构建标识已用于其他内容，请重新核对。")
                stale = manifest["base_source_hash"] != base_hash or manifest["review_corpus_hash"] != corpus_hash or manifest["date"] > business_today().isoformat()
                return self._view(manifest, report, stale=stale)
            self.root.mkdir(parents=True, exist_ok=True)
            if sum(path.is_dir() and not path.name.startswith("staging-") for path in self.root.iterdir()) >= 20:
                raise HTTPException(409, "候选版本已达 20 个，请由本机管理员清理旧构建目录。")
            if len(list(self.root.glob("failed-*.json"))) >= 50:
                raise HTTPException(409, "失败记录已达 50 个，请由本机管理员归档后再构建。")
            staging = self._child("staging-" + uuid4().hex)
            staging.mkdir()
            report = None
            try:
                today = business_today()
                generated, aliases, selection = self._corpus(corpus, selected, today, identifier)
                (staging / "sources.json").write_text(generated.model_dump_json(), encoding="utf-8")
                candidate = LocalKnowledgeRetriever(staging / "sources.json", staging / "knowledge.sqlite3", tokenizer=self.lexical.tokenizer)
                candidate.sync()
                provider = getattr(self.retriever, "provider", None) or QwenEmbeddingProvider(self.settings)
                source_store = getattr(self.retriever, "vector_store", None) or VectorStore(self.settings.knowledge_vector_path or
                    self.lexical.index_path.with_name("knowledge-vectors.sqlite3"))
                store = VectorStore(staging / "knowledge-vectors.sqlite3")
                vectors = store.copy_cached(candidate, source_store, provider, today=today)
                report = regression(self.lexical, candidate, aliases, today)
                if not report["passed"]:
                    raise HTTPException(409, {"code": "KNOWLEDGE_BUILD_REGRESSION", "message": "候选版本出现词法回归，未保留构建，在线知识库未切换。",
                                             "regressions": report["regressions"]})
                (staging / "regression.json").write_text(encode(report), encoding="utf-8")
                manifest = {"id": identifier, "policy": BUILD_POLICY, "date": today.isoformat(),
                    "status": "built_not_published", "publication_allowed": False, "request_hash": request_hash,
                    "base_source_hash": base_hash, "review_corpus_hash": corpus_hash, "tokenizer": candidate.tokenizer,
                    "tokenizer_identity": candidate.tokenizer_identity, "embedding": {"identity": provider.identity, "dimensions": provider.dimensions},
                    "selection": selection, "new_chunks": len(aliases), "vectors": vectors,
                    "files": {name: digest(staging / name) for name in FILES}}
                # Validate the full package before any final-directory rename.
                (staging / "manifest.json").write_text(encode({**manifest, "id": staging.name}), encoding="utf-8")
                self._verify(staging)
                (staging / "manifest.json").write_text(encode(manifest), encoding="utf-8")
                with self.database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if self.review._require_reviewer(connection) != role_revision:
                        raise HTTPException(409, "构建期间审核权限变化，未保留构建。")
                    sources, current_hash = self.review._catalog()
                    self._selection(connection, requested, sources, current_hash)
                    if (digest(self.lexical.source_path) != base_hash or current_hash != corpus_hash or business_today() != today):
                        raise HTTPException(409, "构建期间来源或日期变化，未保留构建。")
                    os.rename(staging, destination)  # New UUID directory; never replace an existing package.
                return self._view(manifest, report)
            except VectorCacheUnavailable:
                self._failure(identifier, request_hash, "vector_cache_unavailable")
                raise
            except HTTPException as error:
                code = "development_regression" if isinstance(error.detail, dict) and error.detail.get("code") == "KNOWLEDGE_BUILD_REGRESSION" else "permissions_or_sources_changed"
                self._failure(identifier, request_hash, code, report)
                raise
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, KnowledgeUnavailable):
                self._failure(identifier, request_hash, "artifact_failure")
                raise
            finally:
                if staging.exists():
                    if self._child(staging.name) != staging or not staging.name.startswith("staging-"):
                        raise ValueError("Unsafe staging cleanup")
                    shutil.rmtree(staging)
