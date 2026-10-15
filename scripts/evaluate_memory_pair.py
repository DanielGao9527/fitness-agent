"""Compare committed memory workflows using each version's original APIs.

Default annotated mode tests wiring only. Live mode requires a matching human
label review and explicit attempt budget. All output stays under artifacts/.
"""
import argparse
from contextlib import ExitStack, redirect_stdout
from dataclasses import fields
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from scripts.check_memory_baseline import APPLICATION, block_external_network, extract_snapshot, git, resolve_commit, snapshot_archive
from evals.memory.protocol import load_suite, value_matches, candidate_matches
from evals.memory.paired import compare, equipment_value, review_template, validate_review

PROTOCOL = "equipment-memory-paired-v1"
EVALUATORS = ("scripts/evaluate_memory_pair.py", "scripts/evaluate_memory.py", "scripts/check_memory_baseline.py",
              "evals/memory/paired.py", "evals/memory/protocol.py")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def plan(manifest, cases):
    if not cases:
        raise ValueError("No cases selected")
    attempts = sum(step["op"] == "message" for case in cases for step in case["steps"])
    return {"protocol": PROTOCOL, "dataset_sha256": manifest["sha256"], "cases": len(cases),
            "case_ids": [case["id"] for case in cases], "maximum_attempts_per_version": attempts,
            "maximum_provider_attempts": attempts * 2, "model": "qwen3.6-plus",
            "label_review": "matching_human_review_required", "embedding_calls": 0,
            "production_database_used": False}


def new_output(output):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("Output must be a new ignored artifacts directory")
    output.mkdir(parents=True, exist_ok=False)
    return output


