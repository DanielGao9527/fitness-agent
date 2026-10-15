"""Isolated versioned memory evaluation: annotated wiring or bounded live Qwen.

Every case uses a disposable private database. Live generation sees only the
normal app prompt, never gold labels. Failed calls are counted; no retries.
"""
import argparse
from contextlib import ExitStack
from datetime import date, timedelta, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
from uuid import uuid4

if __name__ == '__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import create_app
from config import ROOT, Settings, DEFAULT_QWEN_MODEL
from evals.memory.protocol import load_suite, candidate_matches, advice_checks, summarize, latency_summary
from model.factory import ModelError
from model.qwen import QwenTextModel
from schemas import CoachUnderstanding
from services import business_time

BASE='/api/coach/conversations'
PROTOCOL='equipment-memory-eval-v1'


def write_json(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def append_event(path,event):
    with path.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(event,ensure_ascii=False)+'\n')
        stream.flush()
        os.fsync(stream.fileno())


class MeasuredModel:
    def __init__(self,mode,limit,ledger,model_name):
        self.mode,self.limit,self.ledger,self.model_name=mode,limit,ledger,model_name
        self.calls=0
        self.invocations=0
        self.latencies=[]
        self.failures=0
        self.last=None
        self.step=None
        self.during=None
        self.case_id=None

    def generate(self,*,system_prompt,message):
        if self.mode=='live' and (self.calls>=self.limit or self.failures>=3):
            raise ModelError('EVAL_BUDGET_STOP','Evaluation call budget or provider failure limit reached',429)
        data=json.loads(message)
        self.invocations+=1
        self.last=None
        if self.during:
            self.during()
        if self.mode=='annotated':
            expected=self.step['expect']
            turn=data['messages'][-1]
            result={'scope':'strength','command':'','clarification':'none','notes':[
                {'version':x['version'],'avoid':[],'preferences':[],'training_caution':False,
                 'diet_caution':False,'unresolved':False} for x in data['messages']]}
            if expected['outcome'] in ('candidate','stale'):
                result['equipment_memory']={'source_version':turn['version'],'source_text':turn['message'],
                    'value':expected['values'][0],'scope':expected['scope']}
            else:
                result['clarification']='which_training'
            raw=json.dumps(result,ensure_ascii=False)
        else:
            self.calls+=1
            # Durable before dispatch: interrupted attempts remain visible.
            append_event(self.ledger,{'event':'attempt','call':self.calls,'case':self.case_id,
                'request_sha256':hashlib.sha256((system_prompt+message).encode()).hexdigest()})
            started=time.monotonic()
            try:
                raw=self.provider.generate(system_prompt=system_prompt,message=message)
                self.failures=0
            except ModelError as error:
                self.failures+=1
                append_event(self.ledger,{'event':'failure','call':self.calls,'code':error.code})
                raise
            finally:
                self.latencies.append(time.monotonic()-started)
        try:
            self.last=CoachUnderstanding.model_validate_json(raw).model_dump()
        except ValueError:
            self.last={'invalid_schema':True}
        if self.mode=='live':
            append_event(self.ledger,{'event':'response','call':self.calls,'seconds':self.latencies[-1],
                'parsed':self.last})
        return raw


