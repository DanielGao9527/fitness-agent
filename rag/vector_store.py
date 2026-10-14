"""Explicitly built public-document vectors; online reads never populate this cache."""
import hashlib
import math
import sqlite3
import struct
import threading
from contextlib import closing
from pathlib import Path
from typing import Protocol

from rag.rag_service import TEXT_TEMPLATE


VECTOR_APP_ID = 0x46564531


class EmbeddingProvider(Protocol):
    identity: str
    dimensions: int

    def embed(self, texts: list[str], *, text_type: str, before_call=None) -> list[list[float]]: ...


class VectorCacheUnavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def unit_vector(values, dimensions):
    if len(values) != dimensions:
        raise ValueError("Invalid vector dimensions")
    if any(isinstance(value, bool) or not isinstance(value, (float, int))
           or not math.isfinite(value) for value in values):
        raise ValueError("Invalid vector values")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("Invalid vector norm")
    return [value / norm for value in values]


class VectorStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.lock = threading.RLock()

    @staticmethod
    def _provider(provider):
        if (not isinstance(provider.identity, str) or not provider.identity
                or not isinstance(provider.dimensions, int) or isinstance(provider.dimensions, bool)
                or not 1 <= provider.dimensions <= 16384):
            raise ValueError("Invalid embedding identity or dimensions")

    @staticmethod
    def _check_database(connection, *, allow_empty=False):
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app_id != VECTOR_APP_ID and not (allow_empty and app_id == 0 and not tables):
            raise VectorCacheUnavailable("vector_cache_invalid")

    def _rows(self, provider):
        self._provider(provider)
        if not self.path.exists():
            raise VectorCacheUnavailable("vector_cache_missing")
        try:
            with self.lock, closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                self._check_database(connection)
                metadata = connection.execute("SELECT dimensions,template FROM builds WHERE identity=?",
                                              (provider.identity,)).fetchone()
                if metadata is None:
                    raise VectorCacheUnavailable("embedding_identity_mismatch")
                if metadata != (provider.dimensions, TEXT_TEMPLATE):
                    raise VectorCacheUnavailable("embedding_identity_mismatch")
                rows = connection.execute("SELECT chunk_id,text_hash,vector,vector_hash FROM vectors "
                                          "WHERE identity=?", (provider.identity,)).fetchall()
                return {row[0]: row[1:] for row in rows}
        except (sqlite3.Error, OSError):
            raise VectorCacheUnavailable("vector_cache_invalid") from None

    @staticmethod
    def _decode(row, dimensions):
        _, payload, checksum = row
        if (not isinstance(payload, bytes) or len(payload) != dimensions * 4
                or hashlib.sha256(payload).hexdigest() != checksum):
            raise VectorCacheUnavailable("vector_cache_invalid")
        try:
            return unit_vector(list(struct.unpack(f"<{dimensions}f", payload)), dimensions)
        except (ValueError, struct.error, OverflowError):
            raise VectorCacheUnavailable("vector_cache_invalid") from None

    def load(self, documents, provider):
        """Require complete current coverage; stale extra rows are never returned."""
        rows = self._rows(provider)
        result = {}
        for document in documents:
            row = rows.get(document.hit.chunk_id)
            if row is None or row[0] != document.content_hash:
                raise VectorCacheUnavailable("vector_cache_incomplete")
            result[document.hit.chunk_id] = self._decode(row, provider.dimensions)
        return result

    def build(self, lexical, provider, *, today=None, batch_size=10, before_call=None):
        """Offline-only operation. Failed encoding leaves every active build unchanged."""
        self._provider(provider)
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
            raise ValueError("Invalid embedding batch size")
        documents = lexical.export_documents(today=today)
        initial = {document.hit.chunk_id: (document.content_hash, document.hit) for document in documents}
        if self.path in (lexical.source_path, lexical.index_path):
            raise ValueError("Vector cache must have an independent path")
        # Reject another database before making any provider call, including partial/empty builds.
        if self.path.exists():
            try:
                with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
                    self._check_database(connection, allow_empty=True)
            except sqlite3.Error:
                raise VectorCacheUnavailable("vector_cache_invalid") from None
        try:
            previous = self._rows(provider)
        except VectorCacheUnavailable as error:
            if error.reason not in ("vector_cache_missing", "embedding_identity_mismatch"):
                raise
            previous = {}
        staged, missing = {}, []
        for document in documents:
            row = previous.get(document.hit.chunk_id)
            if row and row[0] == document.content_hash:
                self._decode(row, provider.dimensions)
                staged[document.hit.chunk_id] = row
            else:
                missing.append(document)
        reused = len(staged)
        calls_before = getattr(provider, "calls", 0)
        tokens_before = getattr(provider, "input_tokens", 0)
        batches = 0
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            values = provider.embed([document.text for document in batch], text_type="document",
                                    before_call=before_call)
            batches += 1
            if len(values) != len(batch):
                raise ValueError("Embedding batch has wrong result count")
            for document, vector in zip(batch, values):
                normalized = unit_vector(vector, provider.dimensions)
                payload = struct.pack(f"<{provider.dimensions}f", *normalized)
                staged[document.hit.chunk_id] = (document.content_hash, payload,
                                               hashlib.sha256(payload).hexdigest())
        current = lexical.export_documents(today=today)
        if initial != {document.hit.chunk_id: (document.content_hash, document.hit) for document in current}:
            raise VectorCacheUnavailable("sources_changed")
        corpus_hash = lexical.snapshot_metadata(current, today=today)["corpus_hash"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock, closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._check_database(connection, allow_empty=True)
                connection.execute(f"PRAGMA application_id={VECTOR_APP_ID}")
                connection.execute("CREATE TABLE IF NOT EXISTS builds (identity TEXT PRIMARY KEY, "
                    "dimensions INTEGER NOT NULL,template TEXT NOT NULL,corpus_hash TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS vectors (identity TEXT NOT NULL, "
                    "chunk_id TEXT NOT NULL,text_hash TEXT NOT NULL,vector BLOB NOT NULL,vector_hash TEXT NOT NULL,"
                    "PRIMARY KEY(identity,chunk_id))")
                connection.execute("DELETE FROM vectors WHERE identity=?", (provider.identity,))
                connection.executemany("INSERT INTO vectors VALUES (?,?,?,?,?)",
                    [(provider.identity, key, *row) for key, row in staged.items()])
                connection.execute("INSERT OR REPLACE INTO builds VALUES (?,?,?,?)",
                    (provider.identity, provider.dimensions, TEXT_TEMPLATE, corpus_hash))
        except (sqlite3.Error, OSError):
            raise VectorCacheUnavailable("vector_cache_invalid") from None
        return {"chunk_count": len(staged), "cache_hits": reused, "embedded_count": len(missing),
                "calls": getattr(provider, "calls", calls_before + batches) - calls_before,
                "input_tokens": getattr(provider, "input_tokens", tokens_before) - tokens_before,
                "identity": provider.identity, "dimensions": provider.dimensions,
                "corpus_hash": corpus_hash, "text_template": TEXT_TEMPLATE}
