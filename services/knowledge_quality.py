"""Fixed, cache-only hybrid acceptance. No cloud calls or client-supplied pass flags."""
import hashlib
import json
import re

from config import ROOT
from rag.hybrid import HybridKnowledgeRetriever
from rag.vector_store import VectorStore, VectorCacheUnavailable
from scripts.evaluate_retrieval import CachedQueries, encode_queries, load_cases
from services.knowledge_feedback import signature

POLICY = "knowledge-release-quality-v1"
PROTOCOL_PATH = ROOT / "evals/release_quality/v1/manifest.json"


def protocol():
    raw = PROTOCOL_PATH.read_bytes()
    data = json.loads(raw)
    if (data["policy"] != POLICY or data["split"] != "test" or data["limit"] != 10
            or data["candidate_limit"] != 20 or data["rrf_k"] != 60
            or data["min_mean_recall6"] != 0.8
            or data["review_status"] not in ("pending_review", "reviewed")):
        raise ValueError("Unknown release quality protocol")
    dataset = (ROOT / data["dataset"]).resolve()
    if not dataset.is_relative_to(ROOT.resolve()) or hashlib.sha256(dataset.read_bytes()).hexdigest() != data["dataset_sha256"]:
        raise ValueError("Frozen quality input changed")
    return data, hashlib.sha256(raw).hexdigest(), dataset


def cache_path(directory, provider, rows):
    metadata = {"identity": provider.identity, "dimensions": provider.dimensions,
        "questions": hashlib.sha256(json.dumps([row["question"] for row in rows], ensure_ascii=False).encode()).hexdigest()}
    key = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()[:24]
    return directory / ("public-queries-" + key + ".json")


def score(ids, relevant, aliases):
    # Preserve actual ranking slots: aliases/duplicates cannot compress later ranks or refill Top 6.
    seen, positions = set(), []
    for rank, key in enumerate(ids, 1):
        canonical = aliases.get(key, key)
        if canonical not in seen and canonical in relevant:
            positions.append(rank)
        seen.add(canonical)
    return {"recall6": sum(rank <= 6 for rank in positions) / len(relevant),
            "mrr10": 1 / positions[0] if positions and positions[0] <= 10 else 0.0}


def evaluate(base, candidate, manifest, cache_directory, today):
    data, protocol_hash, dataset = protocol()
    reviewed = data["review_status"] == "reviewed" and bool(re.fullmatch(r"[0-9a-f]{64}", data.get("label_review_sha256") or ""))
    report = {"policy": POLICY, "protocol_sha256": protocol_hash, "dataset_sha256": data["dataset_sha256"],
        "manifest_sha256": None, "independent_semantic_quality": "reviewed" if reviewed else "not_evaluated",
        "hybrid_ranking_quality": "not_evaluated", "label_review_sha256": data.get("label_review_sha256"),
        "blockers": [] if reviewed else ["independent_semantic_quality"], "passed": False,
        "query_cache_sha256": None, "real_provider_calls": 0, "results": []}
    lexical = getattr(base, "lexical", base)
    if not reviewed:
        return {**report, "blockers": ["independent_semantic_quality", "hybrid_ranking_quality"]}
    documents = lexical.export_documents(today=today)
    rows = [row for row in load_cases(dataset, {doc.hit.chunk_id for doc in documents}) if row["split"] == "test"]
    if not rows or not any(row["answerable"] for row in rows):
        raise ValueError("Quality suite needs answerable held-out cases")
    provider = getattr(base, "provider", None)
    if (provider is None or not isinstance(base, HybridKnowledgeRetriever)
            or base.candidate_limit != data["candidate_limit"] or base.rrf_k != data["rrf_k"]):
        return {**report, "blockers": report["blockers"] + ["hybrid_ranking_quality"]}
    cache = cache_path(cache_directory, provider, rows)
    try:
        if cache.is_symlink() or cache.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Invalid query cache")
        before = hashlib.sha256(cache.read_bytes()).hexdigest()
        vectors, _ = encode_queries(rows, provider, cache_directory, live=False)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {**report, "blockers": report["blockers"] + ["hybrid_ranking_quality"]}
    replay = CachedQueries(provider, dict(zip((row["question"] for row in rows), vectors)))
    old = HybridKnowledgeRetriever(lexical, replay, base.vector_store)
    new = HybridKnowledgeRetriever(candidate, replay, VectorStore(candidate.index_path.with_name("knowledge-vectors.sqlite3")))
    aliases = {item["built_chunk_id"]: item["source_chunk_id"] for item in manifest["selection"]}
    results = []
    for row in rows:
        if not row["answerable"]:
            continue  # Empty qrels cannot grade rejection/answer correctness.
        snapshots = [engine.search_snapshot(row["question"], 10, today=today) for engine in (old, new)]
        grades = [score([hit.chunk_id for hit in snapshot.hits], set(row["relevant_chunk_ids"]), aliases) for snapshot in snapshots]
        passed = all(snapshot.metadata["mode"] == "hybrid" for snapshot in snapshots) and all(
            grades[1][key] >= grades[0][key] for key in grades[0])
        results.append({"id": row["id"], "passed": passed, "baseline": grades[0], "candidate": grades[1]})
    stable = before == hashlib.sha256(cache.read_bytes()).hexdigest()
    mean_recall = sum(item["candidate"]["recall6"] for item in results) / len(results) if results else 0.0
    ranking_passed = stable and bool(results) and mean_recall >= data["min_mean_recall6"] and all(item["passed"] for item in results)
    blockers = report["blockers"] + ([] if ranking_passed else ["hybrid_ranking_quality"])
    return {**report, "results": results, "query_cache_sha256": before, "mean_recall6": mean_recall,
            "hybrid_ranking_quality": "passed" if ranking_passed else "failed", "blockers": blockers, "passed": not blockers}


def current(report, cache_directory, base):
    """Read-time receipt verification only; never rerun rankings or encode a user query."""
    data, protocol_hash, dataset = protocol()
    if (report.get("policy") != POLICY or report.get("passed") is not True or report.get("blockers") != []
            or data["review_status"] != "reviewed" or report.get("protocol_sha256") != protocol_hash
            or report.get("label_review_sha256") != data["label_review_sha256"]):
        return False
    provider = getattr(base, "provider", None)
    if (provider is None or not isinstance(base, HybridKnowledgeRetriever)
            or base.candidate_limit != data["candidate_limit"] or base.rrf_k != data["rrf_k"]
            or not re.fullmatch(r"[0-9a-f]{64}", data.get("label_review_sha256") or "")):
        return False
    rows = [row for row in load_cases(dataset, {doc.hit.chunk_id for doc in base.export_documents()}) if row["split"] == "test"]
    path = cache_path(cache_directory, provider, rows)
    return not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == report.get("query_cache_sha256")
