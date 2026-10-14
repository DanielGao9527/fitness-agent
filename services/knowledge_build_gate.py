"""Frozen known-development lexical regression; not semantic release approval."""
import hashlib
import json

from config import ROOT
from rag.catalog import Corpus
from scripts.evaluate_retrieval import load_cases

POLICY = "knowledge-build-dev-lexical-v1"
DATASET_HASH = "ec6f7446a9560654a1056f8cdda9e1bbef22041c3c4fa043169f965795e75cec"
PROTOCOL_PATH = ROOT / "evals/release_gate/v1/manifest.json"


def regression(baseline, candidate, aliases, today):
    protocol_bytes = PROTOCOL_PATH.read_bytes()
    protocol = json.loads(protocol_bytes)
    if (protocol["policy"] != POLICY or protocol["dataset"] != "evals/fitness_qa.jsonl"
            or protocol["dataset_sha256"] != DATASET_HASH or protocol["split"] != "dev" or protocol["limit"] != 6):
        raise ValueError("Unknown build regression protocol")
    dataset = ROOT / protocol["dataset"]
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != DATASET_HASH:
        raise ValueError("Frozen development input changed")
    reference = Corpus.model_validate_json((ROOT / "data/knowledge/sources.json").read_bytes())
    known = {f"{source.id}:{section.id}" for source in reference.sources for section in source.sections}
    cases = [row for row in load_cases(dataset, known) if row["split"] == "dev"]
    before = {doc.hit.chunk_id: doc for doc in baseline.export_documents(today=today)}
    after = {doc.hit.chunk_id: doc for doc in candidate.export_documents(today=today)}
    preserved = all(after.get(key) == document for key, document in before.items())
    results = []
    for row in cases:
        old = [hit.chunk_id for hit in baseline.search(row["question"], limit=6, today=today)]
        hits = [hit.chunk_id for hit in candidate.search(row["question"], limit=6, today=today)]
        new = list(dict.fromkeys(aliases.get(key, key) for key in hits))
        expected = set(row["relevant_chunk_ids"])
        if row["answerable"] and not expected <= before.keys():
            outcome = "not_comparable"
        else:
            success = (set(old) & expected) <= (set(new) & expected) if expected else bool(old) or not hits
            outcome = "passed" if success else "regression"
        results.append({"id": row["id"], "outcome": outcome, "baseline_ids": old, "candidate_ids": hits})
    comparable = sum(row["outcome"] != "not_comparable" for row in results)
    failed = sum(row["outcome"] == "regression" for row in results)
    return {"policy": POLICY, "dataset_sha256": DATASET_HASH,
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(), "split": "dev", "limit": 6,
        "cases": len(results), "comparable": comparable, "not_comparable": len(results)-comparable,
        "regressions": failed, "original_documents_preserved": preserved,
        "passed": preserved and comparable > 0 and failed == 0, "results": results,
        "scope": "known_development_lexical_regression", "independent_semantic_quality": "not_evaluated",
        "hybrid_ranking_quality": "not_evaluated", "real_provider_calls": 0, "publication_allowed": False}
