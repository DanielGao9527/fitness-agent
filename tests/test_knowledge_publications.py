"""Synthetic publication mechanics; never independent semantic quality evidence."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from database import Database
from rag.catalog import Corpus
from rag.rag_service import LocalKnowledgeRetriever
from services import knowledge_quality
from services.backups import create_backup, restore_backup, fingerprints, table_columns
from services.business_time import business_today
from services.knowledge_builds import FrozenKnowledge
from services.knowledge_feedback import encode
from services.knowledge_runtime import PublishedKnowledge
from services.knowledge_review import set_reviewer
from schema_fixtures import strip_publications_v22
from scripts.migrate_memory import migrate
from test_foundation import application, client, register
from test_knowledge import library, mutate
from test_coach_knowledge import knowledge_chat, answer
from test_coach import conversation, send, BASE
from test_knowledge_workbench import workbench, WORK
from test_knowledge_review import review, grant, revise, submit, check, approve
from test_knowledge_builds import builds, body, service
from test_knowledge_releases import release, register_package, check_release, registration


def quality_fixture(app, directory, monkeypatch):
    directory.mkdir()
    row = {'id':'synthetic-label-case','group_id':'synthetic-only','split':'test','answerable':True,
           'question':'营养标签 两份','relevant_chunk_ids':['fda-label:servings']}
    data = encode(row).encode()+b'\n'
    (directory/'cases.jsonl').write_bytes(data)
    protocol = {'policy':knowledge_quality.POLICY,'dataset':'cases.jsonl','dataset_sha256':hashlib.sha256(data).hexdigest(),
        'split':'test','review_status':'reviewed','label_review_sha256':'1'*64,'candidate_limit':20,'rrf_k':60,'limit':10,'min_mean_recall6':0.8,
        'notice':'Synthetic contract fixture, not human review or semantic accuracy evidence'}
    path=directory/'manifest.json';path.write_text(encode(protocol),encoding='utf-8')
    monkeypatch.setattr(knowledge_quality,'ROOT',directory)
    monkeypatch.setattr(knowledge_quality,'PROTOCOL_PATH',path)
    base=getattr(app.state.knowledge,'governance_base',app.state.knowledge)
    cache_dir=app.state.database.path.parent/'release-evaluation';cache_dir.mkdir(exist_ok=True)
    cache=knowledge_quality.cache_path(cache_dir,base.provider,[row])
    metadata={'identity':base.provider.identity,'dimensions':base.provider.dimensions,
        'questions':hashlib.sha256(json.dumps([row['question']],ensure_ascii=False).encode()).hexdigest()}
    cache.write_text(json.dumps({'manifest':metadata,'vectors':[[1.0,0.0]]}),encoding='utf-8')
    return path,cache


def accept(client,item,**changes):
    return client.post(WORK+'/releases/'+item['id']+'/approve',json={
        'version':item['version'],'binding_hash':item['report'].get('binding_hash','0'*64),'reviewed':True,**changes})


def switch(client,item=None,**changes):
    current=client.get(WORK+'/live').json()
    request={'client_id':str(uuid4()),'expected_revision':current['revision'],'action':'activate' if item else 'rollback',
        'release_id':item['id'] if item else None,'publication_version':item['approval']['version'] if item else None,'confirmed':True,**changes}
    return client.post(WORK+'/live/switch',json=request)


@pytest.fixture
def publication(release,tmp_path,monkeypatch):
    values,package=release
    client,app,provider,*_=values
    protocol,cache=quality_fixture(app,tmp_path/'quality',monkeypatch)
    item=register_package(client,package)
    response=check_release(client,item)
    assert response.status_code==200,response.text
    checked=response.json()
    assert checked['can_approve'] and checked['report']['passed'],checked
    response=accept(client,checked)
    assert response.status_code==200,response.text
    approved=response.json()
    base=app.state.knowledge
    app.state.knowledge=PublishedKnowledge(app.state.database,base,app.state.settings,app.state.knowledge_build_lock)
    return values,approved,protocol,cache


def test_manual_approval_atomic_switch_idempotent_retry_and_rollback(publication):
    (client,app,provider,*_),item,_,_=publication
    assert item['approval']['state']=='approved' and not item['published']
    assert client.get(WORK+'/live').json()['base_active']
    key=str(uuid4())
    response=switch(client,item,client_id=key)
    assert response.status_code==200,response.text
    assert response.json()['revision']==1 and response.json()['release_id']==item['id']
    assert app.state.knowledge.catalog()['version']=='knowledge-build-'+item['id']
    # Repeating the original request never switches a second time.
    request={'client_id':key,'expected_revision':0,'action':'activate','release_id':item['id'],
             'publication_version':1,'confirmed':True}
    assert client.post(WORK+'/live/switch',json=request).json()['reused']
    assert switch(client).json()['revision']==2
    replay=client.post(WORK+'/live/switch',json=request).json()
    assert replay['base_active'] and replay['applied_revision']==1
    assert app.state.knowledge.catalog()['version']!='knowledge-build-'+item['id'] and provider.calls==0
    assert switch(client,item,action='rollback').status_code==200
    assert client.get(WORK+'/live').json()['revision']==3


def test_real_unreviewed_protocol_cannot_be_approved_or_activated(release):
    (client,app,provider,*_),package=release
    item=register_package(client,package)
    response=check_release(client,item)
    assert response.status_code==200,response.text
    item=response.json()
    assert not item['can_approve'] and not item['report']['passed']
    assert accept(client,item).status_code==409
    assert switch(client,{**item,'approval':{'version':1}}).status_code==409
    assert client.get(WORK+'/live').json()['base_active'] and provider.calls==0


@pytest.mark.parametrize('change', [{'reviewed':False},{'reviewed':1},{'passed':True},{'user_id':1}])
def test_unconfirmed_or_client_supplied_quality_cannot_approve(publication,change):
    (client,app,_,*_),item,_,_=publication
    assert accept(client,item,**change).status_code==422


def test_competing_switches_and_stale_revisions_are_not_replayed(publication):
    (client,app,_,*_),item,_,_=publication
    def apply(_):
        return switch(client,item,expected_revision=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(apply,range(2)))
    assert sorted(response.status_code for response in responses)==[200,409]
    assert switch(client,expected_revision=0).status_code==409
    assert client.get(WORK+'/live').json()['revision']==1


@pytest.mark.parametrize('cause',['candidate','withdraw','protocol','cache','package','approver'])
def test_revocation_clears_live_pointer_and_cannot_resurrect(publication,cause):
    (client,app,provider,_,_,source,candidate,_),item,protocol,cache=publication
    assert switch(client,item).status_code==200
    path=None;original=None
    if cause=='candidate':
        assert revise(client,candidate,source).status_code==200
    elif cause=='withdraw':
        assert client.post(WORK+'/releases/'+item['id']+'/withdraw',json={'version':item['version']}).status_code==200
    elif cause=='approver':
        set_reviewer(app.state.database,'alice',False);set_reviewer(app.state.database,'alice',True)
    else:
        path=protocol if cause=='protocol' else cache if cause=='cache' else service(app).root/item['id']/'manifest.json'
        original=path.read_bytes();path.write_bytes(original+b' ')
    assert app.state.knowledge.catalog()['version']!='knowledge-build-'+item['id']
    assert client.get(WORK+'/live').json()['base_active']
    if path:path.write_bytes(original)
    assert switch(client,item).status_code==409 and provider.calls==0


def test_rollback_rejects_version_that_was_never_live(publication):
    (client,_,_,*_),item,_,_=publication
    assert switch(client,item,action='rollback').status_code==409


def test_read_and_switch_authentication_and_private_field_separation(publication):
    (client,app,_,*_),item,_,_=publication
    with TestClient(app) as other:
        assert other.get(WORK+'/live').status_code==401
        register(other,'bob')
        assert other.get(WORK+'/live').status_code==403
        assert accept(other,item).status_code==403
        assert other.post(WORK+'/live/switch',json={'client_id':str(uuid4()),'expected_revision':0,
            'action':'activate','release_id':item['id'],'publication_version':1,'confirmed':True}).status_code==403
    assert switch(client,item).status_code==200
    raw=json.dumps(client.get(WORK+'/live').json())
    assert not any(key in raw for key in ('actor_id','approver_id','password','conversation_id','question'))


def test_nonempty_publication_restore_keeps_head_and_revocation_triggers(publication,tmp_path):
    (client,app,_,*_),item,_,_=publication
    assert switch(client,item).status_code==200
    snapshot,restored=tmp_path/'snapshot.sqlite3',tmp_path/'restored.sqlite3'
    before=fingerprints(app.state.database.path)
    create_backup(app.state.database.path,snapshot);restore_backup(snapshot,restored)
    Database(restored).initialize()
    after=fingerprints(restored)
    assert all(after[name]==before[name] for name in before.keys()-{'sessions','meal_consents'})
    with Database(restored).connect() as connection:
        assert connection.execute('SELECT release_id FROM knowledge_live').fetchone()[0]==item['id']
        connection.execute('UPDATE knowledge_reviewers SET enabled=0,revision=revision+1')
        assert connection.execute('SELECT release_id FROM knowledge_live').fetchone()[0] is None


def test_schema21_migration_preserves_nonempty_old_tables(tmp_path):
    path=tmp_path/'old.sqlite3';Database(path).initialize()
    with sqlite3.connect(path) as connection:
        strip_publications_v22(connection)
        connection.execute('PRAGMA user_version=21')
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'synthetic','placeholder')")
    columns,before=table_columns(path),fingerprints(path)
    result=migrate(path,tmp_path/'backup.sqlite3')
    assert result['schema']==22 and result['tables']==38
    assert fingerprints(path,columns=columns)==before


def test_alias_ranking_keeps_actual_slots_and_does_not_refill_top_six():
    aliases={'excerpt':'target'}
    assert knowledge_quality.score(['excerpt','target','x'],{'target'},aliases)=={'recall6':1.0,'mrr10':1.0}
    ids=['x1','x2','x3','x4','x5','x6','excerpt']
    assert knowledge_quality.score(ids,{'target'},aliases)=={'recall6':0.0,'mrr10':1/7}


def test_fixed_planning_sources_do_not_lock_or_depend_on_publication(publication):
    (client,app,_,*_),item,_,_=publication
    assert switch(client,item).status_code==200
    from services.training_plans import TrainingPlanService
    from services.meal_plans import MealPlanService
    from services.training_recommendations import TrainingRecommendationService
    with app.state.database.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for kind in (TrainingPlanService,MealPlanService,TrainingRecommendationService):
            planning=kind(app.state.database,1,app.state.knowledge)
            assert planning.retriever is app.state.knowledge.governance_base
            assert planning.evidence()


def test_approver_revocation_is_independent_of_registrant(release,tmp_path,monkeypatch):
    (client,app,_,*_),package=release
    quality_fixture(app,tmp_path/'quality',monkeypatch)
    item=check_release(client,register_package(client,package)).json()
    with TestClient(app) as other:
        register(other,'bob');grant(app,'bob')
        response=accept(other,item);assert response.status_code==200,response.text
        item=response.json()
    assert switch(client,item).status_code==200
    set_reviewer(app.state.database,'bob',False)
    assert client.get(WORK+'/live').json()['base_active']
    assert client.get(WORK+'/review-queue').json()['items'][0]['status']=='approved'


def test_query_preparation_plan_never_encodes_or_reads_business_rows(publication,monkeypatch,capsys):
    (client,app,provider,*_),item,protocol,cache=publication
    from scripts import prepare_release_queries as script
    monkeypatch.setattr(script.Settings,'from_env',lambda:app.state.settings)
    before=fingerprints(app.state.database.path)
    assert script.main(['--database',str(app.state.database.path)])==0
    assert json.loads(capsys.readouterr().out)['provider_calls']==0
    assert fingerprints(app.state.database.path)==before and provider.calls==0


def test_failed_query_encoding_needs_new_explicit_attempt_and_preserves_private_rows(publication,monkeypatch,capsys):
    (client,app,_,*_),item,_,_=publication
    from scripts import prepare_release_queries as script
    from test_hybrid_retrieval import FakeEmbedding
    from model.factory import ModelError
    providers=[]
    def provider(*args,**kwargs):
        value=FakeEmbedding('cli-synthetic-new')
        if not providers:value.error=ModelError('SYNTHETIC_FAILURE','synthetic failure')
        providers.append(value);return value
    monkeypatch.setattr(script.Settings,'from_env',lambda:app.state.settings)
    monkeypatch.setattr(script,'QwenEmbeddingProvider',provider)
    before=fingerprints(app.state.database.path)
    args=['--database',str(app.state.database.path),'--live','--max-calls','1','--max-input-chars','1000']
    assert script.main(args)==1 and providers[-1].calls==1
    assert script.main(args)==1 and providers[-1].calls==0
    assert script.main(args+['--acknowledge-previous-attempt'])==0 and providers[-1].calls==1
    assert script.main(args)==0 and providers[-1].calls==0
    assert fingerprints(app.state.database.path)==before
    capsys.readouterr()


def test_checker_role_aba_during_quality_evaluation_cannot_save_report(release,tmp_path,monkeypatch):
    (client,app,provider,*_),package=release
    quality_fixture(app,tmp_path/'quality',monkeypatch)
    item=register_package(client,package)
    original=knowledge_quality.evaluate
    def changed(*args,**kwargs):
        report=original(*args,**kwargs)
        set_reviewer(app.state.database,'bob',False);set_reviewer(app.state.database,'bob',True)
        return report
    monkeypatch.setattr(knowledge_quality,'evaluate',changed)
    with TestClient(app) as other:
        register(other,'bob');grant(app,'bob')
        response=check_release(other,item)
        assert response.status_code==409,response.text
    listing=client.get(WORK+'/releases').json()['items'][0]
    assert listing['version']==1 and listing['report']=={} and provider.calls==0


def test_zero_recall_does_not_pass_even_when_both_rankings_are_equally_bad(publication,monkeypatch):
    (client,app,_,*_),item,_,_=publication
    from rag.hybrid import HybridKnowledgeRetriever
    from rag.rag_service import RetrievalSnapshot
    monkeypatch.setattr(HybridKnowledgeRetriever,'search_snapshot',lambda *a,**k:RetrievalSnapshot([],{'mode':'hybrid'}))
    base=app.state.knowledge.governance_base
    manifest=json.loads((service(app).root/item['id']/'manifest.json').read_bytes())
    candidate=FrozenKnowledge(service(app).root/item['id'],manifest['tokenizer'])
    report=knowledge_quality.evaluate(base,candidate,manifest,app.state.database.path.parent/'release-evaluation',business_today())
    assert report['mean_recall6']==0 and not report['passed'] and 'hybrid_ranking_quality' in report['blockers']


def test_public_version_does_not_expire_just_because_build_date_was_yesterday(publication,monkeypatch):
    (client,app,_,*_),item,_,_=publication
    assert switch(client,item).status_code==200
    from datetime import timedelta
    import services.knowledge_releases as releases
    import rag.rag_service as rag
    tomorrow=business_today()+timedelta(days=1)
    monkeypatch.setattr(releases,'business_today',lambda:tomorrow)
    monkeypatch.setattr(rag,'business_today',lambda:tomorrow)
    assert app.state.knowledge.catalog()['version']=='knowledge-build-'+item['id']
    assert client.get(WORK+'/live').json()['release_id']==item['id']


def test_temporary_source_outage_preserves_live_status_and_allows_withdrawal(publication,monkeypatch):
    (client,app,_,*_),item,_,_=publication
    assert switch(client,item).status_code==200
    from rag.rag_service import KnowledgeUnavailable
    def unavailable(*args,**kwargs):raise KnowledgeUnavailable('synthetic outage')
    monkeypatch.setattr(app.state.knowledge.governance_base,'all_chunks',unavailable)
    live=client.get(WORK+'/live')
    assert live.status_code==200 and live.json()['source_unavailable']
    assert client.post(WORK+'/releases/'+item['id']+'/withdraw',json={'version':item['version']}).status_code==200
    assert client.get(WORK+'/live').json()['base_active']


def test_pointer_aba_while_model_is_answering_rejects_identical_old_sources(publication,monkeypatch):
    (client,app,_,*_),item,_,_=publication
    assert switch(client,item).status_code==200
    from test_knowledge_answers import AnswerModel
    import api.coach as coach_api
    model=AnswerModel()
    def changed():
        assert switch(client).status_code==200
        assert switch(client,item).status_code==200
    model.on_call=changed
    monkeypatch.setattr(coach_api,'create_knowledge_model',lambda _:model)
    data=send(client,conversation(client),'力量训练一般一周做几次').json()
    result=answer(client,data)
    assert result.status_code==409 and result.json()['detail']['code']=='KNOWLEDGE_CHANGED'


def test_excerpt_reaches_real_chat_and_rollback_hides_saved_answer(builds,tmp_path,monkeypatch):
    client,app,provider,_,_,source,candidate,_=builds
    candidate['content']=candidate['content'][:20]
    updated=revise(client,candidate,source).json()
    accepted=approve(client,check(client,submit(client,updated).json())).json()
    request=body(accepted)
    with app.state.database.connect() as connection:
        selected=[dict(connection.execute('SELECT * FROM knowledge_submissions WHERE id=?',(accepted['id'],)).fetchone())]
    corpus=Corpus.model_validate_json(app.state.knowledge.lexical.source_path.read_bytes())
    generated,_,_=service(app)._corpus(corpus,selected,business_today(),request['client_id'])
    directory=tmp_path/'synthetic-cache';directory.mkdir()
    (directory/'sources.json').write_text(generated.model_dump_json(),encoding='utf-8')
    lexical=LocalKnowledgeRetriever(directory/'sources.json',directory/'index.sqlite3',tokenizer=app.state.knowledge.lexical.tokenizer)
    app.state.knowledge.vector_store.build(lexical,provider);provider.calls=0
    package=client.post(WORK+'/builds',json=request).json()
    assert package.get('new_chunks')==1,package
    quality_fixture(app,tmp_path/'quality',monkeypatch)
    item=register_package(client,package)
    checked=check_release(client,item).json()
    response=accept(client,checked);assert response.status_code==200,response.text
    item=response.json();base=app.state.knowledge
    app.state.knowledge=PublishedKnowledge(app.state.database,base,app.state.settings,app.state.knowledge_build_lock)
    assert switch(client,item).status_code==200
    manifest=json.loads((service(app).root/item['id']/'manifest.json').read_bytes())
    alias=manifest['selection'][0]['built_chunk_id']
    from test_knowledge_answers import AnswerModel
    import api.coach as coach_api
    model=AnswerModel();model.output=json.dumps({'status':'answered','chunk_ids':[alias]})
    monkeypatch.setattr(coach_api,'create_knowledge_model',lambda _:model)
    data=send(client,conversation(client),'力量训练一般一周做几次').json()
    from test_knowledge_answers import calls
    before_calls=calls(app)
    response=answer(client,data);assert response.status_code==200,response.text
    saved=response.json()['turns'][-1]['response']['knowledge']
    assert saved['status']=='answered' and saved['sources'][0]['chunk_id']==alias
    assert saved['retrieval']['release_revision']==1
    assert calls(app)==before_calls+2 and provider.calls==1
    assert switch(client).status_code==200
    assert not app.state.knowledge.get_chunks([alias])
    history=client.get(BASE+'/'+data['id']).json()['turns'][-1]['response']['knowledge']
    assert history['status']=='sources_changed' and not history['sources']
    assert switch(client,item,action='rollback').status_code==200
    # Same content/UUID after rollback cannot revive an answer from an older pointer revision.
    history=client.get(BASE+'/'+data['id']).json()['turns'][-1]['response']['knowledge']
    assert history['status']=='sources_changed' and not history['sources']
