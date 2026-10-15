import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import KnowledgeUnavailable
from rag.vector_store import VectorStore
from schema_fixtures import strip_review_v20
from scripts.knowledge_reviewer import main as role_cli
from scripts.migrate_memory import migrate
from services.backups import create_backup, fingerprints, restore_backup, table_columns
from services.knowledge_review import set_reviewer
from test_foundation import application, client, register, PASSWORD
from test_knowledge import library, mutate
from test_coach_knowledge import knowledge_chat
from test_knowledge_workbench import workbench, prepared, proposal, WORK
from test_knowledge_answers import calls


@pytest.fixture
def review(workbench):
    client, app, model, data, feedback, source = prepared(workbench)
    candidate = client.post(WORK + '/candidates', json=proposal(feedback, source)).json()
    return client, app, model, data, feedback, source, candidate


def submit(client, candidate, **change):
    return client.post(WORK + '/candidates/' + candidate['id'] + '/submit', json={
        'version': candidate['version'], 'client_id': str(uuid4()), 'share_public_excerpt': True, **change})


def grant(app, username='alice'):
    return set_reviewer(app.state.database, username, True)


def check(client, submission):
    result = client.post(WORK + '/review-queue/' + submission['id'] + '/check', json={'version': submission['version']})
    assert result.status_code == 200, result.text
    return result.json()


def approval(item, **change):
    return {'version': item['version'], 'gate_id': item['gate']['id'],
            'binding_hash': item['gate']['binding_hash'], 'reviewed': True, **change}


def approve(client, item, **change):
    return client.post(WORK + '/review-queue/' + item['id'] + '/approve', json=approval(item, **change))


def revise(client, candidate, source):
    return client.put(WORK + '/candidates/' + candidate['id'], json={'version': candidate['version'],
        'content': candidate['content'], 'source_chunk_id': source['source']['chunk_id'],
        'source_hash': source['source_hash'], 'reviewed': True})


def test_explicit_submission_is_idempotent_and_queue_does_not_export_private_data(review):
    client, app, model, data, feedback, _, candidate = review
    body = {'version': 1, 'client_id': str(uuid4()), 'share_public_excerpt': True}
    endpoint = WORK + '/candidates/' + candidate['id'] + '/submit'
    result = client.post(endpoint, json=body)
    assert result.status_code == 201, result.text
    item = result.json()
    assert client.post(endpoint, json=body).json() == item
    assert client.post(endpoint, json={**body, 'version': 2}).status_code == 409
    assert submit(client, candidate).status_code == 409
    assert not item['published'] and not item['used_as_knowledge']
    assert client.get(WORK + '/review-queue').status_code == 403
    assert client.get(WORK + '/permissions').json() == {'reviewer': False, 'publication_enabled': False}
    grant(app)
    queue = client.get(WORK + '/review-queue').json()['items']
    assert len(queue) == 1 and 'candidate_id' not in queue[0]
    forbidden = {'user_id','owner_id','username','comment','question','answer','feedback_id','review_note','actor_id'}
    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(child) for child in value))
        return set()
    assert not (keys(queue) & forbidden)
    assert feedback['comment'] not in json.dumps(queue, ensure_ascii=False)
    assert data['id'] not in json.dumps(queue) and candidate['id'] not in json.dumps(queue)
    assert len(model.messages) == calls(app) == 1


@pytest.mark.parametrize('change', [{'share_public_excerpt':False},{'share_public_excerpt':1},{'user_id':2},
    {'status':'approved'},{'content':'private rewrite'},{'source_hash':'0'*64},{'version':True}])
def test_submission_requires_explicit_sharing_and_server_owned_content(review, change):
    client, _, _, _, _, _, candidate = review
    assert submit(client, candidate, **change).status_code == 422
    assert client.get(WORK + '/submissions').json()['items'] == []