class Scenario:
    def __init__(self,case,directory,measured,base_day,*,auto_apply=True):
        self.case,self.model=case,measured
        self.auto_apply=auto_apply
        self.day=date.fromisoformat(base_day)
        self.sessions={}
        self.clients={}
        self.client_stack=ExitStack()
        self.settings=Settings(database_path=directory/'personal.sqlite3',
            knowledge_index_path=directory/'knowledge.sqlite3', retrieval_mode='lexical',
            model_provider='qwen',qwen_model=measured.model_name,
            qwen_api_key=os.getenv('DASHSCOPE_API_KEY','') if measured.mode=='live' else 'synthetic-only',
            coach_enabled=True,training_plan_enabled=True,guest_enabled=False,
            ai_user_daily_limit=1000,ai_global_daily_limit=1000)
        self.app=create_app(self.settings)
        self.model.provider=QwenTextModel(self.settings) if measured.mode=='live' else None

    def client(self,actor='alice'):
        if actor not in self.clients:
            client=self.client_stack.enter_context(TestClient(self.app))
            self.clients[actor]=client
            self.checked(client.post('/api/auth/register',json={'username':actor,'password':'Synthetic-memory-eval-42'}))
            if actor=='alice':
                self.checked(client.put('/api/profile',json=self.case['seed_profile']))
        return self.clients[actor]

    @staticmethod
    def checked(response):
        if response.status_code>=400:
            raise RuntimeError(f'EVAL_API_{response.status_code}')
        return response.json() if response.content else None

    def current(self,name,actor='alice'):
        client=self.client(actor)
        key=(actor,name)
        if key not in self.sessions:
            self.sessions[key]=self.checked(client.post(BASE,json={'client_id':str(uuid4()),'day':self.day.isoformat()}))['id']
        return self.checked(client.get(f'{BASE}/{self.sessions[key]}'))

    def send(self,name,text,actor='alice'):
        current=self.current(name,actor)
        return self.checked(self.client(actor).post(f"{BASE}/{current['id']}/messages",json={
            'client_id':str(uuid4()),'version':current['version'],'message':text}))

    def mutation(self,step):
        if step['op']=='profile':
            self.checked(self.client().put('/api/profile',json={'equipment':step['value']}))
        elif step['op']=='advance':
            self.day+=timedelta(days=step['days'])
        else:
            raise ValueError('Unsupported concurrent mutation')

    def run_step(self,step):
        op=step['op']
        checks={}
        observed={}
        client=self.client()
        if op=='message':
            data=self.send(step['session'],step['text'])
            before=client.get('/api/profile').json()['equipment']
            day_revision=data['context']['day_equipment']['revision']
            self.model.step,self.model.last=step,None
            self.model.during=(lambda:self.mutation(step['during'])) if step['during'] else None
            response=client.post(f"{BASE}/{data['id']}/understand",json={
                'version':data['version'],'client_id':str(uuid4()),'auto_apply':self.auto_apply}) if data['pending'] else None
            self.model.during=None
            status=response.status_code if response is not None else 200
            output=response.json() if response is not None else data
            review=output.get('review') or {}
            expected=step['expect']
            raw_candidate=(self.model.last or {}).get('equipment_memory')
            observed={'http':status,'raw':self.model.last,'review_status':review.get('status'),
                      'candidate':review.get('equipment_memory'),'error':output.get('detail') if status>=400 else None}
            if expected['outcome']=='stale':
                checks['concurrent_stale_rejection']=status==409
            elif expected['outcome']=='clarify':
                checks['clarification']=status==200 and review.get('status')=='needs_input'
                checks['no_false_candidate']=not raw_candidate and not review.get('equipment_memory')
            else:
                checks['raw_candidate_exact']=candidate_matches(raw_candidate,expected,step['text'],data['version'])
                checks['accepted_candidate_exact']=candidate_matches(review.get('equipment_memory'),expected,step['text'],data['version'])
                checks['manual_confirmation_required']=status==200 and bool(output.get('pending')) and review.get('status')=='ready'
            expected_profile=step['during']['value'] if step.get('during',{} ) and step['during']['op']=='profile' else before
            checks['no_unconfirmed_profile_write']=client.get('/api/profile').json()['equipment']==expected_profile
            checks['no_unconfirmed_day_write']=self.current(step['session'])['context']['day_equipment']['revision']==day_revision
            if step['confirm'] and review.get('status')=='ready' and output.get('pending'):
                body={'version':output['version'],'review_id':review['id'],'reviewed':True}
                if step['choice']:
                    body['equipment_scope']=step['choice']
                accepted=client.post(f"{BASE}/{data['id']}/confirm-understanding",json=body)
                checks['confirmation_applied']=accepted.status_code==200
                observed['confirm_http']=accepted.status_code
            elif step['confirm']:
                checks['confirmation_applied']=False
        elif op=='advice':
            actor=step.get('actor','alice')
            data=self.send(step['session'],'今天练什么',actor)
            response=self.client(actor).post(f"{BASE}/{data['id']}/recommend-training",json={
                'client_id':str(uuid4()),'version':data['version']})
            answer=response.json()['turns'][-1]['response'].get('training_recommendation',{}) if response.status_code==200 else {}
            checks=advice_checks(answer,step['expect'])
            checks['advice_api_success']=response.status_code==200
            observed={'http':response.status_code,'status':answer.get('status'),'equipment':answer.get('equipment'),
                'source':answer.get('equipment_source'),'exercises':[{'id':x['id'],'equipment':x['equipment']} for x in answer.get('exercises',[])]}
        elif op in ('advance','profile'):
            self.mutation(step)
        elif op=='delete':
            current=self.current(step['session'])
            self.checked(client.request('DELETE',f"{BASE}/{current['id']}",json={'version':current['version']}))
        elif op=='stale':
            current=self.current(step['session'])
            answers=[turn['response']['training_recommendation'] for turn in current['turns'] if 'training_recommendation' in turn['response']]
            checks['superseded_advice_stale']=bool(answers) and all(a['stale'] for a in answers)
        elif op=='foreign_access':
            current=self.current(step['session'])
            other=self.client('bob')
            checks['account_isolation']=(other.get(f"{BASE}/{current['id']}").status_code==404 and
                other.request('DELETE',f"{BASE}/{current['id']}",json={'version':current['version']}).status_code==404)
        elif op=='restart':
            cookies={name:dict(client.cookies) for name,client in self.clients.items()}
            self.client_stack.close()
            self.client_stack=ExitStack()
            self.app=create_app(self.settings)
            self.clients={}
            for name,values in cookies.items():
                client=self.client_stack.enter_context(TestClient(self.app))
                client.cookies.update(values)
                self.clients[name]=client
        else:
            raise ValueError('Unknown scenario operation')
        return {'op':op,'checks':checks,'observed':observed}

    def run(self):
        rows=[]
        try:
            with patch.object(business_time,'business_today',lambda:self.day), patch('api.coach.create_coach_model',lambda _:self.model):
                for step in self.case['steps']:
                    try:
                        rows.append(self.run_step(step))
                    except Exception as error:
                        # Do not serialize provider exceptions/credentials or silently
                        # drop a failed case from metric denominators.
                        failed={'step_completed':False}
                        if step['op']=='advice':
                            failed.update({name:False for name in advice_checks({},step['expect'])})
                            failed['advice_api_success']=False
                        elif step['op']=='message':
                            outcome=step['expect']['outcome']
                            names=(['raw_candidate_exact','accepted_candidate_exact','manual_confirmation_required'] if outcome=='candidate'
                                else ['concurrent_stale_rejection'] if outcome=='stale' else ['clarification','no_false_candidate'])
                            failed.update({name:False for name in names})
                            failed.update(no_unconfirmed_profile_write=False,no_unconfirmed_day_write=False)
                            if step['confirm']:
                                failed['confirmation_applied']=False
                        rows.append({'op':step['op'],'checks':failed,'error_type':type(error).__name__})
                with self.app.state.database.connect() as connection:
                    counts={table:connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                            for table in ('meals','workouts')}
                rows.append({'op':'record_boundary','checks':{'no_actual_record_writes':not any(counts.values())}})
        finally:
            self.client_stack.close()
        return {'id':self.case['id'],'family':self.case['family'],'split':self.case['split'],
                'passed':all(all(row['checks'].values()) for row in rows),'steps':rows}


