"""Validate an explicitly pinned, inactive regression package. Never rank or encode."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT
from rag.rag_service import KnowledgeUnavailable, LocalKnowledgeRetriever, TEXT_TEMPLATE
from rag.vector_store import VectorCacheUnavailable, VectorStore
from scripts.evaluate_retrieval import encode_queries, load_cases
from services.business_time import business_today
from services.knowledge_quality import cache_path, protocol


POLICY = "fitness-current-corpus-regression-candidate-v1"
QUARANTINE_GROUPS = {"nutrition-balance", "nutrition-allergy"}
FILES = {"original_qa", "original_protocol", "original_corpus", "corpus", "proposed",
         "active", "quarantine", "mapping", "changes", "recheck", "ai_review",
         "owner_review", "owner_session", "prior_ai", "insufficient", "queries", "vectors"}
RUNTIME_FILES = ("app.py", "config.py", "services/knowledge_quality.py", "services/knowledge_runtime.py")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_package(directory, expected_sha256, *, reference_root=ROOT, today=None):
    """Hashes bind declared artifacts; they do not prove human judgment or semantic truth."""
    directory, reference_root = Path(directory).resolve(), Path(reference_root).resolve()
    manifest_path = directory / "manifest.json"
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""), "An explicit manifest SHA-256 is required")
    require(not manifest_path.is_symlink() and sha(manifest_path) == expected_sha256, "Manifest digest mismatch")
    manifest = read_json(manifest_path)
    require(manifest["policy"] == POLICY and manifest["status"] == "inactive_candidate"
            and manifest["review_status"] == "ai_assisted_candidate", "Not an inactive AI-assisted candidate")
    require(all(manifest[key] is False for key in ("activated", "production_eligible", "rankings_run")),
            "Candidate cannot claim activation, production eligibility or completed ranking")
    require(manifest["limits"] == {"original_labels_seen_by_ai": True, "external_or_blinded_review": False,
            "prior_exposure_erased": False, "independent_human_gold": False}, "Review limitations changed")
    require(manifest["metrics"] == {"candidate_limit": 20, "rrf_k": 60, "limit": 10,
            "min_mean_recall6": 0.8, "per_case_no_loss": ["Recall@6", "MRR@10"]}, "Metric policy changed")
    require(set(manifest["files"]) == FILES, "Unexpected package file set")
    paths = {}
    for key, item in manifest["files"].items():
        relative = Path(item["path"])
        path = directory / relative
        require(not relative.is_absolute() and ".." not in relative.parts
                and path.resolve().is_relative_to(directory), "Package path escapes its directory")
        require(not any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
                        for part in (path, *path.parents) if part.is_relative_to(directory)), "Linked package paths are not allowed")
        require(path.is_file() and path.stat().st_size <= 16 * 1024 * 1024
                and sha(path) == item["sha256"], "Package file digest mismatch: " + key)
        paths[key] = path
    require(len(set(path.resolve() for path in paths.values())) == len(paths), "Package assets must be separate files")
    bindings = {"original_qa": "evals/fitness_qa.jsonl", "original_protocol": "evals/release_quality/v1/manifest.json",
                "corpus": "data/knowledge/sources.json"}
    require(all(sha(paths[key]) == sha(reference_root / relative) for key, relative in bindings.items()),
            "Current corpus or frozen v1 binding changed")
    require(set(manifest["runtime_files"]) == set(RUNTIME_FILES)
            and all(sha(reference_root / name) == digest for name, digest in manifest["runtime_files"].items()),
            "Production code binding changed")
    original_protocol = read_json(paths["original_protocol"])
    require(original_protocol["policy"] == "knowledge-release-quality-v1"
            and original_protocol["review_status"] == "pending_review"
            and original_protocol["label_review_sha256"] is None, "Original v1 review state changed")

    with TemporaryDirectory(prefix="fitness-regression-check-") as temporary:
        lexical = LocalKnowledgeRetriever(paths["corpus"], Path(temporary) / "public-index.sqlite3", tokenizer="jieba")
        documents = lexical.export_documents(today=today or business_today())
        valid_ids = {document.hit.chunk_id for document in documents}
        excerpts = {document.hit.chunk_id: document.hit.excerpt for document in documents}
        require(len(documents) == 133, "Current public corpus coverage changed")
        original = load_cases(paths["original_qa"], valid_ids)
        active = load_cases(paths["active"], valid_ids)
        quarantined = load_cases(paths["quarantine"], valid_ids)
        proposed = read_rows(paths["proposed"])
        require(len(original) == len(proposed) == 260 and len(active) == 248 and len(quarantined) == 12,
                "Dataset coverage changed")
        expected_quarantine = [row for row in proposed if row["group_id"] in QUARANTINE_GROUPS]
        require(all(row["split"] == "dev" for row in expected_quarantine)
                and quarantined == expected_quarantine
                and active == [row for row in proposed if row["group_id"] not in QUARANTINE_GROUPS],
                "Quarantine must retain the two complete dev groups without dropping test cases")
        test = [row for row in active if row["split"] == "test"]
        original_test = [row for row in original if row["split"] == "test"]
        identity = lambda row: tuple(row[key] for key in ("id", "question", "split", "group_id"))
        require(len(test) == 87 and [identity(row) for row in original] == [identity(row) for row in proposed]
                and [identity(row) for row in original_test] == [identity(row) for row in test],
                "Question, group, split or test order changed")
        changes = read_json(paths["changes"])["changes"]
        require(len(changes) == 29 and len({row["id"] for row in changes}) == 29, "Expected exactly 29 unique proposals")
        by_change = {row["id"]: row for row in changes}
        changed_ids = set()
        for before, after in zip(original, proposed):
            delta = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            if not delta:
                continue
            changed_ids.add(before["id"])
            require(before["split"] == "test" and delta <= {"answerable", "relevant_chunk_ids", "reference_answer", "evidence_excerpt"},
                    "Undeclared dataset change")
            change = by_change.get(before["id"])
            require(change is not None and all(before[key] == value for key, value in change["frozen_old"].items())
                    and all(after[key] == value for key, value in change["proposed"].items()), "Proposal does not match before/after input")
            require(after["evidence_excerpt"] == " ".join(excerpts[key] for key in after["relevant_chunk_ids"]),
                    "Changed evidence excerpt must match the current approved summaries")
        require(changed_ids == set(by_change), "Missing or extra changes")
        counts = {key: sum(before[key] != after[key] for before, after in zip(original, proposed))
                  for key in ("answerable", "relevant_chunk_ids", "reference_answer")}
        require(counts == {"answerable": 1, "relevant_chunk_ids": 6, "reference_answer": 24}, "Proposal counts changed")
        case037 = next(row for row in test if row["id"] == "fitness-qa-037")
        require(case037["answerable"] is False and case037["relevant_chunk_ids"] == [], "Case037 must remain an insufficiency case")
        mapping = [{"id": row["id"], "original_split": row["split"], "group_id": row["group_id"],
                    "destination": "quarantine" if row["group_id"] in QUARANTINE_GROUPS else "active",
                    "proposal_changed": row["id"] in changed_ids} for row in original]
        require(read_json(paths["mapping"]) == mapping, "Partition mapping is incomplete or changed")
        insufficient = read_json(paths["insufficient"])
        negatives = [row for row in test if not row["answerable"]]
        require(len(negatives) == 16 and insufficient["quality_status"] == "not_evaluated"
                and insufficient["empty_qrels_are_success"] is False
                and insufficient["case_ids"] == [row["id"] for row in negatives], "Insufficient-answer checks must remain ungraded")
        require(len([row for row in test if row["answerable"]]) == 71, "Positive denominator changed")
        recheck_data = read_json(paths["recheck"])
        require(recheck_data["changes_file_sha256"] == sha(paths["changes"])
                and recheck_data["current_corpus_sha256"] == sha(paths["corpus"]), "Recheck version binding changed")
        recheck = recheck_data["items"]
        require(len(recheck) == 29 and {row["id"] for row in recheck} == changed_ids
                and all(row["reviewer_type"] == "ai" and row["original_labels_seen"] is True
                        and row["decision"] == "retain_as_inactive_candidate" for row in recheck), "Recheck cannot claim human approval")
        ai_data = read_json(paths["ai_review"])
        require(ai_data["corpus_file_sha256"] == sha(paths["corpus"])
                and ai_data["frozen_dataset_sha256"] == sha(paths["original_qa"]), "AI audit version binding changed")
        ai_items = ai_data["items"]
        require(len(ai_items) == 87 and {row["id"] for row in ai_items} == {row["id"] for row in test}
                and all(row["reviewer_type"] == "ai" and row["original_labels_seen"] is True for row in ai_items),
                "AI review provenance changed")
        require(changes == [row for row in ai_items if row["proposal_changes"]], "Changes differ from the bound AI audit")
        owner = read_json(paths["owner_review"])
        require(owner["public_source_sha256"] == sha(paths["original_corpus"])
                and owner["dataset_sha256"] == sha(paths["original_qa"])
                and owner["protocol_sha256"] == sha(paths["original_protocol"]), "Original owner record binding changed")
        owner_items = owner["items"]
        owner_sets = {
            "source_support": sorted(row["id"] for row in owner_items if row.get("human_source_support_confirmed")),
            "explicit_boundary": sorted(row["id"] for row in owner_items if row.get("human_product_boundary_confirmed")),
            "conditional_acceptance": sorted(row["id"] for row in owner_items if row.get("conditional_owner_acceptance_applied"))}
        require(owner_sets == manifest["prior_owner_decisions"] and [len(value) for value in owner_sets.values()] == [2, 6, 3],
                "Prior owner decisions cannot be promoted into new human labels")
        current_sources = {row["id"]: row for row in read_json(paths["corpus"])["sources"]}
        old_sources = {row["id"]: row for row in read_json(paths["original_corpus"])["sources"]}
        require(all(current_sources[key] == old_sources[key] for row in owner_items
                    if row.get("human_source_support_confirmed") for key in row["checked_source_ids"]),
                "Prior owner source evidence changed")
        embedding = manifest["embedding"]
        require(embedding["text_template"] == TEXT_TEMPLATE and isinstance(embedding["identity"], str)
                and type(embedding["dimensions"]) is int, "Embedding metadata changed")
        provider = SimpleNamespace(identity=embedding["identity"], dimensions=embedding["dimensions"])
        require(not any(Path(str(paths["vectors"]) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")),
                "Vector cache must be a standalone snapshot")
        vectors = VectorStore(paths["vectors"]).load(documents, provider)
        require(cache_path(paths["queries"].parent, provider, test).resolve() == paths["queries"].resolve(),
                "Query cache name or question binding changed")
        queries, usage = encode_queries(test, provider, paths["queries"].parent, live=False)
        require(usage["calls"] == 0 and len(queries) == 87 and len(vectors) == 133, "Incomplete public caches")
    require(sha(manifest_path) == expected_sha256
            and all(sha(paths[key]) == item["sha256"] for key, item in manifest["files"].items()),
            "Package changed during validation")
    require(all(sha(paths[key]) == sha(reference_root / relative) for key, relative in bindings.items())
            and all(sha(reference_root / name) == digest for name, digest in manifest["runtime_files"].items()),
            "Production reference changed during validation")
    return {"policy": POLICY, "validation_status": "valid_inputs_not_evaluated", "active_cases": 248,
            "dev_cases": 161, "test_cases": 87, "quarantined_dev_cases": 12,
            "answerable_test_cases": 71, "insufficient_test_cases": 16,
            "public_document_vectors": 133, "public_query_vectors": 87,
            "rankings_run": False, "answer_or_refusal_quality": "not_evaluated",
            "provider_requests": 0, "production_eligible": False, "activated": False,
            "notice": "Structural and identity checks only; not independent human review, a fresh holdout, quality scores or release approval."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    if args.package is None:
        if args.expected_sha256:
            parser.error("--expected-sha256 requires --package")
        current, _, _ = protocol()
        result = {"mode": "production_protocol_unchanged", "policy": current["policy"],
                  "review_status": current["review_status"], "candidate_checked": False,
                  "rankings_run": False, "provider_requests": 0}
    else:
        if not args.expected_sha256:
            parser.error("--package requires an explicitly checked --expected-sha256")
        try:
            result = check_package(args.package, args.expected_sha256)
        except (OSError, ValueError, KeyError, TypeError, KnowledgeUnavailable, VectorCacheUnavailable) as error:
            parser.exit(2, "Candidate validation stopped: " + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