def test_local_checks_manual_pre_review_and_restart_never_publish(review):
    client, app, model, _, _, _, candidate = review
    before = app.state.knowledge.export_documents()
    item = submit(client, candidate).json()
    grant(app)
    checked = check(client, item)
    assert checked['status'] == 'checked' and checked['gate']['passed']
    report = checked['gate']['report']
    assert report['semantic_quality'] == 'not_evaluated' and report['release_regression'] == 'not_run'
    assert not report['publication_allowed'] and report['real_provider_calls'] == 0
    assert approve(client, checked, reviewed=False).status_code == 422
    assert approve(client, checked, reviewed=1).status_code == 422
    assert approve(client, checked, binding_hash='0'*64).status_code == 409
    assert approve(client, checked, gate_id=str(uuid4())).status_code == 409
    accepted = approve(client, checked)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['status'] == 'approved' and not accepted.json()['published']
    assert approve(client, checked).status_code == 409
    assert app.state.knowledge.export_documents() == before and calls(app) == len(model.messages) == 1
    restarted = create_app(app.state.settings)
    restarted.state.knowledge = app.state.knowledge
    with TestClient(restarted) as other:
        assert other.post('/api/auth/login', json={'username':'alice','password':PASSWORD}).status_code == 200
        assert other.get(WORK + '/submissions').json()['items'][0]['status'] == 'approved'
        assert other.get(WORK + '/permissions').json()['reviewer']
    assert client.post(WORK + '/candidates/' + candidate['id'] + '/publish', json={'version':1}).status_code == 404


def test_reviewer_is_not_private_owner_and_cannot_grant_roles_over_http(review):
    client, app, _, _, _, _, candidate = review
    item = submit(client, candidate).json()
    with TestClient(app) as other:
        assert other.get(WORK + '/submissions').status_code == 401
        register(other,'bob')
        assert other.get(WORK + '/submissions').json()['items'] == []
        assert other.post(WORK + '/review-queue/' + item['id'] + '/check', json={'version':1}).status_code == 403
        assert submit(other,candidate).status_code == 404
        grant(app,'bob')
        checked = check(other,item)
        assert approve(other,checked).status_code == 200
        assert other.get(WORK + '/feedback').json()['items'] == []
        assert other.get(WORK + '/candidates').json()['items'] == []
        assert other.post(WORK + '/submissions/' + item['id'] + '/withdraw',json={'version':3}).status_code == 404
        assert other.post(WORK + '/permissions',json={'reviewer':True,'user_id':1}).status_code == 405
        assert other.post(WORK + '/review-queue/' + item['id'] + '/approve',json={'version':3,'user_id':1}).status_code == 422


def test_candidate_edit_aba_permanently_invalidates_old_pre_review(review):
    client, app, _, _, _, source, candidate = review
    grant(app)
    checked = check(client,submit(client,candidate).json())
    assert approve(client,checked).status_code == 200
    # Even saving identical content creates a new candidate revision.
    updated = revise(client,candidate,source)
    assert updated.status_code == 200
    stale = client.get(WORK + '/submissions').json()['items'][0]
    assert stale['status'] == 'invalidated' and stale['reason'] == 'candidate_changed'
    assert approve(client,checked).status_code == 409
    assert submit(client,updated.json()).status_code == 201
    assert len(client.get(WORK + '/submissions').json()['items']) == 2


def test_answer_aba_and_reviewer_regrant_do_not_revive_approval(review):
    client, app, _, data, _, _, candidate = review
    grant(app)
    checked = check(client,submit(client,candidate).json())
    assert approve(client,checked).status_code == 200
    with app.state.database.connect() as connection:
        row = connection.execute('SELECT message FROM coach_turns WHERE conversation_id=? AND version=1',(data['id'],)).fetchone()
        connection.execute('UPDATE coach_turns SET message=? WHERE conversation_id=? AND version=1',('changed',data['id']))
        connection.execute('UPDATE coach_turns SET message=? WHERE conversation_id=? AND version=1',(row[0],data['id']))
    assert client.get(WORK + '/submissions').json()['items'][0]['status'] == 'invalidated'
    again = check(client,submit(client,candidate).json())
    assert approve(client,again).status_code == 200
    set_reviewer(app.state.database,'alice',False)
    assert client.get(WORK + '/review-queue').status_code == 403
    grant(app)
    row = next(item for item in client.get(WORK + '/submissions').json()['items'] if item['id']==again['id'])
    assert row['status'] == 'invalidated' and row['reason'] == 'reviewer_changed'


