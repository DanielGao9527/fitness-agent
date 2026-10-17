import json
import shutil

import pytest

from scripts.replay_knowledge_loop import DEFAULT_SUITE, DEFAULT_SOURCE, ROOT, load_suite, main, replay_suite


def test_frozen_single_and_bounded_contracts_and_privacy():
    result = replay_suite()
    assert result["contracts"] == {name: {"passed": 24, "total": 24} for name in ("single", "bounded")}
    assert result["real_provider_calls"] == 0 and result["not_model_quality"]
    assert not result["uses_private_database"] and result["phase_timings_are_virtual"]
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in ('"question"', '"outputs"', '"retry_query"', '"excerpt"', '"content"'):
        assert forbidden not in serialized
    rows = {(row["case_id"], row["policy"]): row for row in result["cases"]}
    assert rows["zero_recovery", "bounded"]["simulated_calls"] == {"retrieval": 2, "embedding": 2, "judgment": 2}
    assert rows["zero_recovery", "single"]["simulated_calls"]["judgment"] == 0
    assert rows["both_empty", "bounded"]["simulated_calls"]["judgment"] == 1
    assert rows["withdraw_after_second", "bounded"]["status"] == "error"


@pytest.mark.parametrize("change", ["cases", "corpus", "count"])
def test_replay_refuses_mixed_or_modified_inputs(tmp_path, change):
    suite = tmp_path / "suite"
    shutil.copytree(DEFAULT_SUITE, suite)
    source = DEFAULT_SOURCE
    if change == "cases":
        with (suite / "cases.jsonl").open("ab") as stream:
            stream.write(b'\n')
    elif change == "corpus":
        copied = tmp_path / "sources.json"
        copied.write_bytes(source.read_bytes() + b'\n')
        source = copied
    else:
        manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
        manifest["cases"] += 1
        (suite / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_suite(suite, source)


def test_cli_writes_aggregate_and_refuses_overwriting_prior_report(tmp_path):
    output = tmp_path / "run"
    assert main(["--output", str(output)]) == 0
    before = (output / "report.json").read_bytes()
    assert "不是独立语义评测" in (output / "REPORT.md").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        main(["--output", str(output)])
    assert (output / "report.json").read_bytes() == before


def test_frozen_replay_keeps_old_corpus_and_refuses_corrected_runtime_catalog():
    assert DEFAULT_SOURCE != ROOT / "data/knowledge/sources.json"
    load_suite(DEFAULT_SUITE, DEFAULT_SOURCE)
    with pytest.raises(ValueError, match="corpus mismatch"):
        load_suite(DEFAULT_SUITE, ROOT / "data/knowledge/sources.json")
