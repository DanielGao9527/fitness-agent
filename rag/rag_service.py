import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from datetime import date
from services.business_time import business_today
from pathlib import Path
from typing import Protocol

from rag.catalog import Corpus
from schemas import KnowledgeHit


class KnowledgeRetriever(Protocol):
    def search(self, query: str, limit: int = 3, *, topic="all", today=None) -> list[KnowledgeHit]: ...

    def get_chunks(self, chunk_ids, *, topic="all", today=None) -> list[KnowledgeHit]: ...


class EmptyKnowledgeRetriever:
    def search(self, query: str, limit: int = 3, *, topic="all", today=None) -> list[KnowledgeHit]:
        return []

    def get_chunks(self, chunk_ids, *, topic="all", today=None) -> list[KnowledgeHit]:
        return []


class KnowledgeUnavailable(RuntimeError):
    pass


STOP_WORDS = {"什么", "怎么", "如何", "可以", "需要", "多少", "请问", "今天", "我的", "一下", "这个", "进行", "应该", "是否", "the", "a", "is", "of", "and"}
INDEX_APP_ID = 0x464B4231


def search_terms(text):
    text = unicodedata.normalize("NFKC", text).lower()
    terms = []
    for word in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text):
        parts = [word] if word.isascii() else [word[i:i+2] for i in range(len(word)-1)]
        terms.extend(part for part in parts if part not in STOP_WORDS)
    return list(dict.fromkeys(terms))


