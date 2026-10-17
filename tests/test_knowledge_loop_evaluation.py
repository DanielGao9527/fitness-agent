from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from evals.retrieval_quality import protocol
from scripts import evaluate_knowledge_loop as tool
from model.factory import ModelError
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import LocalKnowledgeRetriever, RetrievalSnapshot


def suite():
    return protocol.load_suite(tool.SUITE, tool.SOURCE)


def approved(manifest, cases):
    review = protocol.review_template(manifest, cases)
    review.update(reviewer='Synthetic test reviewer', reviewed_at='2026-10-16')
    for row in review['cases']:
        row['decision'] = 'accepted'
    return review


def test_frozen_semantic_data_has_disjoint_families_and_keeps_repaired_scope_case():
    manifest, cases = suite()
    assert len(cases) == 24 and manifest['families'] == 12
    assert sum(c['answerable'] for c in cases) == 16
    assert protocol.plan(manifest, cases)['maximum_provider_attempts'] == 144
    assert protocol.plan(manifest, protocol.select(cases))['maximum_provider_attempts'] == 72
    assert protocol.plan(manifest, cases, 'lexical')['maximum_provider_attempts'] == 72
    # Keep the original case/labels: fixing routing must not edit the benchmark.
    repaired = next(case for case in cases if case['id'] == 'loop-semantic-draft-01')
    assert repaired['question'] == '一般成年人，肌肉强化活动每周最低要安排几天？'
    assert repaired['answerable'] and repaired['expected_status'] == 'answered'
    assert tool.preflight(manifest, cases, 'hybrid')['static_scope_flags'] == []


@pytest.mark.parametrize('question', [
    '我今天怎么安排训练',
    '一般成年人每周安排几天力量训练，帮我结合记录安排今天',
    '近期肩伤，每周推荐几次力量训练',
])
def test_preflight_still_flags_personal_mixed_and_injury_controls(question):
    manifest, cases = suite()
    # Synthetic negative controls only; never rewrite frozen questions or labels.
    control = deepcopy(cases[0])
    control['question'] = question
    result = tool.preflight(manifest, [control], 'hybrid')
    assert result['static_scope_flags'] == [control['id']]
    assert result['provider_calls'] == 0
    assert suite()[1] == cases


@pytest.mark.parametrize('change', ['bytes', 'family', 'reference', 'duplicate', 'counts', 'answerability', 'controls'])
def test_modified_inputs_or_split_leaks_cannot_silently_change_suite(tmp_path, change):
    manifest, cases = suite()
    if change == 'family': cases[-1]['family'] = cases[0]['family']
    if change == 'reference': cases[-2].update(answerable=True, expected_status='answered', reference_chunk_ids=cases[0]['reference_chunk_ids'])
    if change == 'duplicate': cases[-1] = cases[0]
    if change == 'counts': manifest['cases'] += 1
    if change == 'answerability': cases[0]['answerable'] = False
    if change == 'controls': manifest['controls']['rrf_k'] = 100
    raw = ''.join(json.dumps(c) + '\n' for c in cases).encode()
    if change != 'bytes': manifest['cases_sha256'] = hashlib.sha256(raw).hexdigest()
    (tmp_path / 'cases.jsonl').write_bytes(raw)
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError): protocol.load_suite(tmp_path, tool.SOURCE)


def test_review_cannot_be_exported_as_approved_or_reused_after_case_changes(tmp_path, monkeypatch):
    manifest, cases = suite()
    monkeypatch.setattr(tool, 'ROOT', tmp_path)
    output = tmp_path / 'artifacts/review'
    # Use the real source path; preflight imports no application or private DB.
    tool.prepare_review(output, manifest, cases)
    review = json.loads((output / 'labels.json').read_text(encoding='utf-8'))
    assert all(row['decision'] == 'pending' for row in review['cases'])
    with pytest.raises(ValueError): protocol.validate_review(review, manifest, cases)
    accepted = approved(manifest, cases)
    protocol.validate_review(accepted, manifest, cases)
    cases[0]['question'] += 'changed'
    with pytest.raises(ValueError): protocol.validate_review(accepted, manifest, cases)
    with pytest.raises(FileExistsError): tool.prepare_review(output, manifest, cases)


