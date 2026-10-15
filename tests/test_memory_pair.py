"""Paired denominators, unavailable protocols and authorization boundaries."""
import json
from types import SimpleNamespace

import pytest

from evals.memory.paired import case_hash, compare, equipment_value, review_template, validate_review
from evals.memory.protocol import load_suite
from scripts import evaluate_memory_pair as runner


def case(key="one"):
    return {"id": key, "steps": [{"op": "message"}], "split": "dev", "family": key}


def version(rows, *, status="completed", unchanged=True):
    return {"status": status, "cases": rows, "source_unchanged": unchanged}


def result(key="one", **checks):
    return {"id": key, "steps": [{"op": "message", "checks": checks}]}


def test_paired_gains_and_regressions_use_identical_slots():
    cases = [case("one"), case("two"), case("three")]
    versions = {"baseline": version([result("one", equipment=True), result("two", equipment=False), result("three", equipment=True)]),
                "candidate": version([result("one", equipment=True), result("two", equipment=True), result("three", equipment=False)])}
    report = compare(cases, versions, live=True, review_verified=True)
    metric = report["metrics"]["equipment"]
    assert report["quality_evaluated"] and metric["total"] == 3
    assert metric["gains"] == metric["regressions"] == 1
    assert metric["delta_percentage_points"] == 0


def test_missing_protocol_field_is_excluded_instead_of_zero():
    report = compare([case()], {"baseline": version([result(equipment=True)]),
                     "candidate": version([result(equipment=True, scope=True)])}, live=True, review_verified=True)
    assert "scope" not in report["metrics"]
    assert report["metrics"]["equipment"]["total"] == 1
    assert report["excluded_checks"] == [{"id": "one", "step": 0, "metric": "scope", "reason": "unsupported_on_one_side"}]


@pytest.mark.parametrize("live,review", [(False, False), (False, True), (True, False)])
def test_wiring_or_unreviewed_results_never_receive_quality_percentages(live, review):
    report = compare([case()], {"baseline": version([result(equipment=False)]),
                     "candidate": version([result(equipment=True)])}, live=live, review_verified=review)
    assert not report["quality_evaluated"]
    assert report["metrics"]["equipment"]["baseline_rate"] is None
    assert report["metrics"]["equipment"]["candidate_rate"] is None
    assert report["metrics"]["equipment"]["delta_percentage_points"] is None


@pytest.mark.parametrize("kind", ["missing", "provider_failure", "changed_source", "runtime_error"])
def test_partial_failed_or_mutated_runs_preserve_missing_cases_without_claims(kind):
    left = version([result(equipment=False)])
    right = version([result(equipment=True)])
    if kind == "missing":
        right["cases"] = []
    elif kind == "provider_failure":
        right["status"] = "provider_failure"
    elif kind == "changed_source":
        right["source_unchanged"] = False
    else:
        right["cases"][0]["steps"][0]["error_type"] = "RuntimeError"
    report = compare([case()], {"baseline": left, "candidate": right}, live=True, review_verified=True)
    assert not report["quality_evaluated"] and not report["complete"]
    assert report["planned_cases"] == 1
    assert all(metric["delta_percentage_points"] is None for metric in report["metrics"].values())
    if kind == "missing":
        assert report["not_run"]["candidate"] == ["one"]
    elif kind == "runtime_error":
        assert report["invalid_cases"]["candidate"] == ["one"]


def test_case_step_misalignment_cannot_silently_compare_wrong_operations():
    right = result(equipment=True)
    right["steps"][0]["op"] = "advice"
    with pytest.raises(ValueError, match="not aligned"):
        compare([case()], {"baseline": version([result(equipment=True)]), "candidate": version([right])}, live=True, review_verified=True)


def test_two_schema_adapters_extract_original_values_and_reject_conflicts():
    assert equipment_value({"training": {"equipment": "哑铃"}}, False) == "哑铃"
    assert equipment_value({"equipment_memory": {"value": "徒手"}}, True) == "徒手"
    assert equipment_value({"training": {"equipment": "哑铃"}, "equipment_memory": {"value": "徒手"}}, True) is None


