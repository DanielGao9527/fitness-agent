"""Frozen semantic inputs and explicit human review; reference hits are not quality."""
from collections import Counter
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


VERSION = 'knowledge-loop-semantic-v1'
CONTROLS = {'text_model': 'qwen3.6-plus', 'embedding_model': 'text-embedding-v4', 'dimensions': 1024,
            'tokenizer': 'jieba', 'candidate_limit': 20, 'rrf_k': 60, 'per_arm_time_budget_seconds': 45}


class Case(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z0-9_-]{1,64}$')
    family: str = Field(pattern=r'^[a-z0-9_-]{1,64}$')
    split: Literal['dev', 'holdout']
    question: str = Field(min_length=1, max_length=1000)
    answerable: bool
    expected_status: Literal['answered', 'insufficient']
    reference_chunk_ids: list[str] = Field(max_length=20)
    reference_set_exhaustive: Literal[False]
    required_meaning: str = Field(min_length=1, max_length=1600)
    wrong_answer_example: str = Field(min_length=1, max_length=1600)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_suite(directory, source):
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    raw = (directory / 'cases.jsonl').read_bytes()
    if (manifest['version'] != VERSION or manifest['controls'] != CONTROLS
            or hashlib.sha256(raw).hexdigest() != manifest['cases_sha256']
            or hashlib.sha256(source.read_bytes()).hexdigest() != manifest['corpus_sha256']):
        raise ValueError('Frozen semantic suite or public corpus mismatch')
    cases = [Case.model_validate_json(line).model_dump() for line in raw.splitlines() if line.strip()]
    if (len(cases) != manifest['cases'] or len({c['id'] for c in cases}) != len(cases)
            or len({c['family'] for c in cases}) != manifest['families']
            or dict(Counter(c['split'] for c in cases)) != manifest['split_counts']
            or {'true': sum(c['answerable'] for c in cases), 'false': sum(not c['answerable'] for c in cases)} != manifest['answerable_counts']):
        raise ValueError('Suite counts or unique IDs differ')
    families, references, questions = {}, {}, set()
    for case in cases:
        question = ''.join(char.casefold() for char in case['question'] if char.isalnum())
        if question in questions or case['expected_status'] != ('answered' if case['answerable'] else 'insufficient'):
            raise ValueError('Duplicate question or inconsistent answerability')
        questions.add(question)
        if case['answerable'] != bool(case['reference_chunk_ids']) or len(set(case['reference_chunk_ids'])) != len(case['reference_chunk_ids']):
            raise ValueError('Candidate reference labels are inconsistent')
        for index, keys in ((families, [case['family']]), (references, case['reference_chunk_ids'])):
            for key in keys:
                if key in index and index[key] != case['split']:
                    raise ValueError('Family or reference leaks between splits')
                index[key] = case['split']
    return manifest, cases


def select(cases, split='dev', ids=()):
    selected = [c for c in cases if (split == 'all' or c['split'] == split) and (not ids or c['id'] in ids)]
    if not selected or set(ids) - {c['id'] for c in selected}:
        raise ValueError('Empty or unknown selection')
    return selected


def plan(manifest, cases, retrieval='hybrid'):
    multiplier = 2 if retrieval == 'hybrid' else 1
    return {'protocol': VERSION, 'dataset_sha256': manifest['cases_sha256'], 'cases': len(cases),
            'split_counts': dict(Counter(c['split'] for c in cases)), 'retrieval': retrieval,
            'maximum_judgment_attempts': len(cases) * 3,
            'maximum_query_embedding_attempts': len(cases) * 3 if retrieval == 'hybrid' else 0,
            'maximum_provider_attempts': len(cases) * 3 * multiplier,
            'document_embedding_attempts': 0, 'document_vectors_must_already_exist': retrieval == 'hybrid',
            'model': manifest['controls']['text_model'], 'label_review_required': True,
            'answer_review_required_for_quality': True, 'provider_calls': 0}