@pytest.mark.parametrize('change,reason', [('source','source_changed'),('other_source','corpus_changed')])
def test_changed_source_or_corpus_invalidates_gate_even_when_stale_approval_is_rejected(review, change, reason):
    client, app, _, _, _, _, candidate = review
    grant(app)
    checked = check(client,submit(client,candidate).json())
    original = app.state.knowledge.source_path.read_bytes()
    source_id = 'cdc-adults' if change=='source' else 'cdc-intensity'
    mutate(app.state.knowledge,lambda corpus:next(s for s in corpus['sources'] if s['id']==source_id).update(status='withdrawn'))
    assert approve(client,checked).status_code == 409
    # A rejected write still persists the observed revocation.
    app.state.knowledge.source_path.write_bytes(original)
    stale = client.get(WORK + '/submissions').json()['items'][0]
    assert stale['status'] == 'invalidated' and stale['reason'] == reason
    assert approve(client,checked).status_code == 409


def test_gate_rerun_and_rejection_reject_old_tokens_and_allow_explicit_resubmission(review):
    client, app, _, _, _, _, candidate = review
    grant(app)
    item = submit(client,candidate).json()
    one = check(client,item)
    two = check(client,one)
    assert approve(client,one,version=two['version']).status_code == 409
    endpoint = WORK + '/review-queue/' + item['id'] + '/reject'
    rejected = client.post(endpoint,json={'version':two['version'],'reason':'needs_context'})
    assert rejected.status_code == 200 and rejected.json()['status']=='rejected'
    assert approve(client,two).status_code == 409
    assert submit(client,candidate).status_code == 201


def test_failed_lexical_smoke_cannot_be_pre_approved_and_never_calls_dense_model(review, tmp_path, monkeypatch):
    client, app, _, _, _, _, candidate = review
    lexical = app.state.knowledge
    class NoPaidCalls:
        def embed(self,*args,**kwargs):
            raise AssertionError('No paid encoding during precheck')
    app.state.knowledge = HybridKnowledgeRetriever(lexical,NoPaidCalls(),VectorStore(tmp_path/'vectors.sqlite3'))
    item = submit(client,candidate).json()
    grant(app)
    monkeypatch.setattr(lexical,'search',lambda *args,**kwargs:[])
    result = check(client,item)
    assert result['status']=='pending' and not result['gate']['passed']
    assert not result['gate']['report']['checks']['lexical_source_smoke']
    assert approve(client,result).status_code == 409


def test_unavailable_source_blocks_checks_but_owner_withdrawal_and_delete_work(review, monkeypatch):
    client, app, _, _, feedback, _, candidate = review
    item = submit(client,candidate).json()
    grant(app)
    def unavailable():
        raise KnowledgeUnavailable('synthetic')
    monkeypatch.setattr(app.state.knowledge,'all_chunks',unavailable)
    result = client.post(WORK+'/review-queue/'+item['id']+'/check',json={'version':1})
    assert result.status_code==503
    assert client.get(WORK+'/review-queue').json()['items'][0]['source_unavailable']
    assert client.post(WORK+'/submissions/'+item['id']+'/withdraw',json={'version':1}).status_code==200
    assert client.request('DELETE',WORK+'/feedback/'+feedback['id'],json={'version':1}).status_code==204
    assert client.get(WORK+'/submissions').json()['items']==[]
    with app.state.database.connect() as connection:
        for table in ('knowledge_submissions','knowledge_review_events','knowledge_review_gates'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0


def test_submission_retry_concurrency_and_cross_site_boundary(review):
    client, _, _, _, _, _, candidate = review
    body={'version':1,'client_id':str(uuid4()),'share_public_excerpt':True}
    endpoint=WORK+'/candidates/'+candidate['id']+'/submit'
    assert client.post(endpoint,json=body,headers={'Origin':'https://evil.example'}).status_code==403
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:client.post(endpoint,json=body),range(2)))
    assert all(item.status_code==201 for item in results)
    assert results[0].json()['id']==results[1].json()['id']
    assert len(client.get(WORK+'/submissions').json()['items'])==1


