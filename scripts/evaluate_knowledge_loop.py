"""Paired one/two-round knowledge evaluation; default plans without credentials."""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT, Settings
from evals.retrieval_quality.protocol import load_suite, select, plan, review_template, validate_review, score
from model.embeddings import QwenEmbeddingProvider
from model.factory import ModelError
from model.qwen import QwenTextModel
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import LocalKnowledgeRetriever
from rag.vector_store import VectorStore, VectorCacheUnavailable
from services.knowledge_answers import answer_question, outside_scope
from services.knowledge_policy import KnowledgePolicy

SUITE = ROOT / 'evals/retrieval_quality/v1'
# v1 compares the frozen historical corpus, never a silently edited live catalog.
SOURCE = ROOT / 'evals/corpora/reviewed-training-notes-2026-09-23.2.json'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def append(path, value):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def new_output(path):
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT / 'artifacts').resolve()):
        raise ValueError('Output must be a new ignored artifacts directory')
    path.mkdir(parents=True, exist_ok=False)
    return path


def source_hashes():
    paths = [path for pattern in ('model/*.py', 'rag/*.py', 'services/knowledge*.py') for path in ROOT.glob(pattern)]
    paths += [ROOT / name for name in ('config.py', 'schemas.py', 'prompts/knowledge_answer.md',
        'data/knowledge/sources.json', 'evals/corpora/reviewed-training-notes-2026-09-23.2.json',
        'scripts/evaluate_knowledge_loop.py', 'evals/retrieval_quality/protocol.py',
        'evals/retrieval_quality/v1/cases.jsonl', 'evals/retrieval_quality/v1/manifest.json')]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class Halt(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Evaluation stopped; inspect the private attempt receipt')


class Budget:
    def __init__(self, limit, ledger):
        self.limit, self.ledger, self.calls, self.failed = limit, ledger, 0, False
        self.context = {}

    def invoke(self, kind, request, callback):
        if self.failed or self.calls >= self.limit:
            raise Halt('EVAL_ATTEMPT_LIMIT')
        self.calls += 1
        event = {**self.context, 'call': self.calls, 'kind': kind}
        append(self.ledger, {**event, 'event': 'attempt', 'request_sha256': hashlib.sha256(request.encode()).hexdigest(),
                             'input_chars': len(request)})
        started = time.monotonic()
        try:
            value = callback()
        except Exception as error:
            self.failed = True
            code = error.code if isinstance(error, ModelError) else 'PROVIDER_TRANSPORT_ERROR'
            append(self.ledger, {**event, 'event': 'failure', 'code': code})
            # Deliberately not ModelError: hybrid retrieval must not swallow the
            # first provider error and spend more calls through lexical fallback.
            raise Halt(code) from None
        append(self.ledger, {**event, 'event': 'response', 'elapsed_seconds': time.monotonic() - started})
        return value


class CountedModel:
    def __init__(self, model, budget):
        self.model, self.budget = model, budget

    def generate(self, *, system_prompt, message):
        return self.budget.invoke('judgment', system_prompt + message,
            lambda: self.model.generate(system_prompt=system_prompt, message=message))


class CountedEmbedding:
    def __init__(self, provider, budget):
        self.provider, self.budget = provider, budget
        self.identity, self.dimensions = provider.identity, provider.dimensions

    def embed(self, texts, *, text_type, before_call=None):
        if text_type != 'query' or len(texts) != 1:
            raise Halt('EVAL_QUERY_ENCODING_ONLY')
        return self.budget.invoke('embedding', json.dumps(texts, ensure_ascii=False),
            lambda: self.provider.embed(texts, text_type=text_type, before_call=before_call))


class PinnedRetriever:
    def __init__(self, retriever, day, required_mode=None):
        self.retriever, self.day, self.required_mode = retriever, day, required_mode

    def search_snapshot(self, query, limit=6):
        snapshot = self.retriever.search_snapshot(query, limit=limit, today=self.day)
        if self.required_mode and snapshot.metadata.get('mode') != self.required_mode:
            raise Halt('EVAL_RETRIEVAL_DEGRADED')
        return snapshot

    def get_chunks(self, ids):
        return self.retriever.get_chunks(ids, today=self.day)


class EmptyProfile:
    def get_profile(self):
        return {}


def preflight(manifest, cases, retrieval, vectors=None):
    day = date.fromisoformat(manifest['source_date'])
    with TemporaryDirectory(prefix='fitness-loop-preflight-') as directory:
        lexical = LocalKnowledgeRetriever(SOURCE, Path(directory) / 'public.sqlite3', tokenizer='jieba')
        known = {hit.chunk_id for hit in lexical.all_chunks(today=day)}
        for case in cases:
            if set(case['reference_chunk_ids']) - known:
                raise ValueError('Label references an unavailable public source')
        ready = False
        if retrieval == 'hybrid' and vectors is not None:
            if not Path(vectors).is_file():
                raise ValueError('Document vector cache must already exist')
            settings = Settings(database_path=Path(directory) / 'unused-private.sqlite3',
                                embedding_model='text-embedding-v4', embedding_dimensions=1024)
            provider = QwenEmbeddingProvider(settings)
            VectorStore(vectors).load(lexical.export_documents(today=day), provider)
            ready = True
    return {**plan(manifest, cases, retrieval), 'corpus_sha256': manifest['corpus_sha256'],
        'evaluation_corpus': 'frozen_historical_v1_not_current_runtime_quality',
        'document_vectors_ready': ready, 'static_scope_flags': [c['id'] for c in cases if outside_scope(c['question'], {})],
        'preflight_is_not_retrieval_or_model_quality': True}


def prepare_review(output, manifest, cases):
    preflight(manifest, cases, 'lexical')
    output = new_output(output)
    save(output / 'labels.json', review_template(manifest, cases))
    lines = ['# E3 单轮/两轮：标签复核', '',
        '自建公共摘要题，非外部基准；当前全部 pending，没有调用授权或效果结果。',
        '逐题核对问题能否由摘要回答、必要含义是否只针对本题、是否有替代证据。',
        '候选参考集不穷尽；缺证据题须检查完整语料。问题 01 的“安排”会触发范围拦截，原题保留。',
        '有异议停止，另建标签版本；不要只给某个方案补有利标签。确认后在 labels.json 填真实复核人/日期及 accepted。',
        '[完整审核语料](' + SOURCE.as_posix() + ')；资料不足题须检查完整语料，不能仅核对下方候选出处。', '']
    for case in cases:
        lines += ['## ' + case['id'], '', case['question'], '',
            f"划分：{case['split']} / {case['family']}；候选预期：{case['expected_status']}",
            '候选出处：' + ', '.join(case['reference_chunk_ids']),
            '必要含义草案：' + case['required_meaning'], '错误例：' + case['wrong_answer_example'], '']
    corpus = json.loads(SOURCE.read_text(encoding='utf-8'))
    wanted = {key for case in cases for key in case['reference_chunk_ids']}
    lines += ['# 候选出处摘要', '']
    for source in corpus['sources']:
        for section in source['sections']:
            key = source['id'] + ':' + section['id']
            if key in wanted:
                lines += ['## ' + key, '', section['summary'], '', '适用范围：' + source['scope'],
                          '原文：' + source['url'], '']
    (output / 'REVIEW.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def execute_pairs(cases, retriever, model, budget, output, *, seconds=45):
    rows = []
    for number, case in enumerate(cases):
        order = ('single', 'bounded') if number % 2 == 0 else ('bounded', 'single')
        for arm in order:
            budget.context = {'case_id': case['id'], 'arm': arm}
            trace, error, halted, started = {}, None, False, time.monotonic()
            try:
                answer = answer_question(case['question'], retriever, EmptyProfile(), lambda: model, 'qwen3.6-plus',
                    policy=KnowledgePolicy(max_rounds=1 if arm == 'single' else 2, time_budget_seconds=seconds), audit=trace)
            except (Halt, ModelError) as issue:
                answer = {'status': 'error', 'sources': []}
                error = issue.code
                halted = isinstance(issue, Halt)
            row = {'case_id': case['id'], 'arm': arm, 'sample_id': uuid4().hex, 'status': answer['status'],
                   'answer': answer, 'trace': trace, 'error_code': error,
                   'execution_halted': halted, 'elapsed_seconds': time.monotonic() - started}
            rows.append(row)
            append(output / 'predictions.jsonl', row)
            print(json.dumps({'case': case['id'], 'arm': arm, 'status': row['status'], 'provider_attempts': budget.calls}), flush=True)
            if halted:
                return rows
    return rows


def export_answers(output, report):
    binding = hashlib.sha256((output / 'report.json').read_bytes()).hexdigest()
    rows = [{'sample_id': row['sample_id'], 'verdict': 'pending', 'reason': ''} for row in report['rows']]
    random.SystemRandom().shuffle(rows)
    save(output / 'answer-review.json', {'report_sha256': binding, 'reviewer': '', 'reviewed_at': '', 'answers': rows})
    indexed = {r['sample_id']: r for r in report['rows']}
    cases = {c['id']: c for c in report['cases']}
    lines = ['# E3 回答复核（隐藏方案名）', '',
        '每条独立核对：是否直接回答原问题、覆盖必要信息、引用支持且未越界。',
        '不能仅凭引用 ID 或 answered 状态判正确。有效替代出处可接受，须写明原因。',
        '资料不足须确实缺证据且未用相近主题冒充；运行错误不算正确。此包隐藏方案名，不宣称独立盲测。', '']
    for entry in rows:
        row = indexed[entry['sample_id']]
        case = cases[row['case_id']]
        lines += ['## ' + entry['sample_id'], '', case['question'], '', '状态：' + row['status'],
                  '页面答复：' + row['answer'].get('answer', ''),
                  '必要含义草案：' + case['required_meaning']]
        for source in row['answer'].get('sources', []):
            lines += [source['chunk_id'] + '：' + source['excerpt']]
        lines += ['']
    (output / 'ANSWER_REVIEW.md').write_text('\n'.join(lines), encoding='utf-8')


def run_live(manifest, cases, output, review, max_calls, retrieval, vectors):
    validate_review(review, manifest, cases)
    planned = preflight(manifest, cases, retrieval, vectors)
    if type(max_calls) is not int or not planned['maximum_provider_attempts'] <= max_calls <= 200:
        raise ValueError('Explicit attempt limit must cover the selected paired maximum, at most 200')
    if retrieval == 'hybrid' and not planned['document_vectors_ready']:
        raise ValueError('Prepare matching public document vectors separately; no automatic build')
    key = os.getenv('DASHSCOPE_API_KEY', '').strip()
    if not key:
        raise ValueError('Credential missing; live run not started')
    output = new_output(output)
    before = source_hashes()
    vector_hash = hashlib.sha256(Path(vectors).read_bytes()).hexdigest() if retrieval == 'hybrid' else None
    budget = Budget(max_calls, output / 'calls.jsonl')
    report = {'protocol': manifest['version'], 'mode': 'live', 'status': 'running', 'cases': cases, 'rows': [],
        'plan': planned, 'source_hashes': before, 'label_review_sha256': hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest(),
        'vector_cache_sha256': vector_hash, 'model': 'qwen3.6-plus', 'arm_order': 'alternating by case, no shared query cache',
        'provider_calls': 0, 'provider_failed': False, 'uses_private_database': False, 'quality_evaluated': False,
        'tokens_and_cost': 'not_collected', 'notice': 'Human answer review required; no automatic quality score'}
    save(output / 'report.json', report)
    try:
        with TemporaryDirectory(prefix='fitness-loop-eval-') as directory:
            lexical = LocalKnowledgeRetriever(SOURCE, Path(directory) / 'public.sqlite3', tokenizer='jieba')
            settings = Settings(database_path=Path(directory) / 'unused-private.sqlite3', qwen_api_key=key,
                                qwen_model='qwen3.6-plus', embedding_model='text-embedding-v4', embedding_dimensions=1024)
            retriever = lexical if retrieval == 'lexical' else HybridKnowledgeRetriever(lexical,
                CountedEmbedding(QwenEmbeddingProvider(settings), budget), VectorStore(vectors), candidate_limit=20, rrf_k=60)
            report['rows'] = execute_pairs(cases, PinnedRetriever(retriever, date.fromisoformat(manifest['source_date']), retrieval),
                CountedModel(QwenTextModel(settings), budget), budget, output)
        report['status'] = ('completed' if len(report['rows']) == len(cases) * 2
                            and not budget.failed and not any(r['execution_halted'] for r in report['rows']) else 'incomplete')
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__)
        if (output / 'predictions.jsonl').exists():
            report['rows'] = [json.loads(line) for line in (output / 'predictions.jsonl').read_text(encoding='utf-8').splitlines()]
        raise
    finally:
        report.update(provider_calls=budget.calls, provider_failed=budget.failed,
            source_unchanged=source_hashes() == before and (vector_hash is None or hashlib.sha256(Path(vectors).read_bytes()).hexdigest() == vector_hash))
        save(output / 'report.json', report)
    if report['status'] == 'completed' and report['source_unchanged']:
        export_answers(output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--split', choices=['dev', 'holdout', 'all'], default='dev')
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--retrieval', choices=['hybrid', 'lexical'], default='hybrid')
    parser.add_argument('--vectors', type=Path)
    parser.add_argument('--prepare-review', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--review-file', type=Path)
    parser.add_argument('--max-calls', type=int, default=0)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--score-report', type=Path)
    parser.add_argument('--answer-review', type=Path)
    args = parser.parse_args(argv)
    manifest, cases = load_suite(args.suite, SOURCE)
    if args.score_report is not None:
        if args.live or args.prepare_review or args.answer_review is None or args.output is None:
            parser.error('Scoring requires a completed report, human answer review and a new output')
        raw = args.score_report.read_bytes()
        report = json.loads(raw)
        known = {c['id']: c for c in cases}
        if (report['plan']['dataset_sha256'] != manifest['cases_sha256']
                or any(known.get(c['id']) != c for c in report['cases'])):
            parser.error('Report does not match frozen semantic cases')
        review = json.loads(args.answer_review.read_text(encoding='utf-8'))
        result = score(report, review, hashlib.sha256(raw).hexdigest())
        result.update(report_sha256=hashlib.sha256(raw).hexdigest(),
                      answer_review_sha256=hashlib.sha256(args.answer_review.read_bytes()).hexdigest())
        output = new_output(args.output)
        save(output / 'score.json', result)
        print(json.dumps(result))
        return 0
    cases = select(cases, args.split, args.case)
    if args.prepare_review and args.live:
        parser.error('Review export cannot authorize or start live calls')
    if args.prepare_review:
        if args.output is None:
            parser.error('Review export requires a new ignored output directory')
        prepare_review(args.output, manifest, cases)
        return 0
    if not args.live:
        print(json.dumps(preflight(manifest, cases, args.retrieval, args.vectors)))
        return 0
    if args.output is None or args.review_file is None:
        parser.error('Live mode requires human review, explicit budget and a new output')
    review = json.loads(args.review_file.read_text(encoding='utf-8'))
    result = run_live(manifest, cases, args.output, review, args.max_calls, args.retrieval, args.vectors)
    print(json.dumps({key: result[key] for key in ('status', 'provider_calls', 'source_unchanged', 'quality_evaluated')}))
    return 0 if result['status'] == 'completed' and result['source_unchanged'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ModelError, VectorCacheUnavailable) as error:
        print(type(error).__name__ + ': evaluation stopped; inspect local receipts, no automatic retry', file=sys.stderr)
        raise SystemExit(2) from None
