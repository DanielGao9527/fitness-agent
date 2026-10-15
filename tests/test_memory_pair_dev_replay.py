import json
import socket

import pytest

from scripts import replay_memory_pair_dev as tool


def test_offline_guard_blocks_external_requests_and_restores_sockets_after_failure():
    connect, lookup = socket.socket.connect, socket.getaddrinfo
    progress = {'network_attempts': 0}
    with pytest.raises(RuntimeError, match='forbidden'):
        with tool.offline_only(progress):
            socket.create_connection(('198.51.100.1', 443))
    assert progress['network_attempts'] == 1
    assert socket.socket.connect is connect and socket.getaddrinfo is lookup


def test_dev_replay_reuses_raw_responses_excludes_holdout_and_never_claims_model_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, 'ROOT', tmp_path)
    manifest = {'sha256': 'frozen', 'base_day': '2026-09-29'}
    expected = {'status': 'ready', 'values': ['只有哑铃'], 'resources': ['floor', 'dumbbell']}
    cases = [{'id': name, 'split': split, 'family': 'fixture', 'steps': [
        {'op': 'message'}, {'op': 'advice', 'expect': expected}]} for name, split in
        [('dev-complete', 'dev'), ('dev-missing', 'dev'), ('held', 'holdout')]]
    raw = {'scope': 'strength', 'command': '', 'clarification': 'which_training', 'notes': [
        {'version': 1, 'avoid': [], 'preferences': [], 'training_caution': False,
         'diet_caution': False, 'unresolved': False}]}
    def row(case, response):
        return {**{key: case[key] for key in ('id', 'split', 'family')}, 'steps': [
            {'op': 'message', 'observed': {'raw': response}, 'checks': {'confirmation_applied': False}},
            {'op': 'advice', 'checks': {'condition_adoption': False}},
            {'op': 'record_boundary', 'checks': {'no_actual_record_writes': True}}]}
    receipt = tmp_path / 'original.json'
    content = json.dumps({'protocol': 'equipment-memory-paired-v1', 'mode': 'live', 'status': 'completed',
        'dataset_sha256': 'frozen', 'candidate_commit': 'original-commit', 'model': 'recorded-model',
        'versions': {'candidate': {'status': 'completed', 'source_unchanged': True,
            'cases': [row(cases[0], raw), row(cases[1], None), row(cases[2], {'must_not_be_read': True})]}}})
    receipt.write_text(content, encoding='utf-8')
    monkeypatch.setattr(tool, 'load_suite', lambda _: (manifest, cases))
    monkeypatch.setattr(tool, 'hashes', lambda: {'current': 'unchanged'})
    seen = []
    class FakeScenario:
        def __init__(self, case, directory, model, day, *, auto_apply):
            assert auto_apply is False
            seen.append(case['id'])
            self.case, self.model = case, model
        def run(self):
            assert json.loads(self.model.generate(system_prompt='new prompt', message='{}'))['clarification'] == 'which_training'
            result = row(self.case, raw)
            result['steps'][0]['checks']['confirmation_applied'] = True
            result['steps'][1]['observed'] = {'http': 200, 'status': 'ready', 'equipment': '只有哑铃',
                'exercises': [{'equipment': ['dumbbell', 'floor']}]}
            return result
    monkeypatch.setattr(tool, 'Scenario', FakeScenario)
    output = tmp_path / 'artifacts/replay'
    result = tool.replay(receipt, output)
    assert seen == ['dev-complete']
    assert result['provider_calls'] == result['network_attempts'] == 0
    assert result['unavailable'] == [{'id': 'dev-missing', 'reason': 'missing_recorded_response'}]
    assert result['source_unchanged'] and receipt.read_text(encoding='utf-8') == content
    score = result['comparison']['metrics']['condition_adoption']
    assert (score['baseline_passed'], score['candidate_passed'], score['total']) == (0, 1, 1)
    assert score['candidate_rate'] is None and result['comparison']['quality_evaluated'] is False
    with pytest.raises(FileExistsError):
        tool.replay(receipt, output)
    with pytest.raises(ValueError, match='ignored artifacts'):
        tool.replay(receipt, tmp_path / 'public-output')


@pytest.mark.parametrize('exercises,expected', [([], False), ([{'equipment': ['barbell']}], False),
                                              ([{'equipment': ['dumbbell']}], True)])
def test_replay_condition_adoption_requires_nonempty_allowed_exercises(exercises, expected):
    case = {'steps': [{'op': 'advice', 'expect': {'status': 'ready', 'values': ['只有哑铃'],
                                              'resources': ['floor', 'dumbbell']}}]}
    row = {'steps': [{'op': 'advice', 'checks': {}, 'observed': {'http': 200, 'status': 'ready',
            'equipment': '只有哑铃', 'exercises': exercises}}, {'op': 'record_boundary', 'checks': {}}]}
    assert tool.common_checks(row, case, replayed=True)['steps'][0]['checks']['condition_adoption'] is expected
    assert row['steps'][0]['checks'] == {}
