"""Explicit checkpoint continuation; reuse completed cases, never resample misses.

Default stops at the first provider failure. Additional timeout-only rounds need
an explicit --timeout-restarts flag and the same total attempt ceiling.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import evaluate_memory_pair as pair
from evals.memory.paired import compare, validate_review
from evals.memory.protocol import load_suite


def events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line] if path.exists() else []


def healthy_cases(rows, ledger):
    attempts = {row["call"]: row["case"] for row in ledger if row["event"] == "attempt"}
    received = {row["call"] for row in ledger if row["event"] == "response"}
    failed_cases = {case for call, case in attempts.items() if call not in received}
    # Keep content/validation misses. Only infrastructure-interrupted cases need
    # continuation; selecting only passing answers would bias the comparison.
    return [row for row in rows if row["id"] not in failed_cases]


def checkpoint(source, manifest, cases, extras=()):
    source = Path(source).resolve()
    data = json.loads((source / "report.json").read_text(encoding="utf-8"))
    if (data["protocol"] != pair.PROTOCOL or data["mode"] != "live" or data["dataset_sha256"] != manifest["sha256"]
            or data["case_ids"] != [case["id"] for case in cases]):
        raise ValueError("Checkpoint does not match the frozen live suite")
    if data["python"] != sys.version.split()[0]:
        raise ValueError("Checkpoint Python differs")
    for name, digest in data["evaluator_hashes"].items():
        if hashlib.sha256((pair.ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Frozen evaluator changed; do not mix this checkpoint")
    cache = {}
    for label in ("baseline", "candidate"):
        value = data["versions"].get(label, {})
        if value and not value.get("source_unchanged"):
            raise ValueError("Checkpoint application source was modified")
        rows = value.get("cases", [])
        cache[label] = rows if data.get("healthy_cases_only") else healthy_cases(rows, events(source / (label + "-calls.jsonl")))
    if data.get("total_attempts") is not None and extras:
        raise ValueError("Extra budgets are already included in a resumed checkpoint")
    prior = data.get("total_attempts", data["provider_calls"])
    for extra in extras:
        raw = json.loads((Path(extra) / "report.json").read_text(encoding="utf-8"))
        if any(raw.get(key) != data.get(key) for key in ("protocol", "dataset_sha256", "baseline_commit", "candidate_commit", "model")):
            raise ValueError("Extra receipt is not from this same comparison")
        prior += raw["provider_calls"]
    pending = {label: [case for case in cases if case["id"] not in {row["id"] for row in cache[label]}]
               for label in cache}
    needed = sum(step["op"] == "message" for rows in pending.values() for case in rows for step in case["steps"])
    return data, cache, pending, prior, needed


def resume(source, manifest, cases, output, review_file, *, total_limit, extras=(), timeout_restarts=0, retry_connections=False):
    data, cache, pending, prior, needed = checkpoint(source, manifest, cases, extras)
    review_raw = Path(review_file).read_bytes()
    validate_review(json.loads(review_raw), manifest, cases)
    if hashlib.sha256(review_raw).hexdigest() != data["review_sha256"]:
        raise ValueError("Checkpoint label review changed")
    if not 0 <= timeout_restarts <= 2 or not prior + needed <= total_limit <= 200:
        raise ValueError("Explicit total budget must cover checkpoint continuation")
    if not os.getenv("DASHSCOPE_API_KEY", "").strip():
        raise ValueError("Missing credential; no calls made")
    if pair.git("status", "--porcelain=v1", "--untracked-files=all", "--", *pair.APPLICATION):
        raise ValueError("Uncommitted application changes")
    output = pair.new_output(output)
    report = deepcopy(data)
    report.update(status="running", healthy_cases_only=True, previous_attempts=prior, total_attempts=prior,
                  total_limit=total_limit, provider_calls=0, timeout_restarts_allowed=timeout_restarts,
                  checkpoint_sha256=hashlib.sha256((Path(source) / "report.json").read_bytes()).hexdigest(),
                  continuation_tool_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), stages=[],
                  latency_comparable=False, retry_connections=retry_connections,
                  previous_latency={label: value.get("provider_latency") for label, value in data["versions"].items()})
    for label, rows in cache.items():
        report["versions"].setdefault(label, {"source_unchanged": True}).update(
            cases=deepcopy(rows), status="completed" if len(rows) == len(cases) else "incomplete")
    report.pop("comparison", None)
    new_calls = 0
    pair.save(output / "report.json", report)
    try:
        for round_number in range(timeout_restarts + 1):
            pending = {label: [case for case in cases if case["id"] not in {row["id"] for row in cache[label]}] for label in cache}
            needed = sum(step["op"] == "message" for rows in pending.values() for case in rows for step in case["steps"])
            if not needed or prior + new_calls + needed > total_limit:
                break
            phase = output / ("phase-" + str(round_number + 1))
            phase.mkdir()
            failure_codes = []
            with TemporaryDirectory(prefix="memory-pair-", dir=phase) as temporary:
                evaluator = Path(temporary) / "evaluator"
                for name, digest in data["evaluator_hashes"].items():
                    raw = (pair.ROOT / name).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise ValueError("Evaluator changed during continuation")
                    target = evaluator / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                for label in ("baseline", "candidate"):
                    if not pending[label]:
                        continue
                    snapshot = Path(temporary) / label
                    hashes = pair.extract_snapshot(pair.snapshot_archive(data[label + "_commit"]), snapshot)
                    expected = data["versions"].get(label, {}).get("snapshot_file_hashes")
                    if expected and hashes != expected:
                        raise ValueError("Checkpoint source export differs")
                    limit = sum(step["op"] == "message" for case in pending[label] for step in case["steps"])
                    payload = {"label": label, "mode": "live", "model": data["model"], "max_calls": limit,
                               "cases": pending[label], "base_day": manifest["base_day"], "output": str(phase)}
                    pair.save(phase / (label + "-input.json"), payload)
                    env = {key: value for key, value in os.environ.items()
                           if not key.startswith("FITNESS_") and key not in {"OPENAI_API_KEY", "PYTHONPATH"}}
                    process = subprocess.run([sys.executable, "-I", "-B", str(evaluator / "scripts/evaluate_memory_pair.py"),
                        "--_worker", str(phase / (label + "-input.json")), "--_snapshot", str(snapshot)],
                        cwd=snapshot, env=env, capture_output=True, timeout=120 + limit * 30)
                    observed = json.loads(process.stdout)
                    observed["source_unchanged"] = all(hashlib.sha256((snapshot / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items())
                    if not observed["source_unchanged"]:
                        raise ValueError("Application snapshot changed")
                    ledger = events(phase / (label + "-calls.jsonl"))
                    raw_rows = observed.get("cases", events(phase / (label + "-cases.jsonl")))
                    cache[label].extend(healthy_cases(raw_rows, ledger))
                    failures = [row["code"] for row in ledger if row["event"] == "failure"]
                    failure_codes.extend(failures)
                    report["stages"].append({"round": round_number + 1, "version": label, "status": observed["status"],
                        "failure_codes": failures, "provider_latency": observed.get("provider_latency"),
                        "completed_case_ids": [row["id"] for row in healthy_cases(raw_rows, ledger)]})
                    report["versions"][label] = {**observed, "status": "completed" if observed["status"] == "completed" else "incomplete",
                                                  "cases": cache[label], "snapshot_file_hashes": hashes}
                    new_calls = sum(row["event"] == "attempt" for path in output.glob("phase-*/*-calls.jsonl") for row in events(path))
                    report.update(provider_calls=new_calls, total_attempts=prior + new_calls)
                    pair.save(output / "report.json", report)
                    print(json.dumps({"version": label, "cached_cases": len(cache[label]), "total_attempts": prior + new_calls}), flush=True)
                    if process.returncode or observed["status"] != "completed":
                        break
                if not all(hashlib.sha256((evaluator / name).read_bytes()).hexdigest() == digest for name, digest in data["evaluator_hashes"].items()):
                    raise ValueError("Evaluator snapshot changed")
            # Only transport timeouts may enter an explicitly authorized new round.
            allowed_failures = {"MODEL_TIMEOUT", "MODEL_UNAVAILABLE"} if retry_connections else {"MODEL_TIMEOUT"}
            if not failure_codes or not set(failure_codes) <= allowed_failures:
                break
        for label in cache:
            report["versions"].setdefault(label, {"status": "incomplete", "source_unchanged": True})["cases"] = cache[label]
        report["comparison"] = compare(cases, report["versions"], live=True, review_verified=True)
        report["status"] = "completed" if report["comparison"]["complete"] else "incomplete"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        new_calls = sum(row["event"] == "attempt" for path in output.glob("phase-*/*-calls.jsonl") for row in events(path))
        report.update(provider_calls=new_calls, total_attempts=prior + new_calls)
        pair.save(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--budget-extra", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--total-limit", type=int, default=0)
    parser.add_argument("--timeout-restarts", type=int, default=0)
    parser.add_argument("--retry-connections", action="store_true", help="Requires separate explicit authorization; default stops on connection errors")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    manifest, cases = load_suite(pair.ROOT / "evals/memory/expressions_v2")
    if args.plan:
        _, cache, pending, prior, needed = checkpoint(args.checkpoint, manifest, cases, args.budget_extra)
        print(json.dumps({"previous_attempts": prior, "reused_cases": {key: len(rows) for key, rows in cache.items()},
            "maximum_new_attempts_without_failures": needed, "minimum_total_limit": prior + needed}))
        return 0
    if args.output is None or args.review_file is None:
        parser.error("Explicit output, review and total attempt ceiling are required")
    try:
        report = resume(args.checkpoint, manifest, cases, args.output, args.review_file, total_limit=args.total_limit,
                        extras=args.budget_extra, timeout_restarts=args.timeout_restarts, retry_connections=args.retry_connections)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print("Continuation stopped; inspect the local receipt. No unbounded retry.", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "total_attempts": report["total_attempts"],
                      "quality_evaluated": report["comparison"]["quality_evaluated"]}))
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