def source_hashes():
    patterns=['*.py','api/*.py','services/*.py','model/*.py','prompts/*.md','data/knowledge/*.json','data/training/*.json']
    paths=sorted({p for pattern in patterns for p in ROOT.glob(pattern) if p.is_file()})
    paths+= [Path(__file__),ROOT/'evals/memory/protocol.py']
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def run_evaluation(cases,manifest,output,*,mode='annotated',max_calls=0,model_name=DEFAULT_QWEN_MODEL):
    if mode not in ('annotated','live') or mode=='live' and not 1<=max_calls<=200:
        raise ValueError('Live evaluation requires an explicit 1..200 call budget')
    if mode=='live' and not os.getenv('DASHSCOPE_API_KEY','').strip():
        raise ValueError('DASHSCOPE_API_KEY missing; live evaluation not run')
    output=Path(output).resolve()
    if output.is_relative_to((ROOT/'static').resolve()):
        raise ValueError('Evaluation artifacts must not be served as static content')
    output.mkdir(parents=True,exist_ok=False)
    hashes=source_hashes()
    metadata={'protocol':PROTOCOL,'mode':mode,'model':model_name if mode=='live' else None,
        'dataset':manifest,'selected_ids':[c['id'] for c in cases],'max_provider_calls':max_calls,
        'source_hashes':hashes,'started_at':datetime.now(timezone.utc).isoformat(),
        'tokens_and_cost':'not_collected','real_model_quality':mode=='live',
        'comparison_baseline':None,'temporary_databases':True,'public_retrieval':'lexical; no embedding calls'}
    write_json(output/'run.json',metadata)
    measured=MeasuredModel(mode,max_calls,output/'calls.jsonl',model_name)
    results=[]
    for case in cases:
        if mode=='live' and (measured.calls>=max_calls or measured.failures>=3):
            break
        measured.case_id=case['id']
        with TemporaryDirectory(prefix='fitness-memory-eval-') as directory:
            result=Scenario(case,Path(directory),measured,manifest['base_day']).run()
        results.append(result)
        append_event(output/'cases.jsonl',result)
        summary={**summarize(results),'mode':mode,'selected':len(cases),'completed':len(results),
            'quality_claim':'synthetic_suite_only' if mode=='live' else 'wiring_only_not_model_quality',
            'model_invocations':measured.invocations,
            'provider_calls':measured.calls,'provider_latency':latency_summary(measured.latencies),
            'not_run_ids':[c['id'] for c in cases[len(results):]],
            'source_unchanged':source_hashes()==hashes}
        write_json(output/'summary.json',summary)
        print(json.dumps({'case':case['id'],'passed':result['passed'],'completed':len(results),'calls':measured.calls}),flush=True)
    if not results:
        raise ValueError('No cases selected')
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['annotated','live'],default='annotated')
    parser.add_argument('--suite',type=Path,help='Frozen cases.jsonl/manifest.json directory; defaults to the original v1 suite')
    parser.add_argument('--split',choices=['dev','holdout','all'],default='dev')
    parser.add_argument('--case',action='append',default=[])
    parser.add_argument('--max-calls',type=int,default=0)
    parser.add_argument('--model',default=DEFAULT_QWEN_MODEL)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args(argv)
    manifest,cases=load_suite(args.suite)
    selected=[c for c in cases if (args.split=='all' or c['split']==args.split) and (not args.case or c['id'] in args.case)]
    if not selected or set(args.case)-{c['id'] for c in selected}:
        parser.error('No cases or case IDs outside the selected split')
    if args.plan:
        print(json.dumps({'cases':len(selected),'maximum_model_attempts':sum(s['op']=='message' for c in selected for s in c['steps']),
                          'dataset_sha256':manifest['sha256']},indent=2))
        return 0
    if args.output is None:
        parser.error('--output must name a new directory')
    try:
        summary=run_evaluation(selected,manifest,args.output,mode=args.mode,max_calls=args.max_calls,model_name=args.model)
    except (ValueError,OSError) as error:
        print(type(error).__name__+': evaluation could not start or persist results',file=sys.stderr)
        return 2
    return 0 if not summary['not_run_ids'] and summary['source_unchanged'] else 2


if __name__=='__main__':
    raise SystemExit(main())
