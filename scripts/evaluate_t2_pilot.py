"""Small public candidate-pool audit; this is NOT the full T2Retrieval benchmark."""
import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT, Settings
from model.embeddings import QwenEmbeddingProvider, EmbeddingError
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import KnowledgeDocument, RetrievalSnapshot, LocalKnowledgeRetriever
from rag.vector_store import VectorStore, unit_vector
from scripts.evaluate_retrieval import CachedQueries, encode_queries, metrics


@dataclass(frozen=True)
class PublicHit:
    chunk_id: str


class CachedPublicBatches:
    """Offline benchmark checkpoints only; never used for private online queries."""
    def __init__(self, provider, directory):
        self.provider, self.directory = provider, directory
        self.identity, self.dimensions = provider.identity, provider.dimensions
        self.directory.mkdir(parents=True, exist_ok=True)
        self.reused_batches = 0

    @property
    def calls(self):
        return self.provider.calls

    @property
    def input_tokens(self):
        return self.provider.input_tokens

    def embed(self, texts, *, text_type, before_call=None):
        manifest = {"identity": self.identity, "dimensions": self.dimensions, "role": text_type,
                    "text_sha256": hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()}
        key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        path = self.directory / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["manifest"] != manifest or len(payload["vectors"]) != len(texts):
                raise ValueError("Public batch cache mismatch")
            if hashlib.sha256(json.dumps(payload["vectors"]).encode()).hexdigest() != payload["checksum"]:
                raise ValueError("Public batch cache corrupt")
            values = [unit_vector(vector, self.dimensions) for vector in payload["vectors"]]
            self.reused_batches += 1
            return values
        # Failure leaves previous successful batches intact; never automatically retry.
        values = self.provider.embed(texts, text_type=text_type, before_call=before_call)
        if len(values) != len(texts):
            raise ValueError("Public batch vector count mismatch")
        values = [unit_vector(vector, self.dimensions) for vector in values]
        payload = {"manifest": manifest, "vectors": values,
                   "checksum": hashlib.sha256(json.dumps(values).encode()).hexdigest()}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
        return values


