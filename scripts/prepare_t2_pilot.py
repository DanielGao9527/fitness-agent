"""Prepare a pinned, reduced-pool public retrieval smoke test; never call a model.

Optional preparation dependency (not an application runtime requirement):
    python -m pip install --only-binary=:all: --no-deps pyarrow==23.0.1

Run from any working directory:
    python scripts/prepare_t2_pilot.py
    python scripts/prepare_t2_pilot.py --offline

This is not an official full-corpus MTEB/T2Retrieval evaluation. All positives
for sampled queries are retained, which makes the candidate pool easier.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DATASET = "mteb/T2Retrieval"
REVISION = "921dd3af6e78d1ae7ee0368aa8d7eaee02c8f08e"
FILES = {
    "corpus": "corpus/dev-00000-of-00001.parquet",
    "queries": "queries/dev-00000-of-00001.parquet",
    "qrels": "data/dev-00000-of-00001.parquet",
}
EXPECTED_SHA256 = {
    "corpus": "8f0400530c6ba664e580e2ae7bb5ed41a908b8abc17c3b3934f42cc65992ac1e",
    "queries": "186b4d0a13cc4d8339eccfc6d465d1b2d4d1f6572defa8d39f3c8da3b66b0210",
    "qrels": "c4c6ffd715faf151e0832a26dfcf2d7ec1edc17d9620a9aa99e721cc2436d7d2",
}


def file_info(directory: Path, name: str, *, offline: bool) -> dict:
    relative = FILES[name]
    path = directory / relative
    url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{relative}?download=true&revision_check=20260924"
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"Missing pinned public data file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".parquet.partial")
        request = urllib.request.Request(url, headers={"User-Agent": "fitness-public-retrieval-eval/1.0"})
        deadline = time.monotonic() + 300
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
            read_available = getattr(response, "read1", response.read)
            while block := read_available(64 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Public data download exceeded 300 seconds: {relative}")
                output.write(block)
        temporary.replace(path)
    content = path.read_bytes()
    if content[:4] != b"PAR1" or content[-4:] != b"PAR1":
        raise ValueError(f"Not a complete Parquet file: {path}")
    digest = hashlib.sha256(content).hexdigest()
    expected = EXPECTED_SHA256.get(name)
    if expected is not None and digest != expected:
        raise ValueError(f"Public data SHA-256 mismatch: {relative}")
    return {"path": relative, "url": url, "bytes": len(content), "sha256": digest}


def load_text_rows(rows: list[dict], *, include_title: bool) -> dict[str, str]:
    result = {}
    for row in rows:
        key = str(row["_id"])
        text = row["text"]
        if not isinstance(text, str):
            raise ValueError(f"Non-text row: {key}")
        title = row.get("title") if include_title else None
        if title is not None and not isinstance(title, str):
            raise ValueError(f"Non-text title: {key}")
        joined = "\n".join(part for part in (title, text) if part)
        if key in result or not joined.strip():
            raise ValueError(f"Duplicate ID or blank text: {key}")
        result[key] = joined
    return result


def prepare(directory: Path, *, seed: int, query_count: int, corpus_size: int, offline: bool) -> dict:
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise SystemExit("Install optional preparation dependency: pyarrow==23.0.1") from exc
    if query_count < 1 or corpus_size < 1:
        raise ValueError("query_count and corpus_size must be positive")
    files = {name: file_info(directory, name, offline=offline) for name in FILES}
    tables = {name: parquet.read_table(directory / FILES[name]) for name in FILES}
    corpus = load_text_rows(tables["corpus"].to_pylist(), include_title=True)
    queries = load_text_rows(tables["queries"].to_pylist(), include_title=False)
    relevant = defaultdict(set)
    for row in tables["qrels"].to_pylist():
        query_id, document_id = str(row["query-id"]), str(row["corpus-id"])
        if query_id not in queries or document_id not in corpus:
            raise ValueError("A qrel refers to an unknown query or corpus ID")
        if row["score"] > 0:
            relevant[query_id].add(document_id)
    eligible = sorted(query_id for query_id in queries if relevant[query_id])
    if query_count > len(eligible):
        raise ValueError("Requested more queries than eligible positive-labelled queries")
    rng = random.Random(seed)
    selected_queries = sorted(rng.sample(eligible, query_count))
    positive_ids = {document_id for query_id in selected_queries for document_id in relevant[query_id]}
    distractor_pool = sorted(set(corpus) - positive_ids)
    distractor_count = min(max(corpus_size - len(positive_ids), 0), len(distractor_pool))
    selected_documents = sorted(positive_ids | set(rng.sample(distractor_pool, distractor_count)))
    selected_lengths = {key: len(corpus[key].encode("utf-8")) for key in selected_documents}
    result = {
        "source": {
            "dataset": DATASET,
            "url": f"https://huggingface.co/datasets/{DATASET}/tree/{REVISION}",
            "revision": REVISION,
            "split": "dev",
            "license_in_dataset_card": "apache-2.0",
            "files": files,
            "reader": f"pyarrow=={pyarrow.__version__}",
            "original_counts": {name: tables[name].num_rows for name in FILES},
        },
        "sampling": {
            "purpose": "public_smoke_test_reduced_candidate_pool_not_official_full_corpus_score",
            "seed": seed,
            "query_count_requested": query_count,
            "corpus_size_requested": corpus_size,
            "eligible_queries": len(eligible),
            "selected_queries": len(selected_queries),
            "positive_documents": len(positive_ids),
            "random_distractors": distractor_count,
            "selected_documents": len(selected_documents),
            "selected_document_utf8_bytes_total": sum(selected_lengths.values()),
            "selected_document_utf8_bytes_max": max(selected_lengths.values(), default=0),
            "selected_document_characters_total": sum(len(corpus[key]) for key in selected_documents),
            "selected_query_utf8_bytes_total": sum(len(queries[key].encode("utf-8")) for key in selected_queries),
            "selected_query_characters_total": sum(len(queries[key]) for key in selected_queries),
            "selected_documents_over_24000_utf8_bytes": [key for key, size in selected_lengths.items() if size > 24000],
            "protocol": [
                "Sort eligible query IDs lexicographically as strings; eligibility requires at least one qrel with score > 0.",
                "Use Python random.Random(seed).sample without replacement to select queries.",
                "Retain the union of ALL score > 0 relevant document IDs for selected queries.",
                "Using the same RNG state, sample without replacement from lexicographically sorted remaining corpus IDs to reach the requested pool size.",
                "If positives exceed the requested pool size, retain all positives; never truncate relevant documents.",
                "Sort output IDs lexicographically; join nonempty title and text with a newline; do not normalize or shorten public text.",
                "Do not examine retrieval scores to select questions, positives or distractors.",
            ],
            "limitations": [
                "The pool is smaller than the official corpus and forced to contain every selected positive; retrieval is easier.",
                "Public qrels are used as provided and may not exhaust all relevant documents.",
                "The 100-query smoke test does not establish an official MTEB score or Fitness-domain improvement.",
            ],
        },
        "corpus": [{"id": key, "text": corpus[key]} for key in selected_documents],
        "queries": [{"id": key, "text": queries[key], "relevant_ids": sorted(relevant[key])} for key in selected_queries],
    }
    assert all(set(item["relevant_ids"]).issubset(selected_documents) for item in result["queries"])
    directory.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = directory / "pilot.json.partial"
    temporary.write_bytes(payload)
    temporary.replace(directory / "pilot.json")
    return {"output": str(directory / "pilot.json"), "sha256": hashlib.sha256(payload).hexdigest(), **result["sampling"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/public-eval/t2")
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--query-count", type=int, default=100)
    parser.add_argument("--corpus-size", type=int, default=1000)
    parser.add_argument("--offline", action="store_true", help="Require downloaded fixed-revision files; prohibit network access.")
    args = parser.parse_args()
    report = prepare(args.output_dir, seed=args.seed, query_count=args.query_count, corpus_size=args.corpus_size, offline=args.offline)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
