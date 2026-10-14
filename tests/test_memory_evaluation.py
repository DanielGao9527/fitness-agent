"""Evaluation integrity: no gold leakage, honest failures and bounded calls."""
import json
from pathlib import Path

import pytest

from evals.memory.protocol import load_suite, candidate_matches, advice_checks, summarize
from model.factory import ModelError
from scripts.evaluate_memory import MeasuredModel, run_evaluation


def test_frozen_suite_counts_and_family_separation():
    manifest,cases=load_suite()
    assert len(cases)==100 and manifest['provenance']=='self_authored_synthetic'
    assert len({c['family'] for c in cases})==20
    dev={c['family'] for c in cases if c['split']=='dev'}
    held={c['family'] for c in cases if c['split']=='holdout'}
    assert not dev&held
    assert sum(c['split']=='dev' for c in cases)==60
    assert all(2<=len(c['steps'])<=8 for c in cases)


def test_grading_rejects_wrong_scope_untraceable_source_and_wrong_final_device():
    expected={'scope':'day','values':['只有哑铃']}
    good={'source_text':'今天只有哑铃','source_version':1,'scope':'day','value':'只有哑铃'}
    assert candidate_matches(good,expected,'今天只有哑铃',1)
    for change in ({'scope':'long_term'},{'source_version':2},{'source_text':'只有哑铃'},{'value':'杠铃'}):
        assert not candidate_matches({**good,**change},expected,'今天只有哑铃',1)
    checks=advice_checks({'status':'ready','equipment':'只有哑铃','equipment_source':'day',
        'exercises':[{'equipment':['barbell']}]}, {'status':'ready','values':['只有哑铃'],'source':'day','resources':['floor','dumbbell']})
    assert checks['equipment_adoption'] and not checks['exercise_resources']
    assert summarize([{'family':'x','passed':False,'steps':[{'checks':{'test':False}}]}])['metrics']['test']=={'passed':0,'total':1,'rate':0.0}


def test_annotated_mode_is_explicit_and_never_uses_cloud(tmp_path,monkeypatch):
    monkeypatch.setattr('scripts.evaluate_memory.QwenTextModel.generate',lambda **_:pytest.fail('Unexpected network call'))
    manifest,cases=load_suite()
    selected=[c for c in cases if c['id'] in ('stable-01','scope_choice-01')]
    report=run_evaluation(selected,manifest,tmp_path/'report')
    assert report['cases']['passed']==2
    assert report['provider_calls']==0 and report['model_invocations']==2
    assert report['quality_claim']=='wiring_only_not_model_quality'
    with pytest.raises(FileExistsError):
        run_evaluation(selected,manifest,tmp_path/'report')


def test_live_wrapper_passes_only_app_prompt_counts_failed_attempt_and_never_retries(tmp_path):
    model=MeasuredModel('live',1,tmp_path/'calls.jsonl','test-model')
    model.case_id='synthetic'
    model.step={'expect':{'secret_gold_label':'NOT_FOR_MODEL'}}
    seen=[]
    class Provider:
        def generate(self,**kwargs):
            seen.append(kwargs)
            raise ModelError('MODEL_TIMEOUT','redacted')
    model.provider=Provider()
    for _ in range(2):
        with pytest.raises(ModelError):
            model.generate(system_prompt='APP PROMPT',message='{"messages":[]}')
    assert seen==[{'system_prompt':'APP PROMPT','message':'{"messages":[]}'}]
    assert model.calls==1
    events=[json.loads(line) for line in model.ledger.read_text().splitlines()]
    assert [x['event'] for x in events]==['attempt','failure']


def test_three_provider_failures_stop_run_without_hiding_cases(tmp_path,monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY','synthetic-only')
    def fail(*args,**kwargs):
        raise ModelError('MODEL_AUTH_ERROR','redacted',503)
    monkeypatch.setattr('scripts.evaluate_memory.QwenTextModel.generate',fail)
    manifest,cases=load_suite()
    report=run_evaluation(cases[:5],manifest,tmp_path/'failures',mode='live',max_calls=20)
    assert report['provider_calls']==3 and report['completed']==3
    assert report['not_run_ids']==[c['id'] for c in cases[3:5]]
    assert report['metrics']['raw_candidate_exact']['total']==3
    assert report['metrics']['equipment_adoption']['total']==3
    assert report['cases']['passed']==0


def test_live_requires_explicit_budget_before_any_output_or_call(tmp_path):
    manifest,cases=load_suite()
    with pytest.raises(ValueError,match='budget'):
        run_evaluation(cases[:1],manifest,tmp_path/'missing',mode='live')
    assert not (tmp_path/'missing').exists()


def test_report_rejects_duplicate_attempts_and_mixed_modes(tmp_path):
    from scripts.report_memory_evaluation import combine
    manifest,cases=load_suite()
    meta={'dataset':manifest,'mode':'annotated','model':None,'protocol':'test','source_hashes':{}}
    summary={'source_unchanged':True,'provider_latency':{'n':0}}
    row={'id':cases[0]['id'],'family':cases[0]['family'],'split':cases[0]['split'],
         'passed':False,'steps':[{'checks':{'raw_candidate_exact':False}}]}
    for name in ('a','b'):
        path=tmp_path/name
        path.mkdir()
        for filename,value in (('run.json',meta),('summary.json',summary)):
            (path/filename).write_text(json.dumps(value),encoding='utf-8')
        (path/'cases.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
    result=combine([tmp_path/'a'])
    assert result['all']['metrics']['raw_candidate_exact']['total']==1
    assert len(result['missing_ids'])==99
    with pytest.raises(ValueError,match='Duplicate'):
        combine([tmp_path/'a',tmp_path/'b'])
    (tmp_path/'b'/'run.json').write_text(json.dumps({**meta,'mode':'live'}),encoding='utf-8')
    with pytest.raises(ValueError,match='mix'):
        combine([tmp_path/'a',tmp_path/'b'])
