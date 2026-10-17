"""Synthetic package contracts only; these are never semantic quality scores."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace

import httpx
import pytest

from config import ROOT, Settings
from rag.rag_service import LocalKnowledgeRetriever, TEXT_TEMPLATE
from rag.vector_store import VectorCacheUnavailable, VectorStore
from scripts import check_regression_candidate as checker
from services.knowledge_quality import cache_path


DAY = date(2026, 10, 18)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def save_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def seal(package):
    manifest = checker.read_json(package / "manifest.json")
    for item in manifest["files"].values():
        item["sha256"] = checker.sha(package / item["path"])
    save(package / "manifest.json", manifest)
    return checker.sha(package / "manifest.json")


@pytest.fixture
def package(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Package checking must not construct a network client or read model credentials")
    monkeypatch.setattr(httpx, "Client", no_network)
    monkeypatch.setattr(Settings, "from_env", no_network)
    reference, bundle = tmp_path / "reference", tmp_path / "candidate"
    bundle.mkdir()
    for relative in (*checker.RUNTIME_FILES, "evals/fitness_qa.jsonl", "evals/release_quality/v1/manifest.json"):
        target = reference / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    # Immutable public historical corpus keeps fixtures independent of future runtime edits.
    corpus_path = ROOT / "evals/corpora/reviewed-training-notes-2026-09-23.2.json"
    target = reference / "data/knowledge/sources.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(corpus_path, target)
    names = {name: name + ".json" for name in checker.FILES}
    names.update(original_qa="original.jsonl", proposed="proposed.jsonl", active="active.jsonl",
                 quarantine="quarantine.jsonl", vectors="document-vectors.sqlite3")
    shutil.copyfile(reference / "evals/fitness_qa.jsonl", bundle / names["original_qa"])
    shutil.copyfile(reference / "evals/release_quality/v1/manifest.json", bundle / names["original_protocol"])
    shutil.copyfile(corpus_path, bundle / names["original_corpus"])
    shutil.copyfile(corpus_path, bundle / names["corpus"])
    original = checker.read_rows(bundle / names["original_qa"])
    proposed = deepcopy(original)
    changed_source_ids = {33, 34, 37, 38, 39, 40, 41, 42, 163, 164, 171, 172, 175, 176,
                          201, 202, 209, 210, 215, 216, 217, 218, 225, 226}
    additions = {94: ["phe-eatwell:balance"], 95: ["phe-eatwell:fruit"], 96: ["phe-eatwell:balance"],
                 115: ["fda-allergy-label:ingredients", "fda-allergy-label:cross-contact"],
                 116: ["fda-allergy-label:ingredients"]}
    corpus = checker.read_json(bundle / names["corpus"])
    excerpts = {source["id"] + ":" + section["id"]: section["summary"]
                for source in corpus["sources"] for section in source["sections"]}
    audit = []
    for before, after in zip(original, proposed):
        number = int(after["id"].split("-")[-1])
        if number in changed_source_ids:
            after["reference_answer"] = "Synthetic reference clarification " + str(number)
        after["relevant_chunk_ids"].extend(additions.get(number, []))
        if number == 37:
            after.update(answerable=False, relevant_chunk_ids=[])
        delta = [key for key in ("answerable", "relevant_chunk_ids", "reference_answer") if before[key] != after[key]]
        if delta:
            after["evidence_excerpt"] = " ".join(excerpts[key] for key in after["relevant_chunk_ids"])
        if after["split"] == "test":
            audit.append({"id": after["id"], "reviewer_type": "ai", "original_labels_seen": True,
                          "proposal_changes": delta,
                          "frozen_old": {key: before[key] for key in ("answerable", "relevant_chunk_ids", "reference_answer", "evidence_excerpt")},
                          "proposed": {key: after[key] for key in ("answerable", "relevant_chunk_ids", "reference_answer")}})
    changes = [row for row in audit if row["proposal_changes"]]
    active = [row for row in proposed if row["group_id"] not in checker.QUARANTINE_GROUPS]
    quarantined = [row for row in proposed if row["group_id"] in checker.QUARANTINE_GROUPS]
    for key, rows in (("proposed", proposed), ("active", active), ("quarantine", quarantined)):
        save_rows(bundle / names[key], rows)
    changed_ids = {row["id"] for row in changes}
    save(bundle / names["mapping"], [{"id": row["id"], "original_split": row["split"], "group_id": row["group_id"],
        "destination": "quarantine" if row["group_id"] in checker.QUARANTINE_GROUPS else "active",
        "proposal_changed": row["id"] in changed_ids} for row in original])
    save(bundle / names["changes"], {"changes": changes})
    save(bundle / names["recheck"], {"changes_file_sha256": checker.sha(bundle / names["changes"]),
        "current_corpus_sha256": checker.sha(bundle / names["corpus"]),
        "items": [{"id": row["id"], "reviewer_type": "ai", "original_labels_seen": True,
                   "decision": "retain_as_inactive_candidate"} for row in changes]})
    save(bundle / names["ai_review"], {"items": audit, "corpus_file_sha256": checker.sha(bundle / names["corpus"]),
                                      "frozen_dataset_sha256": checker.sha(bundle / names["original_qa"])})
    owner_sets = {"source_support": ["fitness-qa-013", "fitness-qa-014"],
        "explicit_boundary": ["fitness-qa-115", "fitness-qa-116", "fitness-qa-246", "fitness-qa-251", "fitness-qa-256", "fitness-qa-260"],
        "conditional_acceptance": ["fitness-qa-242", "fitness-qa-243", "fitness-qa-244"]}
    owner_items = [{"id": key, "human_source_support_confirmed": True, "checked_source_ids": ["lf-axiom"]}
                   for key in owner_sets["source_support"]]
    owner_items += [{"id": key, "human_product_boundary_confirmed": True} for key in owner_sets["explicit_boundary"]]
    owner_items += [{"id": key, "conditional_owner_acceptance_applied": True} for key in owner_sets["conditional_acceptance"]]
    save(bundle / names["owner_review"], {"items": owner_items, "public_source_sha256": checker.sha(bundle / names["original_corpus"]),
         "dataset_sha256": checker.sha(bundle / names["original_qa"]), "protocol_sha256": checker.sha(bundle / names["original_protocol"])})
    save(bundle / names["owner_session"], {"notice": "Synthetic owner provenance fixture, not actual human review"})
    save(bundle / names["prior_ai"], {"notice": "Synthetic provenance fixture"})
    test = [row for row in active if row["split"] == "test"]
    save(bundle / names["insufficient"], {"quality_status": "not_evaluated", "empty_qrels_are_success": False,
        "case_ids": [row["id"] for row in test if not row["answerable"]]})
    provider = SimpleNamespace(identity="synthetic-package-only", dimensions=4)
    lexical = LocalKnowledgeRetriever(bundle / names["corpus"], tmp_path / "index.sqlite3", tokenizer="jieba")
    docs = lexical.export_documents(today=DAY)
    payload = struct.pack("<4f", 1, 0, 0, 0)
    staged = {doc.hit.chunk_id: (doc.content_hash, payload, hashlib.sha256(payload).hexdigest()) for doc in docs}
    VectorStore(bundle / names["vectors"])._write(staged, provider, lexical.snapshot_metadata(docs, today=DAY)["corpus_hash"])
    queries = cache_path(bundle / "queries", provider, test)
    names["queries"] = queries.relative_to(bundle).as_posix()
    save(queries, {"manifest": {"identity": provider.identity, "dimensions": provider.dimensions,
        "questions": hashlib.sha256(json.dumps([row["question"] for row in test], ensure_ascii=False).encode()).hexdigest()},
        "vectors": [[1, 0, 0, 0] for _ in test]})
    save(bundle / "manifest.json", {"policy": checker.POLICY, "status": "inactive_candidate", "review_status": "ai_assisted_candidate",
        "activated": False, "production_eligible": False, "rankings_run": False,
        "limits": {"original_labels_seen_by_ai": True, "external_or_blinded_review": False, "prior_exposure_erased": False, "independent_human_gold": False},
        "metrics": {"candidate_limit": 20, "rrf_k": 60, "limit": 10, "min_mean_recall6": 0.8, "per_case_no_loss": ["Recall@6", "MRR@10"]},
        "files": {key: {"path": relative, "sha256": ""} for key, relative in names.items()},
        "runtime_files": {key: checker.sha(reference / key) for key in checker.RUNTIME_FILES},
        "prior_owner_decisions": owner_sets,
        "embedding": {"identity": provider.identity, "dimensions": 4, "text_template": TEXT_TEMPLATE}})
    seal(bundle)
    return bundle, reference


def run(package):
    bundle, reference = package
    return checker.check_package(bundle, checker.sha(bundle / "manifest.json"), reference_root=reference, today=DAY)


def asset(bundle, name):
    return bundle / checker.read_json(bundle / "manifest.json")["files"][name]["path"]


def test_valid_inactive_package_never_grades_or_calls_network(package):
    bundle, reference = package
    before = {path: checker.sha(reference / path) for path in (*checker.RUNTIME_FILES, "evals/fitness_qa.jsonl", "evals/release_quality/v1/manifest.json")}
    result = run(package)
    assert result["validation_status"] == "valid_inputs_not_evaluated"
    assert (result["test_cases"], result["insufficient_test_cases"], result["provider_requests"]) == (87, 16, 0)
    assert not result["rankings_run"] and not result["production_eligible"]
    assert before == {path: checker.sha(reference / path) for path in before}


def test_rejects_unpinned_manifest(package):
    bundle, reference = package
    with pytest.raises(ValueError, match="digest"):
        checker.check_package(bundle, "0" * 64, reference_root=reference, today=DAY)


@pytest.mark.parametrize("field,value", [("review_status", "reviewed"), ("activated", True), ("production_eligible", True)])
def test_candidate_cannot_claim_publication_or_human_review(package, field, value):
    bundle, _ = package
    manifest = checker.read_json(bundle / "manifest.json")
    manifest[field] = value
    save(bundle / "manifest.json", manifest)
    with pytest.raises(ValueError):
        run(package)


def test_threshold_cannot_be_lowered(package):
    bundle, _ = package
    manifest = checker.read_json(bundle / "manifest.json")
    manifest["metrics"]["min_mean_recall6"] = 0.79
    save(bundle / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Metric"):
        run(package)


def test_asset_path_cannot_escape_package(package):
    bundle, _ = package
    manifest = checker.read_json(bundle / "manifest.json")
    manifest["files"]["mapping"]["path"] = "../outside.json"
    save(bundle / "manifest.json", manifest)
    with pytest.raises(ValueError, match="escapes"):
        run(package)


def test_modified_reference_corpus_is_not_accepted(package):
    _, reference = package
    path = reference / "data/knowledge/sources.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="binding changed"):
        run(package)


def test_correct_qrels_cannot_be_silently_deleted(package):
    bundle, _ = package
    path = asset(bundle, "active")
    rows = checker.read_rows(path)
    next(row for row in rows if row["id"] == "fitness-qa-095")["relevant_chunk_ids"].pop()
    save_rows(path, rows)
    seal(bundle)
    with pytest.raises(ValueError, match="Quarantine"):
        run(package)


def test_partial_quarantine_group_is_not_accepted(package):
    bundle, _ = package
    path = asset(bundle, "quarantine")
    save_rows(path, checker.read_rows(path)[:-1])
    seal(bundle)
    with pytest.raises(ValueError, match="coverage"):
        run(package)


def test_partition_map_must_account_for_all_original_cases(package):
    bundle, _ = package
    path = asset(bundle, "mapping")
    save(path, checker.read_json(path)[:-1])
    seal(bundle)
    with pytest.raises(ValueError, match="mapping"):
        run(package)


def test_ai_record_cannot_be_relabelled_human(package):
    bundle, _ = package
    path = asset(bundle, "ai_review")
    data = checker.read_json(path)
    data["items"][0]["reviewer_type"] = "human"
    save(path, data)
    seal(bundle)
    with pytest.raises(ValueError, match="provenance"):
        run(package)


def test_empty_qrels_do_not_become_refusal_success(package):
    bundle, _ = package
    path = asset(bundle, "insufficient")
    data = checker.read_json(path)
    data["empty_qrels_are_success"] = True
    save(path, data)
    seal(bundle)
    with pytest.raises(ValueError, match="ungraded"):
        run(package)


def test_structural_validity_does_not_hide_unresolved_label_scope(package):
    bundle, _ = package
    path = asset(bundle, "recheck")
    data = checker.read_json(path)
    next(row for row in data["items"] if row["id"] == "fitness-qa-037")["dispute_requiring_new_owner_decision"] = True
    save(path, data)
    seal(bundle)
    result = run(package)
    assert result["validation_status"] == "valid_inputs_not_evaluated"
    assert result["unresolved_label_disputes"] == ["fitness-qa-037"]
    assert result["production_eligible"] is False and result["rankings_run"] is False


@pytest.mark.parametrize("fault", ["identity", "dimensions", "order"])
def test_wrong_query_cache_binding_is_not_accepted(package, fault):
    bundle, _ = package
    path = asset(bundle, "queries")
    data = checker.read_json(path)
    if fault == "order":
        data["manifest"]["questions"] = "0" * 64
    else:
        data["manifest"][fault] = "other-model" if fault == "identity" else 1024
    save(path, data)
    seal(bundle)
    with pytest.raises((ValueError, VectorCacheUnavailable), match="cache"):
        run(package)


@pytest.mark.parametrize("flag", ["--live", "--rank"])
def test_cli_has_no_live_or_ranking_switch(flag):
    with pytest.raises(SystemExit) as error:
        checker.main([flag])
    assert error.value.code == 2


def test_cli_default_still_reports_original_v1_without_candidate(monkeypatch):
    monkeypatch.setattr(Settings, "from_env", lambda: pytest.fail("No credentials should be read"))
    result = checker.main([])
    assert result["policy"] == "knowledge-release-quality-v1"
    assert result["review_status"] == "pending_review"
    assert result["candidate_checked"] is False and result["provider_requests"] == 0
