"""Bounded lexical + dense retrieval; providers never get private record context."""
import math

from model.factory import ModelError
from rag.rag_service import RetrievalSnapshot
from rag.vector_store import VectorCacheUnavailable, unit_vector


class HybridKnowledgeRetriever:
    def __init__(self, lexical, provider, vector_store, *, candidate_limit=20, rrf_k=60,
                 before_call=None):
        if (not isinstance(candidate_limit, int) or isinstance(candidate_limit, bool)
                or not 1 <= candidate_limit <= 20):
            raise ValueError("Invalid retrieval candidate limit")
        if not isinstance(rrf_k, (int, float)) or isinstance(rrf_k, bool) or not math.isfinite(rrf_k) or rrf_k <= 0:
            raise ValueError("Invalid RRF constant")
        self.lexical = lexical
        self.provider = provider
        self.vector_store = vector_store
        self.candidate_limit = candidate_limit
        self.rrf_k = rrf_k
        self.before_call = before_call

    def for_request(self, before_call):
        """The quota callback belongs to this request, never to the shared provider."""
        return type(self)(self.lexical, self.provider, self.vector_store,
                          candidate_limit=self.candidate_limit, rrf_k=self.rrf_k,
                          before_call=before_call)

    def catalog(self, today=None):
        result = self.lexical.catalog(today=today)
        result["requested_method"] = "hybrid_rrf"
        documents = self.lexical.export_documents(today=today)
        reason = None
        if not documents:
            result.update(vector_status="empty", degraded_reason=None)
            return result
        if self.provider is None:
            reason = "embedding_not_configured"
        else:
            try:
                self.vector_store.load(documents, self.provider)
            except VectorCacheUnavailable as error:
                reason = error.reason
        result.update(vector_status="degraded" if reason else "ready", degraded_reason=reason)
        return result

    def get_chunks(self, chunk_ids, *, topic="all", today=None):
        return self.lexical.get_chunks(chunk_ids, topic=topic, today=today)

    def all_chunks(self, *, topic="all", today=None):
        return self.lexical.all_chunks(topic=topic, today=today)

    def export_documents(self, *, topic="all", today=None):
        return self.lexical.export_documents(topic=topic, today=today)

    def search(self, query, limit=3, *, topic="all", today=None):
        return self.search_snapshot(query, limit, topic=topic, today=today).hits

    def search_snapshot(self, query, limit=3, *, topic="all", today=None):
        if not isinstance(query, str) or len(query) > 4000 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("Invalid knowledge query")
        candidate_limit = max(limit, self.candidate_limit)
        lexical = self.lexical.search_snapshot(query, candidate_limit, topic=topic, today=today)
        metadata = dict(lexical.metadata, requested_mode="hybrid", candidate_limit=candidate_limit,
                        rrf_k=self.rrf_k)
        if self.provider is not None:
            metadata.update(embedding_identity=self.provider.identity, dimensions=self.provider.dimensions)

        def fallback(reason, *, snapshot=lexical):
            current = self.lexical.export_documents(topic=topic, today=today)
            available = {document.hit.chunk_id: document for document in current}
            current_metadata = self.lexical.snapshot_metadata(current, today=today)
            hits = [hit for hit in snapshot.hits
                    if hit.chunk_id in available and hit == available[hit.chunk_id].hit]
            if current_metadata["corpus_hash"] != snapshot.metadata["corpus_hash"]:
                reason = "sources_changed"
            value = dict(metadata, mode="lexical", degraded=reason not in ("empty_corpus", "browse"),
                         reason=reason)
            # Retain all candidate ranks but never a query, vector or provider error body.
            valid_ids = {hit.chunk_id for hit in hits}
            value["rankings"] = [rank for rank in snapshot.metadata["rankings"] if rank["chunk_id"] in valid_ids]
            value.update({key: current_metadata[key] for key in ("corpus_version", "corpus_hash")})
            return RetrievalSnapshot(hits[:limit], value)

        if not query.strip():
            return fallback("browse")
        if self.provider is None:
            return fallback("embedding_not_configured")
        documents = self.lexical.export_documents(topic=topic, today=today)
        if not documents:
            return fallback("empty_corpus")
        if self.lexical.snapshot_metadata(documents, today=today)["corpus_hash"] != metadata["corpus_hash"]:
            latest = self.lexical.search_snapshot(query, candidate_limit, topic=topic, today=today)
            return fallback("sources_changed", snapshot=latest)
        try:
            vectors = self.vector_store.load(documents, self.provider)
        except VectorCacheUnavailable as error:
            return fallback(error.reason)
        try:
            # The provider invokes before_call immediately before its real HTTP request.
            # HTTPException (quota denied) deliberately escapes this fallback handling.
            def authorized_call():
                if self.before_call:
                    self.before_call()
                metadata["embedding_called"] = True
            values = self.provider.embed([query], text_type="query", before_call=authorized_call)
            if len(values) != 1:
                raise ValueError("Invalid query embedding count")
            query_vector = unit_vector(values[0], self.provider.dimensions)
        except ModelError:
            return fallback("embedding_query_failed")
        except (TypeError, ValueError, OverflowError):
            return fallback("embedding_invalid_vector")

        # Sources may be edited or withdrawn while waiting on the provider.
        current = self.lexical.export_documents(topic=topic, today=today)
        before = {document.hit.chunk_id: (document.content_hash, document.hit) for document in documents}
        after = {document.hit.chunk_id: (document.content_hash, document.hit) for document in current}
        if before != after:
            latest = self.lexical.search_snapshot(query, candidate_limit, topic=topic, today=today)
            return fallback("sources_changed", snapshot=latest)
        dense = sorted(vectors, key=lambda key: (
            -math.fsum(left * right for left, right in zip(query_vector, vectors[key])), key))[:candidate_limit]
        lexical_ranks = {hit.chunk_id: rank for rank, hit in enumerate(lexical.hits, 1) if hit.chunk_id in after}
        dense_ranks = {key: rank for rank, key in enumerate(dense, 1)}
        scores = {key: ((1 / (self.rrf_k + lexical_ranks[key]) if key in lexical_ranks else 0)
                        + (1 / (self.rrf_k + dense_ranks[key]) if key in dense_ranks else 0))
                  for key in lexical_ranks.keys() | dense_ranks.keys()}
        order = sorted(scores, key=lambda key: (-scores[key], key))
        current_metadata = self.lexical.snapshot_metadata(current, today=today)
        current_metadata.pop("embedding_called", None)
        metadata.update(current_metadata)
        metadata.update(mode="hybrid", requested_mode="hybrid", degraded=False, reason=None,
            rankings=[{"chunk_id": key, "lexical_rank": lexical_ranks.get(key),
                       "dense_rank": dense_ranks.get(key), "rrf_score": scores[key],
                       "content_hash": after[key][0]} for key in order])
        return RetrievalSnapshot([after[key][1] for key in order[:limit]], metadata)
