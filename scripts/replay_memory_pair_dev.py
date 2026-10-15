"""Replay only development responses from a completed pair; no new model calls."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.memory.paired import compare
from evals.memory.protocol import load_suite, value_matches
from scripts.check_memory_baseline import block_external_network
ROOT = Path(__file__).resolve().parents[1]
Scenario = None  # Lazy import; importing this tool must not open an application DB.
COMMON = {'confirmation_applied', 'manual_confirmation_required', 'clarification',
          'condition_adoption', 'advice_api_success', 'no_unconfirmed_profile_write', 'no_actual_record_writes'}


@contextmanager
def offline_only(progress):
    saved = socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo
    try:
        block_external_network(progress)
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo = saved


def hashes():
    patterns = ('*.py', 'api/*.py', 'services/*.py', 'model/*.py', 'prompts/*.md',
                'data/knowledge/*.json', 'data/training/*.json')
    result = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for pattern in patterns for path in ROOT.glob(pattern) if path.is_file()}
    for name in ('scripts/replay_memory_pair_dev.py', 'scripts/replay_memory_responses.py',
                 'scripts/evaluate_memory.py', 'evals/memory/protocol.py', 'evals/memory/paired.py', 'static/coach.js'):
        result[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def runtime_types(directory):
    # app.py creates a default app at import time. Bind even that bootstrap to a
    # disposable DB and clear runtime overrides/credentials before importing it.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('FITNESS_') and key not in ('DASHSCOPE_API_KEY', 'OPENAI_API_KEY')}
    environment['FITNESS_DB_PATH'] = str(directory / 'module-default.sqlite3')
    with patch.dict(os.environ, environment, clear=True):
        from scripts.evaluate_memory import Scenario as current_scenario
        from scripts.replay_memory_responses import RecordedModel
    return Scenario or current_scenario, RecordedModel


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def common_checks(row, case, *, replayed=False):
    row = deepcopy(row)
    if len(row['steps']) != len(case['steps']) + 1:
        raise ValueError('Recorded scenario steps differ')
    for number, step in enumerate(row['steps']):
        if replayed and step['op'] == 'advice' and case['steps'][number]['expect']['status'] == 'ready':
            answer, expected = step.get('observed', {}), case['steps'][number]['expect']
            exercises = answer.get('exercises', [])
            step['checks']['condition_adoption'] = (
                answer.get('http') == 200 and answer.get('status') == 'ready'
                and value_matches(answer.get('equipment'), expected['values']) and bool(exercises)
                and all(set(item['equipment']) <= set(expected['resources']) for item in exercises))
        step['checks'] = {key: value for key, value in step['checks'].items() if key in COMMON}
    return row


def replay(receipt, output):
    receipt = Path(receipt).resolve()
    original = receipt.read_bytes()
    source = json.loads(original)
    manifest, cases = load_suite(ROOT / 'evals/memory/expressions_v2')
    version = source.get('versions', {}).get('candidate', {})
    if (source.get('protocol') != 'equipment-memory-paired-v1' or source.get('mode') != 'live'
            or source.get('status') != 'completed' or source.get('dataset_sha256') != manifest['sha256']
            or version.get('status') != 'completed' or not version.get('source_unchanged')):
        raise ValueError('Expected completed, unchanged paired live receipt with matching dataset')
    dev = [case for case in cases if case['split'] == 'dev']
    ids = {case['id'] for case in dev}
    # Filter before reading any response fields; held-out outputs are never replayed.
    rows = [row for row in version['cases'] if row['id'] in ids]
    indexed = {row['id']: row for row in rows}
    if len(indexed) != len(rows) or any(row['split'] != 'dev' for row in rows):
        raise ValueError('Duplicate or inconsistent development records')
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / 'artifacts').resolve()):
        raise ValueError('Replay output must stay in ignored artifacts')
    output.mkdir(parents=True, exist_ok=False)
    current_hashes, progress = hashes(), {'network_attempts': 0}
    before, after, unavailable = [], [], []
    report = {'mode': 'recorded_dev_replay', 'provider_calls': 0, 'real_model_quality': False,
              'dataset_sha256': manifest['sha256'], 'source_receipt_sha256': hashlib.sha256(original).hexdigest(),
              'recorded_candidate_commit': source['candidate_commit'], 'recorded_model': source['model'],
              'source_hashes': current_hashes, 'planned_dev_cases': len(dev),
              'quality_claim': 'fixed_response_handling_only_not_new_model_quality', 'auto_apply': False}
    write_json(output / 'run.json', report)
    with offline_only(progress), TemporaryDirectory(prefix='fitness-memory-bootstrap-') as bootstrap:
        scenario_type, recorded_model = runtime_types(Path(bootstrap))
        for case in dev:
            row = indexed.get(case['id'])
            outputs = [step.get('observed', {}).get('raw') for step in row['steps'] if step['op'] == 'message'] if row else []
            if len(outputs) != sum(step['op'] == 'message' for step in case['steps']) or any(
                    not item or item.get('invalid_schema') for item in outputs):
                unavailable.append({'id': case['id'], 'reason': 'missing_recorded_response'})
                continue
            model = recorded_model(outputs)
            with TemporaryDirectory(prefix='fitness-memory-dev-replay-') as directory:
                result = scenario_type(case, Path(directory), model, manifest['base_day'], auto_apply=False).run()
            if model.calls != len(outputs):
                raise ValueError('Recorded responses not fully consumed; replay stopped')
            before.append(common_checks(row, case))
            after.append(common_checks(result, case, replayed=True))
            with (output / 'cases.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')
    unchanged = current_hashes == hashes() and receipt.read_bytes() == original
    versions = {key: {'cases': rows, 'status': 'completed', 'source_unchanged': unchanged}
                for key, rows in (('baseline', before), ('candidate', after))}
    report.update(source_unchanged=unchanged, network_attempts=progress['network_attempts'],
                  replayed_cases=len(after), unavailable=unavailable,
                  comparison=compare(dev, versions, live=False, review_verified=False))
    write_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.receipt, args.output)
    print(json.dumps({key: result[key] for key in ('mode', 'provider_calls', 'network_attempts',
                                                 'source_unchanged', 'replayed_cases', 'unavailable', 'comparison')}))
    return 0 if result['source_unchanged'] and not result['unavailable'] and not result['network_attempts'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
