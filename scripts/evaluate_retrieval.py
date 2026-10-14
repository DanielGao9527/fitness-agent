"""Compare retrieval on source-labelled public Fitness QA. No answer-model calls."""
import argparse
import hashlib
import json
import math
import sys
import time
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT, Settings
from model.embeddings import EmbeddingError, QwenEmbeddingProvider
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import LocalKnowledgeRetriever
from rag.vector_store import VectorStore, unit_vector


def metrics(ranked, relevant):
    # Standard binary-qrels definitions; one query is the unit of aggregation.
    positive = set(relevant)
    if not positive:
        return None
    unique = list(dict.fromkeys(ranked))
    answer = {f"recall@{k}": len(set(unique[:k]) & positive) / len(positive) for k in (3, 6, 10, 20)}
    answer.update({f"hit@{k}": float(bool(set(unique[:k]) & positive)) for k in (3, 6)})
    answer["mrr@10"] = next((1 / rank for rank, key in enumerate(unique[:10], 1) if key in positive), 0.0)
    dcg = sum(1 / math.log2(rank + 1) for rank, key in enumerate(unique[:10], 1) if key in positive)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(positive)) + 1))
    answer["ndcg@10"] = dcg / ideal
    return answer


def load_cases(path, valid_ids):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("QA dataset is empty")
    ids, questions, groups = set(), set(), {}
    evidence_splits = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("QA records must be objects")
        if (not isinstance(row.get("id"), str) or not row["id"].strip() or row["id"] in ids
                or not isinstance(row.get("question"), str) or not row["question"].strip()
                or row["question"] in questions or row.get("split") not in ("dev", "test")
                or not isinstance(row.get("group_id"), str) or not row["group_id"].strip() or type(row.get("answerable")) is not bool
                or not isinstance(row.get("relevant_chunk_ids"), list)
                or any(not isinstance(key, str) for key in row["relevant_chunk_ids"])
                or len(set(row["relevant_chunk_ids"])) != len(row["relevant_chunk_ids"])
                or bool(row["relevant_chunk_ids"]) != row["answerable"]
                or not set(row["relevant_chunk_ids"]) <= valid_ids):
            raise ValueError("Invalid QA IDs, split, evidence or duplicate question")
        if groups.setdefault(row["group_id"], row["split"]) != row["split"]:
            raise ValueError("Question group leaks between dev and test")
        for key in row["relevant_chunk_ids"]:
            if evidence_splits.setdefault(key, row["split"]) != row["split"]:
                raise ValueError("Evidence block leaks between dev and test")
        normalized = "".join(c for c in unicodedata.normalize("NFKC", row["question"]).casefold()
                             if not c.isspace() and not unicodedata.category(c).startswith("P"))
        if normalized in questions:
            raise ValueError("Duplicate normalized question")
        ids.add(row["id"])
        questions.add(normalized)
    return rows


class CachedQueries:
    """Real provider vectors replayed for fair ranking timing, never mock semantic data."""
    def __init__(self, provider, values):
        self.identity, self.dimensions = provider.identity, provider.dimensions
        self.values = values

    def embed(self, texts, *, text_type, before_call=None):
        if text_type != "query":
            raise ValueError("Query replay cannot build documents")
        return [self.values[text] for text in texts]