def prepare_review(output, manifest, cases):
    output = new_output(output)
    save(output / "review.json", review_template(manifest, cases))
    lines = ["# 同题对照标签复核", "", "这是待人工复核的自建合成场景，不是已审核或外部基准。",
             "逐条核对下述输入、用户操作与预期；有异议先停止，不原地改冻结标签。",
             "确认后在 review.json 填写真实复核人、日期、independent_human_review=true，逐条 decision=accepted。",
             "该文件不包含模型结果，也不自动授权调用。", ""]
    for case in cases:
        lines += ["## " + case["id"], "", case["description"], "",
                  "初始档案：" + json.dumps(case["seed_profile"], ensure_ascii=False), ""]
        scopes = {"session": "仅本次会话", "day": "仅今天", "long_term": "长期档案", "unspecified": "尚未明确范围"}
        for number, step in enumerate(case["steps"], 1):
            if step["op"] == "message":
                expected = step["expect"]
                meaning = ("提取器械「" + " / ".join(expected["values"]) + "」，范围为" + scopes[expected["scope"]]
                           if expected["outcome"] == "candidate" else "拒绝调用期间条件改变的过期结果"
                           if expected["outcome"] == "stale" else "补问，不采纳器械候选")
                text = "在会话「" + step["session"] + "」说：“" + step["text"] + "”；预期：" + meaning
                text += "；随后用户明确确认" if step["confirm"] else "；不确认、不保存"
                if step["choice"]:
                    text += "，用户选择范围：" + scopes[step["choice"]]
                if step.get("during"):
                    text += "；理解期间额外操作：" + json.dumps(step["during"], ensure_ascii=False)
            elif step["op"] == "advice":
                expected = step["expect"]
                text = "在会话「" + step["session"] + "」问今天练什么；预期状态：" + expected["status"]
                if expected["status"] == "ready":
                    text += "；预期器械：" + " / ".join(expected["values"])
                    text += "；动作仅可用：" + ", ".join(expected["resources"])
            elif step["op"] == "stale":
                text = "查看旧会话「" + step["session"] + "」的建议是否过期（接口字段诊断，不与缺字段的旧版算准确率差）"
            elif step["op"] == "advance":
                text = "业务日期前进 " + str(step["days"]) + " 天"
            else:
                text = json.dumps(step, ensure_ascii=False)
            lines += [str(number) + ". " + text]
        lines += [""]
    (output / "REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    save(output / "plan.json", plan(manifest, cases))


def paired_model(base, modern):
    class PairModel(base.MeasuredModel):
        def generate(self, *, system_prompt, message):
            if self.mode == "live" and self.failures:
                raise base.ModelError("EVAL_STOP", "Provider failure; no retry", 429)
            if self.mode != "annotated" or modern:
                return super().generate(system_prompt=system_prompt, message=message)
            data = json.loads(message)
            self.invocations += 1
            self.last = None
            if self.during:
                self.during()
            result = {"scope": "strength", "command": "", "clarification": "none", "notes": [
                {"version": turn["version"], "avoid": [], "preferences": [],
                 "training_caution": False, "diet_caution": False, "unresolved": False} for turn in data["messages"]]}
            if self.step["expect"]["outcome"] in ("candidate", "stale"):
                result["training"] = {"source_text": data["messages"][-1]["message"],
                                      "equipment": self.step["expect"]["values"][0]}
            else:
                result["clarification"] = "which_training"
            raw = json.dumps(result, ensure_ascii=False)
            self.last = base.CoachUnderstanding.model_validate_json(raw).model_dump()
            return raw

    return PairModel


def worker(payload, snapshot, progress):
    snapshot = Path(snapshot).resolve()
    if snapshot.name not in {"baseline", "candidate"} or not snapshot.parent.name.startswith("memory-pair-"):
        raise ValueError("Worker requires an isolated snapshot")
    runtime = snapshot / ".pair-runtime"
    runtime.mkdir(exist_ok=False)
    os.environ["FITNESS_DB_PATH"] = str(runtime / "module-default.sqlite3")
    if payload["mode"] != "live":
        os.environ["DASHSCOPE_API_KEY"] = ""
        progress["network_attempts"] = 0
        block_external_network(progress)
    sys.path.insert(0, str(snapshot))
    progress["phase"] = "import_snapshot"
    # Both versions use the same frozen evaluator; app imports stay in their
    # exported source trees. No production module or user database is changed.
    spec = importlib.util.spec_from_file_location("paired_scenario_base", ROOT / "scripts/evaluate_memory.py")
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    import app
    import schemas
    if Path(app.__file__).resolve().parent != snapshot or Path(schemas.__file__).resolve().parent != snapshot:
        raise RuntimeError("Application imports escaped the selected snapshot")
    modern = "equipment_memory" in schemas.CoachUnderstanding.model_fields

    class PairScenario(base.Scenario):
        def __init__(self, case, directory, measured, base_day):
            self.case, self.model, self.day = case, measured, date.fromisoformat(base_day)
            self.sessions, self.clients, self.client_stack = {}, {}, ExitStack()
            arguments = dict(database_path=directory / "personal.sqlite3", knowledge_index_path=directory / "knowledge.sqlite3",
                retrieval_mode="lexical", model_provider="qwen", qwen_model=measured.model_name,
                qwen_api_key=os.getenv("DASHSCOPE_API_KEY", "") if measured.mode == "live" else "synthetic-only",
                coach_enabled=True, training_plan_enabled=True, registration_enabled=True, guest_enabled=False,
                ai_user_daily_limit=1000, ai_global_daily_limit=1000)
            supported = {field.name for field in fields(base.Settings)}
            self.settings = base.Settings(**{key: value for key, value in arguments.items() if key in supported})
            self.app = base.create_app(self.settings)
            measured.provider = base.QwenTextModel(self.settings) if measured.mode == "live" else None

        def run_step(self, step):
            if step["op"] == "message":
                data = self.send(step["session"], step["text"])
                client = self.client()
                before = self.checked(client.get("/api/profile"))["equipment"]
                day_before = data["context"].get("day_equipment", {}).get("revision")
                self.model.step, self.model.last = step, None
                self.model.during = (lambda: self.mutation(step["during"])) if step["during"] else None
                try:
                    response = client.post(f"{base.BASE}/{data['id']}/understand", json={
                        "version": data["version"], "client_id": str(uuid4()), "auto_apply": False}) if data["pending"] else None
                finally:
                    self.model.during = None
                status = response.status_code if response is not None else 200
                output = response.json() if response is not None else data
                review = output.get("review") or {}
                expected = step["expect"]
                raw_value = equipment_value(self.model.last, modern)
                accepted = equipment_value(review, modern)
                checks = {}
                if expected["outcome"] == "stale":
                    checks["concurrent_stale_rejection"] = status == 409
                elif expected["outcome"] == "clarify":
                    checks["clarification"] = status == 200 and review.get("status") == "needs_input"
                    checks["no_false_equipment"] = not raw_value and not accepted
                else:
                    checks["raw_equipment_value"] = value_matches(raw_value, expected["values"])
                    checks["accepted_equipment_value"] = status == 200 and value_matches(accepted, expected["values"])
                    checks["manual_confirmation_required"] = status == 200 and bool(output.get("pending")) and review.get("status") == "ready"
                target = step["during"]["value"] if step.get("during") and step["during"]["op"] == "profile" else before
                checks["no_unconfirmed_profile_write"] = self.checked(client.get("/api/profile"))["equipment"] == target
                observed = {"http": status, "raw": self.model.last, "review_status": review.get("status"),
                            "accepted_equipment": accepted}
                diagnostic = {"scope_protocol_supported": modern}
                if modern:
                    diagnostic["no_unconfirmed_day_write"] = self.current(step["session"])["context"]["day_equipment"]["revision"] == day_before
                    if expected["outcome"] == "candidate":
                        diagnostic["candidate_provenance_and_scope"] = candidate_matches(review.get("equipment_memory"), expected, step["text"], data["version"])
                if step["confirm"]:
                    checks["confirmation_applied"] = False
                    if review.get("status") == "ready" and output.get("pending"):
                        body = {"version": output["version"], "review_id": review["id"], "reviewed": True}
                        if modern and step["choice"]:
                            body["equipment_scope"] = step["choice"]
                        confirmed = client.post(f"{base.BASE}/{data['id']}/confirm-understanding", json=body)
                        checks["confirmation_applied"] = confirmed.status_code == 200
                        observed["confirm_http"] = confirmed.status_code
                return {"op": "message", "checks": checks, "observed": observed, "diagnostics": diagnostic}
            if step["op"] == "stale":
                # The old API may not expose this field. Retain it as a diagnostic,
                # never turn field absence into a zero quality score.
                current = self.current(step["session"])
                answers = [turn["response"]["training_recommendation"] for turn in current["turns"]
                           if "training_recommendation" in turn["response"]]
                return {"op": "stale", "checks": {}, "diagnostics": {"stale_flags": [a.get("stale") for a in answers]}}
            result = super().run_step(step)
            if step["op"] == "advice" and step["expect"]["status"] == "ready":
                result["checks"].pop("equipment_adoption", None)
                result["checks"]["equipment_value"] = result["observed"]["http"] == 200 and value_matches(result["observed"]["equipment"], step["expect"]["values"])
                result["checks"]["condition_adoption"] = all(result["checks"][key] for key in
                    ("advice_status", "advice_api_success", "equipment_value", "exercise_resources"))
                result["diagnostics"] = {"reported_source": result["observed"].get("source"),
                                         "source_protocol_supported": modern}
            return result

    ledger = Path(payload["output"]) / (payload["label"] + "-calls.jsonl")
    measured = paired_model(base, modern)(payload["mode"], payload["max_calls"], ledger, payload["model"])
    results = []
    for case in payload["cases"]:
        progress["phase"], measured.case_id = case["id"], case["id"]
        started = time.monotonic()
        with TemporaryDirectory(prefix="case-", dir=runtime) as directory:
            row = PairScenario(case, Path(directory), measured, payload["base_day"]).run()
        row["case_seconds"] = time.monotonic() - started
        results.append(row)
        base.append_event(Path(payload["output"]) / (payload["label"] + "-cases.jsonl"), row)
        if measured.failures or measured.calls >= measured.limit and measured.mode == "live":
            break
    return {"status": "provider_failure" if measured.failures else "completed", "cases": results,
            "provider_calls": measured.calls, "provider_latency": base.latency_summary(measured.latencies),
            "scope_protocol_supported": modern, "network_attempts": progress.get("network_attempts"),
            "application_origin_verified": True}


def run(manifest, cases, output, *, baseline="07b6ec2", candidate=None, mode="annotated", max_calls=0,
        model="qwen3.6-plus", review_file=None):
    required = plan(manifest, cases)["maximum_provider_attempts"]
    review_hash = None
    if mode not in {"annotated", "live"}:
        raise ValueError("Unknown comparison mode")
    if mode == "live":
        if not required <= max_calls <= 200:
            raise ValueError("Explicit budget must cover the selected paired plan (maximum 200)")
        if review_file is None:
            raise ValueError("Matching human label review is required")
        review_raw = Path(review_file).read_bytes()
        validate_review(json.loads(review_raw), manifest, cases)
        review_hash = hashlib.sha256(review_raw).hexdigest()
        if not os.getenv("DASHSCOPE_API_KEY", "").strip():
            raise ValueError("DASHSCOPE_API_KEY missing; no calls made")
    baseline = resolve_commit(baseline)
    candidate = resolve_commit(candidate or git("rev-parse", "HEAD").decode().strip())
    if baseline == candidate:
        raise ValueError("Two distinct committed versions are required")
    if git("status", "--porcelain=v1", "--untracked-files=all", "--", *APPLICATION):
        raise ValueError("Commit application changes before comparison")
    output = new_output(output)
    report = {**plan(manifest, cases), "mode": mode, "model": model, "max_provider_calls": max_calls,
        "baseline_commit": baseline, "candidate_commit": candidate, "review_sha256": review_hash,
        "started_at": datetime.now(timezone.utc).isoformat(), "versions": {}, "status": "running",
        "evaluator_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in EVALUATORS},
        "python": sys.version.split()[0], "tokens_and_cost": "not_collected",
        "limitations": ["self-authored synthetic cases", "fixed baseline-then-candidate order; not randomized",
                        "shared current dependencies", "no aggregate whole-scenario accuracy across unequal capabilities"]}
    save(output / "report.json", report)
    try:
        with TemporaryDirectory(prefix="memory-pair-", dir=output) as temporary:
            evaluator = Path(temporary) / "evaluator"
            for name, digest in report["evaluator_hashes"].items():
                raw = (ROOT / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise RuntimeError("Evaluator changed before snapshot")
                target = evaluator / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            for label, commit in (("baseline", baseline), ("candidate", candidate)):
                snapshot = Path(temporary) / label
                hashes = extract_snapshot(snapshot_archive(commit), snapshot)
                payload = {"label": label, "mode": mode, "model": model, "max_calls": required // 2,
                           "cases": cases, "base_day": manifest["base_day"], "output": str(output)}
                payload_path = output / (label + "-input.json")
                save(payload_path, payload)
                environment = {key: value for key, value in os.environ.items()
                    if not key.startswith("FITNESS_") and key not in {"DASHSCOPE_API_KEY", "OPENAI_API_KEY", "PYTHONPATH"}}
                environment["DASHSCOPE_API_KEY"] = os.getenv("DASHSCOPE_API_KEY", "") if mode == "live" else ""
                result = subprocess.run([sys.executable, "-I", "-B", str(evaluator / "scripts/evaluate_memory_pair.py"), "--_worker", str(payload_path), "--_snapshot", str(snapshot)],
                    cwd=snapshot, env=environment, capture_output=True, timeout=120 + required * 30)
                observed = json.loads(result.stdout)
                if "cases" not in observed:
                    checkpoint = output / (label + "-cases.jsonl")
                    observed["cases"] = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines() if line] if checkpoint.exists() else []
                observed["source_unchanged"] = all(hashlib.sha256((snapshot / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items())
                observed["snapshot_file_hashes"] = hashes
                report["versions"][label] = observed
                save(output / "report.json", report)
                if result.returncode or observed["status"] != "completed" or not observed["source_unchanged"]:
                    break
                print(json.dumps({"version": label, "cases": len(observed["cases"]), "provider_calls": observed["provider_calls"]}), flush=True)
            report["evaluator_snapshot_unchanged"] = all(hashlib.sha256((evaluator / name).read_bytes()).hexdigest() == digest
                                                        for name, digest in report["evaluator_hashes"].items())
            if not report["evaluator_snapshot_unchanged"]:
                raise RuntimeError("Evaluator snapshot was modified")
        report["comparison"] = compare(cases, report["versions"], live=mode == "live", review_verified=bool(review_hash))
        report["status"] = "completed" if report["comparison"]["complete"] else "incomplete"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        report["provider_calls"] = sum(json.loads(line).get("event") == "attempt" for path in output.glob("*-calls.jsonl")
                                       for line in path.read_text(encoding="utf-8").splitlines() if line)
        report["attempted_case_ids"] = {label: list(dict.fromkeys(json.loads(line)["case"] for line in path.read_text(encoding="utf-8").splitlines()
              if line and json.loads(line).get("event") == "attempt")) if path.exists() else []
              for label in ("baseline", "candidate") for path in [output / (label + "-calls.jsonl")]}
        save(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "evals/memory/expressions_v2")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    parser.add_argument("--baseline", default="07b6ec2")
    parser.add_argument("--candidate")
    parser.add_argument("--mode", choices=["annotated", "live"], default="annotated")
    parser.add_argument("--model", default="qwen3.6-plus")
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--prepare-review", action="store_true")
    parser.add_argument("--_worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_snapshot", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        progress = {}
        try:
            with redirect_stdout(io.StringIO()):
                result = worker(json.loads(args._worker.read_text(encoding="utf-8")), args._snapshot, progress)
            print(json.dumps(result, ensure_ascii=True))
            return 0
        except Exception as error:
            print(json.dumps({"status": "failed", "error_type": type(error).__name__, "phase": progress.get("phase")}))
            return 1
    manifest, cases = load_suite(args.suite)
    selected = [case for case in cases if args.split == "all" or case["split"] == args.split]
    if args.plan:
        print(json.dumps({**plan(manifest, selected), "model": args.model}, ensure_ascii=False, indent=2))
        return 0
    if args.output is None:
        parser.error("--output must name a new directory under artifacts")
    try:
        if args.prepare_review:
            prepare_review(args.output, manifest, selected)
            return 0
        report = run(manifest, selected, args.output, baseline=args.baseline, candidate=args.candidate,
                     mode=args.mode, max_calls=args.max_calls, model=args.model, review_file=args.review_file)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print("Comparison did not complete. Check budget, label review and the local receipt; no automatic retry.", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "quality_evaluated": report["comparison"]["quality_evaluated"],
                      "provider_calls": report["provider_calls"]}))
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