@pytest.mark.parametrize("modern", [False, True])
def test_live_pair_stops_after_first_provider_failure_and_never_sends_gold(tmp_path, modern):
    from scripts import evaluate_memory as base

    measured = runner.paired_model(base, modern)("live", 10, tmp_path / "synthetic-calls.jsonl", "synthetic-test-model")
    measured.case_id = "synthetic"
    measured.step = {"expect": {"private_gold": "NEVER SEND THIS"}}
    seen = []

    class Provider:
        def generate(self, **kwargs):
            seen.append(kwargs)
            raise base.ModelError("MODEL_TIMEOUT", "synthetic failure")

    measured.provider = Provider()
    for _ in range(2):
        with pytest.raises(base.ModelError):
            measured.generate(system_prompt="normal app prompt", message='{"messages":[]}')
    assert seen == [{"system_prompt": "normal app prompt", "message": '{"messages":[]}'}]
    assert measured.calls == 1
    events = [json.loads(line) for line in measured.ledger.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["attempt", "failure"]


def test_duplicate_and_unknown_case_outputs_cannot_replace_planned_cases():
    for rows in ([result(), result()], [result("unexpected")]):
        with pytest.raises(ValueError, match="Duplicate or unplanned"):
            compare([case()], {"baseline": version(rows)}, live=True, review_verified=True)


def reviewed(manifest, cases):
    # Unit-test fixture only; never written to the project's real review path.
    value = review_template(manifest, cases)
    value.update(independent_human_review=True, reviewer="synthetic-test-fixture", reviewed_at="2026-10-16")
    for row in value["cases"]:
        row["decision"] = "accepted"
    return value


@pytest.mark.parametrize("corruption", ["pending", "dataset", "case", "duplicate"])
def test_review_must_match_frozen_cases_and_actual_review_status(corruption):
    manifest, cases = {"sha256": "test"}, [case()]
    value = reviewed(manifest, cases)
    if corruption == "pending":
        value["independent_human_review"] = False
    elif corruption == "dataset":
        value["dataset_sha256"] = "other"
    elif corruption == "case":
        value["cases"][0]["case_sha256"] = case_hash(case("changed"))
    else:
        value["cases"].append(value["cases"][0])
    with pytest.raises(ValueError):
        validate_review(value, manifest, cases)


def test_plan_preserves_original_split_and_counts_both_versions():
    manifest, cases = load_suite(runner.ROOT / "evals/memory/expressions_v2")
    assert runner.plan(manifest, cases)["maximum_provider_attempts"] == 54
    dev = [row for row in cases if row["split"] == "dev"]
    assert len(dev) == 12 and runner.plan(manifest, dev)["maximum_provider_attempts"] == 30


def test_live_budget_and_label_gates_run_before_git_output_or_provider(tmp_path, monkeypatch):
    manifest, cases = {"sha256": "test"}, [case()]
    monkeypatch.setattr(runner, "git", lambda *args: pytest.fail("Git must not run before authorization checks"))
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Explicit budget"):
        runner.run(manifest, cases, output, mode="live", max_calls=0)
    with pytest.raises(ValueError, match="review"):
        runner.run(manifest, cases, output, mode="live", max_calls=2)
    assert not output.exists()


def test_review_export_never_marks_itself_reviewed_or_overwrites_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    manifest = {"sha256": "test"}
    item = {**case(), "description": "synthetic", "seed_profile": {"equipment": "徒手"}}
    item["steps"] = [{"op": "message", "session": "source", "text": "今天只有哑铃", "confirm": True, "choice": None,
                      "expect": {"outcome": "candidate", "values": ["只有哑铃"], "scope": "day"}}]
    output = tmp_path / "artifacts" / "review"
    runner.prepare_review(output, manifest, [item])
    saved = json.loads((output / "review.json").read_text(encoding="utf-8"))
    assert saved["independent_human_review"] is False and saved["cases"][0]["decision"] == "pending"
    with pytest.raises(FileExistsError):
        runner.prepare_review(output, manifest, [item])
    with pytest.raises(ValueError, match="artifacts"):
        runner.prepare_review(tmp_path / "static", manifest, [item])


def test_offline_parent_uses_keyless_isolated_workers_and_retains_failed_side(tmp_path, monkeypatch):
    actual_root = runner.ROOT
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    for name in runner.EVALUATORS:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((actual_root / name).read_bytes())
    monkeypatch.setattr(runner, "resolve_commit", lambda value: value)
    monkeypatch.setattr(runner, "git", lambda *args: b"b" * 40 if args[0] == "rev-parse" else b"")
    monkeypatch.setattr(runner, "snapshot_archive", lambda commit: b"synthetic")

    def extract(raw, path):
        path.mkdir()
        return {}

    monkeypatch.setattr(runner, "extract_snapshot", extract)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-do-not-forward")
    monkeypatch.setenv("FITNESS_DB_PATH", "private-do-not-open")
    seen = []

    def child(arguments, **kwargs):
        label = kwargs["cwd"].name
        seen.append(label)
        assert arguments[1:3] == ["-I", "-B"] and kwargs["env"]["DASHSCOPE_API_KEY"] == ""
        assert "FITNESS_DB_PATH" not in kwargs["env"]
        if label == "baseline":
            value = {"status": "failed", "error_type": "ImportError", "phase": "import_snapshot"}
            return SimpleNamespace(returncode=1, stdout=json.dumps(value).encode())
        pytest.fail("Do not continue after a failed worker")

    monkeypatch.setattr(runner.subprocess, "run", child)
    report = runner.run({"sha256": "test", "base_day": "2026-09-29"}, [case()], tmp_path / "artifacts/run", baseline="a" * 40)
    assert seen == ["baseline"] and report["status"] == "incomplete"
    assert report["provider_calls"] == 0 and not report["comparison"]["quality_evaluated"]
    assert report["versions"]["baseline"]["phase"] == "import_snapshot"
    assert report["comparison"]["not_run"]["candidate"] == ["one"]
