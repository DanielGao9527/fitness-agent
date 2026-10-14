"""New wording suites stay separate from frozen v1 evidence."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from config import ROOT
from evals.memory.protocol import load_suite
from scripts.evaluate_memory import main


SUITE = ROOT / 'evals/memory/expressions_v2'


def test_new_suite_is_separate_and_original_evidence_stays_frozen():
    original, old_cases = load_suite()
    manifest, cases = load_suite(SUITE)
    assert original['sha256'] == 'b107eaac08888d4d952dafbe229e8a98c9ec1faa217aabf6516a1cbef066f3e8'
    assert len(old_cases) == 100 and len(cases) == 24
    assert not manifest['human_independent_review']
    assert manifest['independent_review_status'] == 'pending'
    assert {c['id'] for c in old_cases}.isdisjoint(c['id'] for c in cases)
    dev = {c['family'] for c in cases if c['split'] == 'dev'}
    held = {c['family'] for c in cases if c['split'] == 'holdout'}
    assert dev.isdisjoint(held) and len(dev) == len(held) == 4


@pytest.mark.parametrize('change', ['input', 'family_count', 'split_count', 'family_leakage'])
def test_tampered_suite_cannot_silently_change_evaluation(tmp_path, change):
    directory = tmp_path / 'suite'
    shutil.copytree(SUITE, directory)
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    content = directory / 'cases.jsonl'
    if change == 'input':
        content.write_bytes(content.read_bytes() + b'\n')
    elif change == 'family_count':
        manifest['families'] -= 1
    elif change == 'split_count':
        manifest['split_counts']['dev'] += 1
    else:
        cases = [json.loads(line) for line in content.read_text(encoding='utf-8').splitlines()]
        cases[0]['split'] = 'holdout'
        content.write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in cases)+'\n', encoding='utf-8')
        manifest['sha256'] = hashlib.sha256(content.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        load_suite(directory)


def test_plan_new_suite_does_not_call_provider_or_overwrite_results(monkeypatch, capsys):
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.setattr('scripts.evaluate_memory.run_evaluation', lambda *a, **k: pytest.fail('Plan must not run'))
    assert main(['--suite', str(SUITE), '--mode', 'live', '--split', 'all', '--plan']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['cases'] == 24 and result['maximum_model_attempts'] == 27
    assert main(['--plan', '--split', 'all']) == 0
    assert json.loads(capsys.readouterr().out)['cases'] == 100