class PilotLexical:
    """Benchmark-only adapter: same bigram/OR/BM25/two-term rule, no Fitness ingestion."""
    def __init__(self, path, corpus, *, tokenizer="bigram"):
        self.source_path = path.resolve()
        self.index_path = path.with_name("pilot-lexical.sqlite3").resolve()
        # Reuse production text processing without importing public benchmark data
        # into the Fitness knowledge catalog or private records.
        processor = LocalKnowledgeRetriever(self.source_path, self.index_path, tokenizer=tokenizer)
        self.terms, self.tokenizer = processor.terms, tokenizer
        self.documents = [KnowledgeDocument(PublicHit(str(row["id"])), row["text"],
            hashlib.sha256(row["text"].encode()).hexdigest()) for row in corpus]
        self.documents.sort(key=lambda doc: doc.hit.chunk_id)
        self.hits = {doc.hit.chunk_id: doc.hit for doc in self.documents}
        self.corpus_hash = hashlib.sha256(json.dumps([(doc.hit.chunk_id, doc.content_hash)
            for doc in self.documents]).encode()).hexdigest()
        # In-memory FTS cannot overwrite any application database.
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE VIRTUAL TABLE search_index USING fts5(id UNINDEXED, terms)")
        self.connection.executemany("INSERT INTO search_index VALUES (?,?)",
            [(doc.hit.chunk_id, " ".join(self.terms(doc.text))) for doc in self.documents])

    def export_documents(self, **kwargs):
        return self.documents

    def snapshot_metadata(self, documents, **kwargs):
        return {"mode": "lexical", "requested_mode": "lexical", "tokenizer": self.tokenizer,
                "corpus_hash": self.corpus_hash, "corpus_version": "public-t2-pilot",
                "embedding_called": False, "degraded": False}

    def search_snapshot(self, query, limit=20, **kwargs):
        terms = self.terms(query)[:128]
        ranked = []
        if terms:
            match = " OR ".join('"' + term + '"' for term in terms)
            rows = self.connection.execute("SELECT id,terms FROM search_index WHERE search_index MATCH ? "
                                           "ORDER BY bm25(search_index),id", (match,)).fetchall()
            ranked = [key for key, words in rows if len(set(terms) & set(words.split())) >= min(2, len(terms))][:limit]
        metadata = self.snapshot_metadata(self.documents)
        metadata["rankings"] = [{"chunk_id": key, "lexical_rank": i} for i, key in enumerate(ranked, 1)]
        return RetrievalSnapshot([self.hits[key] for key in ranked], metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=ROOT / "artifacts/public-eval/t2/pilot.json")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-calls", type=int, default=120)
    parser.add_argument("--max-input-chars", type=int, default=1500000)
    parser.add_argument("--max-document-chars", type=int, default=6000,
                        help="Common document-prefix limit for every method; recorded in results, never changes original pilot")
    args = parser.parse_args()
    pilot = json.loads(args.pilot.read_text(encoding="utf-8"))
    if not 1 <= args.max_document_chars <= 6000 or args.max_calls < 1 or args.max_input_chars < 1:
        parser.error("Invalid input or request budget")
    corpus = [{**row, "text": row["text"][:args.max_document_chars]} for row in pilot["corpus"]]
    preprocessing = {"max_document_characters": args.max_document_chars,
                     "truncated_documents": sum(len(row["text"]) > args.max_document_chars for row in pilot["corpus"]),
                     "same_text_for_all_methods": True, "relevant_labels_unchanged": True,
                     "limitation": "Some labelled evidence may be outside the common document prefix; this is not a full-text benchmark."}
    lexical = PilotLexical(args.pilot, corpus)
    jieba = PilotLexical(args.pilot, corpus, tokenizer="jieba")
    if len({row["id"] for row in pilot["corpus"]}) != len(pilot["corpus"]):
        parser.error("Duplicate public document IDs")
    if any(len(row["text"].encode("utf-8")) > 24000 for row in corpus + pilot["queries"]):
        parser.error("Public text exceeds the embedding input limit; choose and document a common preprocessing protocol before any paid calls")
    # Larger public batches have a longer offline timeout; online defaults stay unchanged.
    settings = replace(Settings.from_env(), embedding_timeout_seconds=60.0)
    provider = QwenEmbeddingProvider(settings, max_calls=args.max_calls, max_input_chars=args.max_input_chars)
    resumed = CachedPublicBatches(provider, args.pilot.parent / "embedding-batches")
    store = VectorStore(args.pilot.with_name("pilot-vectors.sqlite3"))
    # Use an isolated path and identity; external texts never enter the application's public knowledge.
    if args.live:
        try:
            store.build(lexical, resumed)
        except EmbeddingError as error:
            failure = {"status": "failed", "code": error.code, "phase": "documents",
                       "calls": provider.calls, "input_tokens": provider.input_tokens,
                       "reused_batches": resumed.reused_batches}
            args.pilot.with_name("last-failure.json").write_text(json.dumps(failure), encoding="utf-8")
            print(json.dumps(failure), flush=True)
            raise
    vectors = store.load(lexical.documents, provider)
    rows = [{"question": row["text"]} for row in pilot["queries"]]
    query_vectors, encoding = encode_queries(rows, provider, args.pilot.parent, live=args.live)
    cached = CachedQueries(provider, dict(zip((row["question"] for row in rows), query_vectors)))
    hybrid = HybridKnowledgeRetriever(lexical, cached, store)
    jieba_hybrid = HybridKnowledgeRetriever(jieba, cached, store)
    order = sorted(vectors)
    matrix = np.asarray([vectors[key] for key in order], dtype=np.float32)
    runs = []
    for i, row in enumerate(pilot["queries"]):
        if not set(row["relevant_ids"]) <= set(order):
            raise ValueError("Pilot dropped a labelled positive")
        sparse = lexical.search_snapshot(row["text"], 20)
        words = jieba.search_snapshot(row["text"], 20)
        fused = hybrid.search_snapshot(row["text"], 20)
        jieba_fused = jieba_hybrid.search_snapshot(row["text"], 20)
        if fused.metadata["mode"] != "hybrid" or jieba_fused.metadata["mode"] != "hybrid":
            raise ValueError("Pilot silently degraded")
        scores = matrix @ np.asarray(query_vectors[i], dtype=np.float32)
        dense = [order[k] for k in sorted(range(len(order)), key=lambda k: (-float(scores[k]), order[k]))[:20]]
        for name, ranked in (("A_bigram", [hit.chunk_id for hit in sparse.hits]),
                             ("B_jieba", [hit.chunk_id for hit in words.hits]),
                             ("C_dense", dense), ("D_bigram_rrf", [hit.chunk_id for hit in fused.hits])):
            runs.append({"query_id": row["id"], "method": name, "ranked_ids": ranked,
                         "metrics": metrics(ranked, row["relevant_ids"])})
        runs.append({"query_id": row["id"], "method": "E_jieba_rrf",
                     "ranked_ids": [hit.chunk_id for hit in jieba_fused.hits],
                     "metrics": metrics([hit.chunk_id for hit in jieba_fused.hits], row["relevant_ids"])})
    results = {}
    for method in ("A_bigram", "B_jieba", "C_dense", "D_bigram_rrf", "E_jieba_rrf"):
        values = [row["metrics"] for row in runs if row["method"] == method]
        results[method] = {key: sum(row[key] for row in values) / len(values) for key in values[0]}
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "protocol": "Fixed sampled queries; all labelled positives plus random distractors; reduced candidate pool, NOT official full-corpus MTEB results",
              "sampling": pilot["sampling"], "source": pilot["source"],
              "preprocessing": preprocessing, "indexed_corpus_sha256": lexical.corpus_hash,
              "pilot_sha256": hashlib.sha256(args.pilot.read_bytes()).hexdigest(),
              "queries": len(rows), "corpus": len(order), "model": settings.embedding_model, "model_identity": provider.identity,
              "dimensions": provider.dimensions, "usage": {"calls": provider.calls, "input_tokens": provider.input_tokens},
              "reused_document_batches": resumed.reused_batches,
              "query_encoding": encoding, "metrics": results, "runs": runs}
    destination = args.pilot.with_name("results.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lexical.connection.close()
    jieba.connection.close()
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