def encode_queries(rows, provider, output, *, live):
    manifest = {"identity": provider.identity, "dimensions": provider.dimensions,
                "questions": hashlib.sha256(json.dumps([r["question"] for r in rows], ensure_ascii=False).encode()).hexdigest()}
    key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:24]
    path = output / f"public-queries-{key}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached["manifest"] != manifest or len(cached["vectors"]) != len(rows):
            raise ValueError("Invalid query cache")
        vectors = [unit_vector(v, provider.dimensions) for v in cached["vectors"]]
        return vectors, {"cache_hit": True, "calls": 0, "input_tokens": 0, "seconds": 0}
    if not live:
        raise ValueError("Query vectors are missing; use --live for bounded cloud encoding")
    started, calls, tokens = time.perf_counter(), provider.calls, provider.input_tokens
    vectors = provider.embed([r["question"] for r in rows], text_type="query")
    if len(vectors) != len(rows):
        raise ValueError("Invalid query embedding count")
    vectors = [unit_vector(v, provider.dimensions) for v in vectors]
    payload = {"manifest": manifest, "vectors": vectors}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return vectors, {"cache_hit": False, "calls": provider.calls - calls, "input_tokens": provider.input_tokens - tokens,
                     "seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa", type=Path, default=ROOT / "evals/fitness_qa.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/retrieval-eval")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--model", default=None)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--lexical-only", action="store_true")
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--max-input-chars", type=int, default=500000)
    args = parser.parse_args()
    if args.max_calls < 1 or args.max_input_chars < 1:
        parser.error("Positive call and input budgets are required")
    args.output.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    if args.model:
        settings = replace(settings, embedding_model=args.model)
    # This script never initializes Settings.database_path or reads personal records.
    lexical = {name: LocalKnowledgeRetriever(settings.knowledge_source_path,
                    args.output / f"lexical-{name}.sqlite3", tokenizer=name) for name in ("bigram", "jieba")}
    documents = lexical["bigram"].export_documents(today=args.date)
    rows = load_cases(args.qa, {doc.hit.chunk_id for doc in documents})
    rows = [row for row in rows if args.split == "all" or row["split"] == args.split]
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "fixed_date": args.date.isoformat(),
              "dataset": str(args.qa.resolve()), "dataset_sha256": hashlib.sha256(args.qa.read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256(settings.knowledge_source_path.read_bytes()).hexdigest(),
              "corpus_count": len(documents), "query_count": len(rows), "split": args.split,
              "protocol": {"candidate_limit": 20, "rrf_k": 60, "rrf_weights": [1, 1],
                           "fields": "title-excerpt-keywords-v1", "top_k_in_app": 6},
              "origin": "AI-authored source-grounded project cases; not an external benchmark",
              "model": None, "groups": {}, "runs": [], "usage": {"calls": 0, "input_tokens": 0}}
    methods = {"A_bigram": lexical["bigram"], "B_jieba": lexical["jieba"]}
    query_vectors = dense_order = None
    if not args.lexical_only:
        provider = QwenEmbeddingProvider(settings, max_calls=args.max_calls, max_input_chars=args.max_input_chars)
        store = VectorStore(args.output / "document-vectors.sqlite3")
        if args.live:
            report["index_build"] = store.build(lexical["bigram"], provider, today=args.date)
        vectors = store.load(documents, provider)
        query_vectors, report["query_encoding"] = encode_queries(rows, provider, args.output, live=args.live)
        cached = CachedQueries(provider, dict(zip((r["question"] for r in rows), query_vectors)))
        methods["D_bigram_rrf"] = HybridKnowledgeRetriever(lexical["bigram"], cached, store)
        methods["E_jieba_rrf"] = HybridKnowledgeRetriever(lexical["jieba"], cached, store)
        report["model"] = {"name": settings.embedding_model, "dimensions": provider.dimensions, "identity": provider.identity}
        report["usage"] = {"calls": provider.calls, "input_tokens": provider.input_tokens, "input_chars": provider.input_chars}
        dense_order = sorted(vectors)
        matrix = np.asarray([vectors[key] for key in dense_order], dtype=np.float32)
    for offset, row in enumerate(rows):
        for name in [*methods, *(["C_dense"] if query_vectors is not None else [])]:
            started = time.perf_counter()
            if name == "C_dense":
                scores = matrix @ np.asarray(query_vectors[offset], dtype=np.float32)
                order = sorted(range(len(scores)), key=lambda i: (-float(scores[i]), dense_order[i]))[:20]
                ranked = [dense_order[i] for i in order]
            else:
                snapshot = methods[name].search_snapshot(row["question"], limit=20, today=args.date)
                if name in ("D_bigram_rrf", "E_jieba_rrf") and snapshot.metadata.get("mode") != "hybrid":
                    raise ValueError("Hybrid run unexpectedly degraded; results must not be labelled hybrid")
                ranked = [hit.chunk_id for hit in snapshot.hits]
            record = {"query_id": row["id"], "split": row["split"], "group_id": row["group_id"], "category": row.get("category"),
                      "method": name, "ranked_ids": ranked, "metrics": metrics(ranked, row["relevant_chunk_ids"]),
                      "cached_retrieval_ms": (time.perf_counter() - started) * 1000}
            report["runs"].append(record)
    for split in ("dev", "test"):
        report["groups"][split] = {}
        for name in sorted({run["method"] for run in report["runs"]}):
            runs = [r for r in report["runs"] if r["split"] == split and r["method"] == name]
            scored = [r for r in runs if r["metrics"] is not None]
            if not scored:
                continue
            report["groups"][split][name] = {
                "answerable_queries": len(scored), "no_answer_queries": len(runs) - len(scored),
                **{key: sum(r["metrics"][key] for r in scored) / len(scored) for key in scored[0]["metrics"]},
                "cached_retrieval_p50_ms": float(np.percentile([r["cached_retrieval_ms"] for r in runs], 50)),
                "cached_retrieval_p95_ms": float(np.percentile([r["cached_retrieval_ms"] for r in runs], 95))}
    label = settings.embedding_model if not args.lexical_only else "lexical"
    destination = args.output / f"results-{label}-{args.split}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(destination.resolve()), "usage": report["usage"], "groups": report["groups"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except EmbeddingError as error:
        print(json.dumps({"status": "failed", "code": error.code}))
        sys.exit(1)