def test_live_gates_precede_output_credentials_and_paid_calls(tmp_path, monkeypatch):
    manifest, cases = suite()
    monkeypatch.setattr(tool, 'ROOT', tmp_path)
    output = tmp_path / 'artifacts/live'
    monkeypatch.setattr(tool, 'preflight', lambda *a: {'maximum_provider_attempts': 6, 'document_vectors_ready': False})
    monkeypatch.setattr(tool, 'QwenTextModel', lambda *_: pytest.fail('No provider construction'))
    with pytest.raises(ValueError, match='review'): tool.run_live(manifest, cases, output, {}, 6, 'lexical', None)
    review = approved(manifest, cases)
    with pytest.raises(ValueError, match='attempt limit'): tool.run_live(manifest, cases, output, review, 5, 'lexical', None)
    with pytest.raises(ValueError, match='vectors'): tool.run_live(manifest, cases, output, review, 6, 'hybrid', None)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    with pytest.raises(ValueError, match='Credential'): tool.run_live(manifest, cases, output, review, 6, 'lexical', None)
    assert not output.exists()


def test_default_cli_uses_no_key_and_does_not_open_configured_private_database(tmp_path):
    import os
    sentinel = tmp_path / 'private.sqlite3'
    sentinel.write_bytes(b'Do not open this as a database')
    env = {**os.environ, 'FITNESS_DB_PATH': str(sentinel), 'DASHSCOPE_API_KEY': ''}
    result = subprocess.run([sys.executable, str(tool.ROOT / 'scripts/evaluate_knowledge_loop.py'), '--split', 'all'],
                            cwd=tool.ROOT, env=env, capture_output=True, check=True)
    assert json.loads(result.stdout)['provider_calls'] == 0
    assert sentinel.read_bytes() == b'Do not open this as a database'


def test_complete_execution_exports_pending_answer_review_without_quality_or_private_db(tmp_path, monkeypatch):
    manifest, cases = suite()
    cases = [cases[1]]
    monkeypatch.setattr(tool, 'ROOT', tmp_path)
    monkeypatch.setattr(tool, 'source_hashes', lambda: {'unchanged': 'synthetic'})
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'synthetic-unit-only')
    class Model:
        def __init__(self, settings):
            assert settings.qwen_model == 'qwen3.6-plus'
            assert not settings.database_path.exists()
        def generate(self, *, system_prompt, message):
            evidence = json.loads(message)['evidence']
            return json.dumps({'status': 'answered', 'chunk_ids': [evidence[0]['chunk_id']]})
    monkeypatch.setattr(tool, 'QwenTextModel', Model)
    output = tmp_path / 'artifacts/complete'
    report = tool.run_live(manifest, cases, output, approved(manifest, cases), 3, 'lexical', None)
    assert report['status'] == 'completed' and report['source_unchanged']
    assert report['quality_evaluated'] is False and len(report['rows']) == 2
    review = json.loads((output / 'answer-review.json').read_text(encoding='utf-8'))
    assert all(row['verdict'] == 'pending' for row in review['answers'])
    assert review['report_sha256'] == hashlib.sha256((output / 'report.json').read_bytes()).hexdigest()
    assert not list(output.glob('*.sqlite3'))
    assert 'bounded' not in (output / 'ANSWER_REVIEW.md').read_text(encoding='utf-8')
    assert report['rows'][0]['answer']['answer'] in (output / 'ANSWER_REVIEW.md').read_text(encoding='utf-8')
    with pytest.raises(ValueError): protocol.score(report, review, review['report_sha256'])


def test_attempt_is_durable_before_dispatch_and_failed_attempt_stops_every_later_call(tmp_path):
    ledger = tmp_path / 'calls.jsonl'
    budget = tool.Budget(3, ledger)
    def fail():
        assert json.loads(ledger.read_text(encoding='utf-8'))['event'] == 'attempt'
        raise ModelError('MODEL_TIMEOUT', 'private provider body must not be logged')
    with pytest.raises(tool.Halt): budget.invoke('judgment', 'public question', fail)
    with pytest.raises(tool.Halt): budget.invoke('embedding', 'unused', lambda: pytest.fail('No retry'))
    assert budget.calls == 1
    raw = ledger.read_text(encoding='utf-8')
    assert 'private provider body' not in raw and 'public question' not in raw
    assert [json.loads(line)['event'] for line in raw.splitlines()] == ['attempt', 'failure']


def test_embedding_error_cannot_fall_back_and_spend_another_judgment(tmp_path):
    from datetime import date
    base = LocalKnowledgeRetriever(tool.SOURCE, tmp_path / 'public.sqlite3', tokenizer='jieba')
    class Provider:
        identity, dimensions = 'synthetic', 2
        def embed(self, *args, **kwargs): raise ModelError('EMBEDDING_TIMEOUT', 'do not log')
    class Store:
        def load(self, docs, provider): return {d.hit.chunk_id: [1.0, 0.0] for d in docs}
    class Model:
        def generate(self, **kwargs): pytest.fail('No judgment after embedding failure')
    budget = tool.Budget(6, tmp_path / 'calls.jsonl')
    retriever = tool.PinnedRetriever(HybridKnowledgeRetriever(base, tool.CountedEmbedding(Provider(), budget), Store()), date(2026, 10, 15))
    rows = tool.execute_pairs([{'id': 'public', 'question': '力量训练频率'}], retriever, Model(), budget, tmp_path)
    assert len(rows) == budget.calls == 1 and budget.failed and rows[0]['execution_halted']