class LocalKnowledgeRetriever:
    """Reviewed knowledge summaries only, never personal records or search queries."""

    def __init__(self, source_path: Path, index_path: Path):
        self.source_path = source_path.resolve()
        self.index_path = index_path.resolve()
        self.lock = threading.RLock()

    def sync(self):
        with self.lock:
            return self._sync()

    def _sync(self):
        try:
            if self.source_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Corpus too large")
            raw = self.source_path.read_bytes()
            corpus = Corpus.model_validate_json(raw)
            digest = hashlib.sha256(b"fts5-cjk-bigram-v1\0" + raw).hexdigest()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock, closing(sqlite3.connect(self.index_path, timeout=10)) as connection, connection:
                app_id = connection.execute("PRAGMA application_id").fetchone()[0]
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if app_id != INDEX_APP_ID and (app_id != 0 or tables):
                    raise ValueError("Not a knowledge index; do not overwrite")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={INDEX_APP_ID}")
                connection.execute("CREATE TABLE IF NOT EXISTS catalog (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, payload TEXT NOT NULL, topic TEXT NOT NULL, due TEXT NOT NULL)")
                connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(id UNINDEXED, terms)")
                previous = connection.execute("SELECT value FROM catalog WHERE key='digest'").fetchone()
                if previous and previous[0] == digest:
                    return corpus
                connection.execute("DELETE FROM search_index")
                connection.execute("DELETE FROM chunks")
                for source in corpus.sources:
                    if source.status != "approved":
                        continue
                    for section in source.sections:
                        hit = KnowledgeHit(source_id=source.id, chunk_id=f"{source.id}:{section.id}",
                            title=section.title, locator=section.locator, excerpt=section.summary,
                            **source.model_dump(mode="json", exclude={"id", "title", "sections", "status"}))
                        words = search_terms(" ".join([section.title, section.summary, *section.keywords]))
                        connection.execute("INSERT INTO chunks VALUES (?,?,?,?)", (hit.chunk_id, hit.model_dump_json(), source.topic, source.review_due.isoformat()))
                        connection.execute("INSERT INTO search_index VALUES (?,?)", (hit.chunk_id, " ".join(words)))
                connection.execute("INSERT OR REPLACE INTO catalog VALUES ('digest',?)", (digest,))
                connection.execute("INSERT OR REPLACE INTO catalog VALUES ('version',?)", (corpus.version,))
                return corpus
        except (OSError, ValueError, sqlite3.Error):
            raise KnowledgeUnavailable("知识资料暂不可用，请检查资料文件与索引；未使用旧资料代替。") from None

    def catalog(self, today=None):
        today = today or business_today()
        corpus = self.sync()
        sources = [source for source in corpus.sources if source.status == "approved" and source.reviewed_on <= today <= source.review_due]
        return {"status": "ready" if sources else "empty", "version": corpus.version, "method": "fts5_bm25",
                "source_count": len(sources), "chunk_count": sum(len(source.sections) for source in sources),
                "unavailable_source_count": len(corpus.sources)-len(sources),
                "sources": [source.model_dump(mode="json", exclude={"sections"}) for source in sources]}

    def search(self, query: str, limit: int = 3, *, topic="all", today=None) -> list[KnowledgeHit]:
        if len(query) > 4000 or not 1 <= limit <= 20 or topic not in ("all", "nutrition", "training"):
            raise ValueError("Invalid knowledge query")
        today = (today or business_today()).isoformat()
        with self.lock:
            self.sync()
            try:
                with closing(sqlite3.connect(self.index_path.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    condition = "c.due>=? AND json_extract(c.payload,'$.reviewed_on')<=? AND (?='all' OR c.topic=?)"
                    terms = search_terms(query)[:128]
                    if query.strip() and not terms:
                        return []
                    if not query.strip():
                        rows = connection.execute(f"SELECT c.payload, '' FROM chunks c WHERE {condition} ORDER BY c.id LIMIT ?", (today, today, topic, topic, limit)).fetchall()
                    else:
                        match = " OR ".join('"' + term + '"' for term in terms)
                        rows = connection.execute(f"SELECT c.payload,s.terms FROM search_index s JOIN chunks c ON c.id=s.id WHERE search_index MATCH ? AND {condition} ORDER BY bm25(search_index),c.id", (match,today,today,topic,topic)).fetchall()
                        minimum = min(2, len(terms))
                        rows = [row for row in rows if len(set(terms) & set(row[1].split())) >= minimum][:limit]
                    hits = [KnowledgeHit.model_validate_json(row[0]) for row in rows]
                    return [hit for hit in hits if hit.reviewed_on <= today]
            except (sqlite3.Error, ValueError):
                raise KnowledgeUnavailable("知识索引读取失败，未生成回答。") from None

    def get_chunks(self, chunk_ids, *, topic="all", today=None) -> list[KnowledgeHit]:
        """Read required evidence by stable ID, independently of search ranking or limits."""
        if (not isinstance(chunk_ids, (list, tuple)) or len(chunk_ids) > 20
                or topic not in ("all", "nutrition", "training")
                or any(not isinstance(key, str) or not re.fullmatch(
                    r"[a-z0-9][a-z0-9-]{0,79}:[a-z0-9][a-z0-9-]{0,79}", key) for key in chunk_ids)
                or len(set(chunk_ids)) != len(chunk_ids)):
            raise ValueError("Invalid knowledge IDs")
        if not chunk_ids:
            return []
        today = (today or business_today()).isoformat()
        with self.lock:
            self.sync()
            try:
                with closing(sqlite3.connect(self.index_path.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    placeholders = ",".join("?" for _ in chunk_ids)
                    rows = connection.execute(
                        f"SELECT payload FROM chunks WHERE id IN ({placeholders}) AND due>=? "
                        "AND json_extract(payload,'$.reviewed_on')<=? AND (?='all' OR topic=?)",
                        (*chunk_ids, today, today, topic, topic)).fetchall()
                    hits = [KnowledgeHit.model_validate_json(row[0]) for row in rows]
                    found = {hit.chunk_id: hit for hit in hits}
                    return [found[key] for key in chunk_ids if key in found]
            except (sqlite3.Error, ValueError):
                raise KnowledgeUnavailable("知识索引读取失败，未生成回答。") from None
