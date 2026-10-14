import json

import pytest

from evals.memory.protocol import load_suite
from scripts.replay_memory_responses import RecordedModel, replay


def test_recorded_model_ignores_new_prompt_and_gold_and_does_not_invent_more_responses():
    raw = {'scope': 'strength', 'command': '', 'clarification': 'none',
        'notes': [{'version': 1, 'avoid': [], 'preferences': [], 'training_caution': False,
                   'diet_caution': False, 'unresolved': False}]}
    model = RecordedModel([raw])
    model.step = {'expect': {'scope': 'not-a-real-scope'}}
    output = json.loads(model.generate(system_prompt='new prompt', message='{}'))
    assert output['scope'] == 'strength' and model.calls == 1
    with pytest.raises(ValueError, match='No recorded response'):
        model.generate(system_prompt='', message='{}')


def test_replay_keeps_missing_responses_separate_and_never_runs_holdout(tmp_path, monkeypatch):
    manifest, cases = load_suite()
    dev = [case for case in cases if case['split'] == 'dev']
    held = next(case for case in cases if case['split'] == 'holdout')
    folder = tmp_path / 'baseline'
    folder.mkdir()
    meta = {'dataset': manifest, 'mode': 'live', 'model': 'baseline', 'protocol': 'old', 'source_hashes': {}}
    (folder / 'run.json').write_text(json.dumps(meta), encoding='utf-8')
    (folder / 'summary.json').write_text(json.dumps({'source_unchanged': True}), encoding='utf-8')
    def baseline(case, raw):
        return {'id': case['id'], 'split': case['split'], 'family': case['family'], 'passed': False,
                'steps': [{'op': 'message', 'observed': {'raw': raw}, 'checks': {'clarification': False}}]}
    (folder / 'cases.jsonl').write_text('\n'.join(json.dumps(row) for row in (
        baseline(dev[0], {'recorded': True}), baseline(dev[1], None), baseline(held, {'recorded': True}))), encoding='utf-8')
    seen = []
    class FakeScenario:
        def __init__(self, case, directory, model, day):
            seen.append(case['id'])
            self.case, self.model = case, model
        def run(self):
            self.model.calls = 1
            return {**baseline(self.case, {'recorded': True}), 'passed': True}
    monkeypatch.setattr('scripts.replay_memory_responses.Scenario', FakeScenario)
    report = replay([folder], tmp_path / 'report')
    assert seen == [dev[0]['id']] and report['provider_calls'] == 0
    assert report['replayed_cases'] == 1 and len(report['unavailable']) == 59
    assert report['before']['cases']['total'] == report['after']['cases']['total'] == 1
    assert report['source_unchanged']
    with pytest.raises(ValueError, match='Duplicate'):
        replay([folder, folder], tmp_path / 'duplicate')