def test_only_normal_app_inputs_reach_model_and_arm_order_is_counterbalanced(tmp_path):
    from datetime import date
    base = LocalKnowledgeRetriever(tool.SOURCE, tmp_path / 'public.sqlite3')
    hits = base.get_chunks(['cdc-adults:strength'], today=date(2026, 10, 15))
    class Retriever:
        def search_snapshot(self, query, limit=6): return RetrievalSnapshot(hits, {'mode': 'lexical'})
        def get_chunks(self, ids): return hits
    class Model:
        def generate(self, *, system_prompt, message):
            value = json.loads(message)
            assert set(value) == {'question', 'evidence'}
            assert 'SECRET_GOLD' not in system_prompt + message
            return json.dumps({'status': 'answered', 'chunk_ids': ['cdc-adults:strength']})
    budget = tool.Budget(6, tmp_path / 'calls.jsonl')
    cases = [{'id': 'one', 'question': '力量训练频率', 'required_meaning': 'SECRET_GOLD'},
             {'id': 'two', 'question': '肌肉强化频率', 'wrong_answer_example': 'SECRET_GOLD'}]
    rows = tool.execute_pairs(cases, Retriever(), tool.CountedModel(Model(), budget), budget, tmp_path)
    assert [r['arm'] for r in rows] == ['single', 'bounded', 'bounded', 'single']
    assert all(r['status'] == 'answered' for r in rows) and budget.calls == 4


def test_hybrid_degradation_stops_before_spending_a_judgment():
    class Fallback:
        def search_snapshot(self, *args, **kwargs): return RetrievalSnapshot([], {'mode': 'lexical'})
    with pytest.raises(tool.Halt, match='stopped'):
        tool.PinnedRetriever(Fallback(), None, 'hybrid').search_snapshot('question')


def score_fixture():
    cases = [{'id': 'a', 'answerable': True, 'expected_status': 'answered', 'split': 'dev'},
             {'id': 'b', 'answerable': False, 'expected_status': 'insufficient', 'split': 'holdout'}]
    rows = [{'case_id': key, 'arm': arm, 'sample_id': key + arm, 'status': status,
             'answer': {'sources': [{'chunk_id': 'valid-alternative'}] if status == 'answered' else []}}
            for key, arm, status in [('a', 'single', 'insufficient'), ('a', 'bounded', 'answered'),
                                      ('b', 'single', 'insufficient'), ('b', 'bounded', 'answered')]]
    report = {'mode': 'live', 'status': 'completed', 'source_unchanged': True, 'provider_failed': False, 'cases': cases, 'rows': rows}
    review = {'report_sha256': 'binding', 'reviewer': 'Synthetic human', 'reviewed_at': '2026-10-16',
        'answers': [{'sample_id': r['sample_id'], 'verdict': 'correct' if i in (1, 2) else 'incorrect',
                     'reason': 'Synthetic semantic decision, not citation ID scoring'} for i, r in enumerate(rows)]}
    return report, review


def test_semantic_score_preserves_gains_regressions_and_alternative_evidence():
    report, review = score_fixture()
    result = protocol.score(report, review, 'binding')
    assert result['gains'] == result['regressions'] == 1 and result['net_delta_percentage_points'] == 0
    assert result['metrics']['bounded']['unsupported_answer'] == 1
    assert result['metrics']['bounded']['correct_answer'] == [1, 1]
    assert result['splits']['dev']['gains'] == 1 and result['splits']['holdout']['regressions'] == 1


@pytest.mark.parametrize('fault', ['pending', 'binding', 'human', 'partial', 'error', 'changed', 'duplicate', 'sample', 'wiring'])
def test_no_semantic_quality_from_pending_unmatched_incomplete_or_mock_results(fault):
    report, review = score_fixture()
    if fault == 'pending': review['answers'][0]['verdict'] = 'pending'
    if fault == 'binding': review['report_sha256'] = 'other'
    if fault == 'human': review['reviewer'] = ''
    if fault == 'partial': report['rows'].pop()
    if fault == 'error': report['provider_failed'] = True
    if fault == 'changed': report['source_unchanged'] = False
    if fault == 'duplicate': review['answers'][1] = review['answers'][0]
    if fault == 'sample':
        report['rows'][1]['sample_id'] = report['rows'][0]['sample_id']
        review['answers'].pop(1)
    if fault == 'wiring': report['mode'] = 'annotated'
    with pytest.raises(ValueError): protocol.score(report, review, 'binding')
