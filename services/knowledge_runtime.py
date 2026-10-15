"""Bind paid query encoding to the authenticated request's quota."""
from services.usage import reserve_call
from fastapi import HTTPException
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import RetrievalSnapshot
from rag.vector_store import VectorStore
from services.knowledge_builds import FrozenKnowledge
from services.knowledge_publications import KnowledgePublications


class PublishedKnowledge:
    """Resolve one immutable package per operation, then reject changed publication leases."""
    def __init__(self, database, base, settings, lock, *, lexical_only=False):
        self.database, self.governance_base, self.settings, self.lock = database, base, settings, lock
        self.lexical_only = lexical_only

    @property
    def lexical(self):
        return type(self)(self.database, self.governance_base, self.settings, self.lock, lexical_only=True)

    @property
    def requires_embedding_preflight(self):
        return hasattr(self.governance_base, "for_request") and not self.lexical_only

    def for_request(self, before_call):
        base = self.governance_base
        if hasattr(base, "for_request"):
            base = base.for_request(before_call)
        return type(self)(self.database, base, self.settings, self.lock, lexical_only=self.lexical_only)

    def _resolve(self):
        base = self.governance_base
        service = KnowledgePublications(self.database, None, base, self.settings, self.lock)
        with self.lock, self.database.connect() as connection:
            head = service.head(connection)
            if head["release_id"]:
                connection.execute("BEGIN IMMEDIATE")
                head = service.head(connection)
                if head["release_id"]:
                    try:
                        _, manifest = service.validate(connection, head["release_id"])
                    except HTTPException as error:
                        if error.status_code != 409:
                            raise
                        head = service.head(connection)
                    else:
                        lexical = FrozenKnowledge(service.builds._child(head["release_id"]), manifest["tokenizer"])
                        selected = lexical
                        if not self.lexical_only and isinstance(base, HybridKnowledgeRetriever):
                            selected = HybridKnowledgeRetriever(lexical, base.provider,
                                VectorStore(lexical.index_path.with_name("knowledge-vectors.sqlite3")),
                                candidate_limit=base.candidate_limit, rrf_k=base.rrf_k, before_call=base.before_call)
                        return selected, head
        return (getattr(base, "lexical", base) if self.lexical_only else base), head

    def valid_snapshot(self, metadata, *, connection=None):
        if "release_revision" not in metadata:
            return True  # Existing answers only cite original public sources.
        if connection is None:
            _, head = self._resolve()
        else:
            head = KnowledgePublications.head(connection)
        return metadata["release_revision"] == head["revision"] and metadata.get("release_id", "") == (head["release_id"] or "")

    def catalog(self, today=None):
        selected, head = self._resolve()
        return {**selected.catalog(today=today), "release_revision": head["revision"], "release_id": head["release_id"]}

    def search(self, query, limit=3, **options):
        return self.search_snapshot(query, limit, **options).hits

    def search_snapshot(self, query, limit=3, **options):
        selected, head = self._resolve()
        snapshot = selected.search_snapshot(query, limit, **options)
        metadata = {**snapshot.metadata, "release_revision": head["revision"], "release_id": head["release_id"] or ""}
        if not self.valid_snapshot(metadata):
            return RetrievalSnapshot([], {**metadata, "reason": "sources_changed", "rankings": []})
        return RetrievalSnapshot(snapshot.hits, metadata)

    def _read(self, method, *args, **options):
        selected, head = self._resolve()
        result = getattr(selected, method)(*args, **options)
        return result if self.valid_snapshot({"release_revision": head["revision"], "release_id": head["release_id"] or ""}) else []

    def get_chunks(self, ids, **options):
        return self._read("get_chunks", ids, **options)

    def all_chunks(self, **options):
        return self._read("all_chunks", **options)

    def export_documents(self, **options):
        return self._read("export_documents", **options)


def request_retriever(request, user):
    retriever = request.app.state.knowledge
    if hasattr(retriever, "for_request"):
        return retriever.for_request(lambda: reserve_call(
            request.app.state.database, request.app.state.settings, user["id"]))
    return retriever