def review_template(manifest, cases):
    return {'dataset_sha256': manifest['cases_sha256'], 'reviewer': '', 'reviewed_at': '',
            'cases': [{'id': c['id'], 'case_sha256': digest(c), 'decision': 'pending', 'comments': ''} for c in cases]}


def validate_review(review, manifest, cases):
    if (review.get('dataset_sha256') != manifest['cases_sha256'] or not review.get('reviewer', '').strip()
            or not review.get('reviewed_at')):
        raise ValueError('Explicit human label review required')
    rows = review.get('cases', [])
    indexed = {r['id']: r for r in rows}
    if len(indexed) != len(rows):
        raise ValueError('Duplicate review entries')
    for case in cases:
        row = indexed.get(case['id'], {})
        if row.get('case_sha256') != digest(case) or row.get('decision') != 'accepted':
            raise ValueError('Labels must be reviewed without rewriting frozen inputs')


def score(report, review, binding, *, split_metrics=True):
    """Human semantic decisions are bound to actual, completed predictions."""
    if (report.get('mode') != 'live' or report.get('status') != 'completed' or not report.get('source_unchanged')
            or report.get('provider_failed') or review.get('report_sha256') != binding
            or not review.get('reviewer', '').strip() or not review.get('reviewed_at')):
        raise ValueError('Complete unchanged live run and human answer review required')
    answers = review.get('answers', [])
    judgments = {row['sample_id']: row for row in answers}
    rows = report['rows']
    if (len({r['sample_id'] for r in rows}) != len(rows)
            or len(judgments) != len(answers) or set(judgments) != {r['sample_id'] for r in rows}
            or any(r.get('verdict') not in ('correct', 'incorrect') or not r.get('reason', '').strip() for r in answers)):
        raise ValueError('Every blinded answer needs a semantic verdict and reason')
    paired, metrics = {}, {arm: {'correct_answer': [0, 0], 'correct_insufficient': [0, 0],
                                'overall': [0, 0], 'unsupported_answer': 0} for arm in ('single', 'bounded')}
    if len(rows) != len(report['cases']) * 2:
        raise ValueError('Incomplete paired predictions')
    cases = {c['id']: c for c in report['cases']}
    for row in rows:
        case, arm = cases[row['case_id']], row['arm']
        key = (row['case_id'], arm)
        if key in paired:
            raise ValueError('Duplicate paired prediction')
        correct = (row['status'] == case['expected_status'] and judgments[row['sample_id']]['verdict'] == 'correct'
                   and (row['status'] != 'answered' or bool(row['answer']['sources'])))
        paired[key] = correct
        name = 'correct_answer' if case['answerable'] else 'correct_insufficient'
        for metric in (name, 'overall'):
            metrics[arm][metric][0] += correct
            metrics[arm][metric][1] += 1
        metrics[arm]['unsupported_answer'] += row['status'] == 'answered' and not correct
    if set(paired) != {(key, arm) for key in cases for arm in ('single', 'bounded')}:
        raise ValueError('Mismatched paired slots')
    gains = sum(not paired[(key, 'single')] and paired[(key, 'bounded')] for key in cases)
    regressions = sum(paired[(key, 'single')] and not paired[(key, 'bounded')] for key in cases)
    result = {'quality_evaluated': True, 'metrics': metrics, 'gains': gains, 'regressions': regressions,
            'paired_cases': len(cases), 'net_delta_percentage_points': 100 * (gains - regressions) / len(cases),
            'claim': 'human_reviewed_authored_suite_only_not_external_benchmark'}
    if split_metrics:
        result['splits'] = {}
        for split in sorted({c['split'] for c in cases.values() if 'split' in c}):
            selected = [c for c in cases.values() if c['split'] == split]
            ids = {c['id'] for c in selected}
            subset = [r for r in rows if r['case_id'] in ids]
            samples = {r['sample_id'] for r in subset}
            result['splits'][split] = score({**report, 'cases': selected, 'rows': subset},
                {**review, 'answers': [r for r in answers if r['sample_id'] in samples]}, binding, split_metrics=False)
    return result