def test_role_cli_does_not_create_accounts_or_upgrade_unknown_databases(review,tmp_path,capsys):
    _, app, _, _, _, _, _ = review
    path=app.state.database.path
    args=['--database',str(path),'--username','alice']
    assert role_cli(['grant',*args])==0
    assert role_cli(['revoke',*args])==0
    assert role_cli(['grant','--database',str(path),'--username','missing'])==1
    missing=tmp_path/'missing.sqlite3'
    assert role_cli(['grant','--database',str(missing),'--username','alice'])==1
    assert not missing.exists()
    output=capsys.readouterr()
    assert 'alice' not in output.out and 'missing' not in output.err


def test_guest_cannot_receive_reviewer_capability_even_if_a_role_row_is_inserted(review):
    _, app, _, _, _, _, _ = review
    from services.knowledge_review import reviewer
    from services.guests import GUEST_HEADER
    from dataclasses import replace
    guest_app=create_app(replace(app.state.settings,guest_enabled=True))
    with TestClient(guest_app) as guest:
        guest.headers[GUEST_HEADER]='a'*64
        result=guest.post('/api/auth/guest',json={})
        assert result.status_code==201,result.text
        with guest_app.state.database.connect() as connection:
            row=connection.execute('SELECT user_id FROM guest_accounts').fetchone()
            guest_id=row[0]
            username=connection.execute('SELECT username FROM users WHERE id=?',(guest_id,)).fetchone()[0]
            connection.execute('INSERT INTO knowledge_reviewers(user_id,enabled,revision) VALUES (?,1,1)',(guest_id,))
            assert reviewer(connection,guest_id) is None
        with pytest.raises(ValueError,match='non-guest'):
            set_reviewer(guest_app.state.database,username,True)
        assert not guest.get(WORK+'/permissions').json()['reviewer']


def test_deleting_reviewer_account_invalidates_approval_and_removing_owner_erases_queue(review):
    client, app, _, _, _, _, candidate = review
    submitted=submit(client,candidate).json()
    with TestClient(app) as other:
        register(other,'bob')
        grant(app,'bob')
        assert approve(other,check(other,submitted)).status_code==200
        with app.state.database.connect() as connection:
            reviewer_id=connection.execute("SELECT id FROM users WHERE username='bob'").fetchone()[0]
            connection.execute('DELETE FROM users WHERE id=?',(reviewer_id,))
            assert connection.execute('SELECT status FROM knowledge_submissions').fetchone()[0]=='invalidated'
    with app.state.database.connect() as connection:
        owner=connection.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
        connection.execute('DELETE FROM ai_usage WHERE user_id=?',(owner,))
        connection.execute('DELETE FROM users WHERE id=?',(owner,))
        for table in ('knowledge_submissions','knowledge_review_events','knowledge_review_gates','knowledge_reviewers'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0


def test_v19_upgrade_preserves_feedback_and_candidate_fields_and_restores(tmp_path):
    path=tmp_path/'old.sqlite3'
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        strip_review_v20(connection)
        connection.execute('PRAGMA user_version=19')
    columns,before=table_columns(path),fingerprints(path)
    result=migrate(path,tmp_path/'before.sqlite3')
    assert result['schema']== 22 and result['tables']== 38
    assert fingerprints(path,columns=columns)==before
    with sqlite3.connect(str(path)+'.pre-v20.bak') as old:
        assert old.execute('PRAGMA user_version').fetchone()[0]==19
    snapshot,restored=tmp_path/'snapshot.sqlite3',tmp_path/'restored.sqlite3'
    create_backup(path,snapshot)
    assert restore_backup(snapshot,restored)['schema']== 22
