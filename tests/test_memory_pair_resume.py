"""Resume infrastructure interruptions without rerunning semantic failures."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import resume_memory_pair as resume


def test_cache_keeps_quality_misses_but_not_failed_or_interrupted_provider_calls():
    rows = [{"id": key, "passed": False} for key in ("valid-miss", "timeout", "unknown")]
    ledger = [{"event": "attempt", "call": number, "case": key} for number, key in enumerate(("valid-miss", "timeout", "unknown"), 1)]
    ledger += [{"event": "response", "call": 1}, {"event": "failure", "call": 2, "code": "MODEL_TIMEOUT"}]
    assert resume.healthy_cases(rows, ledger) == [{"id": "valid-miss", "passed": False}]


def setup_fake(tmp_path, monkeypatch, failures):
    actual = resume.pair.ROOT
    monkeypatch.setattr(resume.pair, "ROOT", tmp_path)
    hashes = {}
    for name in resume.pair.EVALUATORS:
        raw = (actual / name).read_bytes()
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "report.json").write_text("{}", encoding="utf-8")
    review = tmp_path / "synthetic-review.json"
    review.write_text("{}", encoding="utf-8")
    case = {"id": "case", "steps": [{"op": "message"}]}
    data = {"protocol": resume.pair.PROTOCOL, "mode": "live", "versions": {"baseline": {
        "source_unchanged": True, "status": "provider_failure", "cases": [{"id": "case", "old_failed": True}]}}, "model": "synthetic",
        "baseline_commit": "a" * 40, "candidate_commit": "b" * 40, "provider_calls": 0,
        "evaluator_hashes": hashes, "review_sha256": hashlib.sha256(review.read_bytes()).hexdigest()}
    monkeypatch.setattr(resume, "checkpoint", lambda *args: (data, {"baseline": [], "candidate": []}, {}, 0, 2))
    monkeypatch.setattr(resume, "validate_review", lambda *args: None)
    monkeypatch.setattr(resume.pair, "git", lambda *args: b"")
    monkeypatch.setattr(resume.pair, "snapshot_archive", lambda commit: b"archive")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-not-a-real-key")

    def extract(content, directory):
        directory.mkdir()
        return {}

    monkeypatch.setattr(resume.pair, "extract_snapshot", extract)
    dispatched = []

    def worker(arguments, **kwargs):
        payload = json.loads(Path(arguments[arguments.index('--_worker') + 1]).read_text(encoding='utf-8'))
        label, output = payload["label"], Path(payload["output"])
        dispatched.append(label)
        if len(dispatched) == 1:
            initial = json.loads((output.parent / 'report.json').read_text(encoding='utf-8'))
            assert initial['healthy_cases_only'] and initial['versions']['baseline']['cases'] == []
        failure = failures[len(dispatched) - 1] if len(dispatched) <= len(failures) else None
        ledger = [{"event": "attempt", "call": 1, "case": "case"}]
        ledger += [{"event": "failure", "call": 1, "code": failure}] if failure else [{"event": "response", "call": 1}]
        (output / (label + '-calls.jsonl')).write_text('\n'.join(json.dumps(row) for row in ledger), encoding='utf-8')
        observed = {"status": "provider_failure" if failure else "completed", "provider_calls": 1,
            "cases": [{"id": "case", "steps": [{"op": "message", "checks": {"adoption": not failure}}]}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(observed).encode())

    monkeypatch.setattr(resume.subprocess, 'run', worker)
    return prior, review, [case], dispatched


@pytest.mark.parametrize("failures,restarts,limit,expected,complete", [
    (["MODEL_TIMEOUT", None, None], 1, 3, 3, True),
    (["MODEL_AUTH_ERROR"], 2, 4, 1, False),
    (["MODEL_TIMEOUT", "MODEL_TIMEOUT", "MODEL_TIMEOUT"], 2, 4, 3, False),
    (["MODEL_TIMEOUT"], 2, 2, 1, False),
    (["MODEL_TIMEOUT"], 0, 4, 1, False),
])
def test_timeout_rounds_need_opt_in_and_cannot_exceed_total_budget(tmp_path, monkeypatch, failures, restarts, limit, expected, complete):
    prior, review, cases, dispatched = setup_fake(tmp_path, monkeypatch, failures)
    result = resume.resume(prior, {"base_day": "2026-09-29"}, cases, tmp_path / "artifacts/out", review,
                           total_limit=limit, timeout_restarts=restarts)
    assert len(dispatched) == result["provider_calls"] == result["total_attempts"] == expected
    assert expected <= limit
    assert result["comparison"]["quality_evaluated"] is complete
    if restarts == 0 or failures[0] != "MODEL_TIMEOUT":
        assert dispatched == ["baseline"]


def test_insufficient_total_budget_stops_before_output_or_dispatch(tmp_path, monkeypatch):
    prior, review, cases, dispatched = setup_fake(tmp_path, monkeypatch, [])
    with pytest.raises(ValueError, match="budget"):
        resume.resume(prior, {"base_day": "2026-09-29"}, cases, tmp_path / "artifacts/out", review, total_limit=1)
    assert not dispatched and not (tmp_path / "artifacts/out").exists()


@pytest.mark.parametrize("allowed,expected", [(False, 1), (True, 3)])
def test_connection_restart_requires_a_separate_explicit_flag(tmp_path, monkeypatch, allowed, expected):
    prior, review, cases, dispatched = setup_fake(tmp_path, monkeypatch, ["MODEL_UNAVAILABLE", None, None])
    result = resume.resume(prior, {"base_day": "2026-09-29"}, cases, tmp_path / "artifacts/out", review,
                           total_limit=3, timeout_restarts=1, retry_connections=allowed)
    assert len(dispatched) == result['total_attempts'] == expected
    assert result['comparison']['quality_evaluated'] is allowed
