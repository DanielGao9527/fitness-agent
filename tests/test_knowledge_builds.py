import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rag.catalog import Corpus
from rag.hybrid import HybridKnowledgeRetriever
from rag.rag_service import LocalKnowledgeRetriever
from rag.vector_store import VectorStore
from services.backups import fingerprints
from services.business_time import business_today
from services.knowledge_builds import KnowledgeBuilds, FrozenKnowledge, FILES
from services.knowledge_review import set_reviewer
from test_foundation import application, client, register
from test_knowledge import library, mutate
from test_coach_knowledge import knowledge_chat
from test_knowledge_workbench import workbench, WORK
from test_knowledge_review import review, grant, submit, check, approve, revise
from test_hybrid_retrieval import FakeEmbedding


@pytest.fixture
def builds(review, tmp_path):
    client, app, model, data, feedback, source, candidate = review
    grant(app)
    accepted = approve(client, check(client, submit(client, candidate).json())).json()
    lexical = app.state.knowledge
    provider = FakeEmbedding()
    store = VectorStore(tmp_path / 'knowledge-vectors.sqlite3')
    store.build(lexical, provider)
    app.state.knowledge = HybridKnowledgeRetriever(lexical, provider, store)
    provider.calls = 0
    return client, app, provider, data, feedback, source, candidate, accepted


def body(item, **changes):
    return {'client_id': str(uuid4()), 'submissions': [{'id': item['id'], 'version': item['version']}],
            'confirmed': True, **changes}


def service(app, user_id=1):
    return KnowledgeBuilds(app.state.database, user_id, app.state.knowledge, app.state.settings, app.state.knowledge_build_lock)


def test_complete_package_deduplicates_identical_summary_and_never_changes_online_knowledge(builds):
    client, app, provider, data, feedback, _, _, accepted = builds
    source_before = app.state.knowledge.lexical.source_path.read_bytes()
    cache_before = app.state.knowledge.vector_store.path.read_bytes()
    records_before = fingerprints(app.state.database.path)
    request = body(accepted)
    result = client.post(WORK + '/builds', json=request)
    assert result.status_code == 201, result.text
    package = result.json()
    assert package['status'] == 'built_not_published' and not package['publication_allowed'] and not package['published']
    assert package['new_chunks'] == 0 and package['deduplicated'] == 1
    assert package['regression']['passed'] and package['regression']['regressions'] == 0
    assert client.post(WORK + '/builds', json=request).json() == package
    root = service(app).root / package['id']
    assert set(path.name for path in root.iterdir()) == set(FILES) | {'manifest.json'}
    manifest, report = service(app)._verify(root)
    assert report['independent_semantic_quality'] == report['hybrid_ranking_quality'] == 'not_evaluated'
    assert report['split'] == 'dev' and not report['publication_allowed']
    for path in root.iterdir():
        if path.suffix == '.json':
            text = path.read_text(encoding='utf-8')
            assert feedback['comment'] not in text and data['id'] not in text
            assert not any('"'+key+'"' in text for key in ('user_id','username','feedback_id','question','reference_answer','review_note','reviewer_id'))
    assert app.state.knowledge.lexical.source_path.read_bytes() == source_before
    assert app.state.knowledge.vector_store.path.read_bytes() == cache_before
    assert fingerprints(app.state.database.path) == records_before and provider.calls == 0
    assert len(client.get(WORK + '/builds').json()['items']) == 1
    assert client.post(WORK + '/builds/' + package['id'] + '/publish', json={}).status_code == 404


@pytest.mark.parametrize('change', [{'confirmed': False}, {'confirmed': 1}, {'submissions': []},
    {'user_id': 99}, {'output': '../private.sqlite3'}, {'live': True}, {'client_id': '../escape'},
    {'submissions': [{'id': str(uuid4()), 'version': True}]}])
def test_untrusted_or_unconfirmed_build_inputs_are_rejected(builds, change):
    client, app, provider, _, _, _, _, accepted = builds
    assert client.post(WORK + '/builds', json=body(accepted, **change)).status_code == 422
    assert not service(app).root.exists() and provider.calls == 0


def test_duplicate_selection_and_non_approved_version_are_blocked(builds):
    client, app, _, _, _, _, _, accepted = builds
    entry = {'id': accepted['id'], 'version': accepted['version']}
    assert client.post(WORK + '/builds', json=body(accepted, submissions=[entry, entry])).status_code == 422
    assert client.post(WORK + '/builds', json=body(accepted, submissions=[{**entry,'version':1}])).status_code == 409
    assert client.post(WORK + '/builds', json=body(accepted, submissions=[{**entry,'id':str(uuid4())}])).status_code == 404
    assert not service(app).root.exists()


@pytest.mark.parametrize('field,value',[('passed',0),('binding_hash','0'*64)])
def test_corrupt_precheck_cannot_be_used_as_build_approval(builds,field,value):
    client,app,_,_,_,_,_,accepted=builds
    with app.state.database.connect() as connection:
        connection.execute(f'UPDATE knowledge_review_gates SET {field}=? WHERE id=?',(value,accepted['gate']['id']))
    assert client.post(WORK+'/builds',json=body(accepted)).status_code==409
    stale=client.get(WORK+'/submissions').json()['items'][0]
    assert stale['status']=='invalidated' and stale['reason']=='gate_changed'
    assert not service(app).root.exists()


def test_build_requires_current_server_reviewer_role_and_cross_site_boundary(builds):
    client, app, _, _, _, _, _, accepted = builds
    assert client.post(WORK + '/builds', json=body(accepted), headers={'Origin':'https://evil.example'}).status_code == 403
    with TestClient(app) as other:
        assert other.get(WORK + '/builds').status_code == 401
        register(other,'bob')
        assert other.get(WORK + '/builds').status_code == 403
        assert other.post(WORK + '/builds', json=body(accepted)).status_code == 403
        grant(app,'bob')
        assert other.post(WORK + '/builds', json=body(accepted)).status_code == 201
        assert other.get(WORK + '/feedback').json()['items'] == []


def test_shortened_excerpt_requires_its_own_exact_cached_encoding(builds):
    client, app, provider, _, _, source, candidate, _ = builds
    candidate['content'] = candidate['content'][:20]
    updated = revise(client,candidate,source).json()
    accepted = approve(client,check(client,submit(client,updated).json())).json()
    request = body(accepted)
    failed = client.post(WORK + '/builds',json=request)
    assert failed.status_code == 409, failed.text
    assert failed.json()['detail']['reason'] == 'vector_cache_incomplete'
    assert not [p for p in service(app).root.iterdir() if p.is_dir()] and provider.calls == 0
    failure=client.get(WORK+'/builds').json()['failures'][0]
    assert failure['code']=='vector_cache_unavailable' and not failure['publication_allowed']
    # Prepare synthetic exact-text vectors explicitly, then deny any further encoding.
    with app.state.database.connect() as connection:
        selected = [dict(connection.execute('SELECT * FROM knowledge_submissions WHERE id=?',(accepted['id'],)).fetchone())]
    corpus = Corpus.model_validate_json(app.state.knowledge.lexical.source_path.read_bytes())
    generated, _, _ = service(app)._corpus(corpus,selected,business_today(),request['client_id'])
    prepared = app.state.database.path.parent / 'synthetic-exact-cache'
    prepared.mkdir()
    (prepared/'sources.json').write_text(generated.model_dump_json(),encoding='utf-8')
    lexical = LocalKnowledgeRetriever(prepared/'sources.json',prepared/'index.sqlite3',tokenizer=app.state.knowledge.lexical.tokenizer)
    app.state.knowledge.vector_store.build(lexical,provider)
    provider.calls = 0
    result = client.post(WORK+'/builds',json=request)
    assert result.status_code == 201,result.text
    assert result.json()['new_chunks'] == 1 and result.json()['deduplicated'] == 0 and provider.calls == 0
    frozen = FrozenKnowledge(service(app).root/request['client_id'],app.state.knowledge.lexical.tokenizer)
    assert len(frozen.all_chunks()) == len(app.state.knowledge.all_chunks())+1


@pytest.mark.parametrize('file', ['sources.json','knowledge.sqlite3','knowledge-vectors.sqlite3','regression.json'])
def test_corrupt_package_is_detected_and_not_repaired_or_activated(builds,file):
    client, app, _, _, _, _, _, accepted = builds
    request=body(accepted)
    assert client.post(WORK+'/builds',json=request).status_code==201
    target=service(app).root/request['client_id']/file
    target.write_bytes(target.read_bytes()+b'corrupt')
    changed=target.read_bytes()
    items=client.get(WORK+'/builds').json()['items']
    assert items[0]['status']=='invalid_package' and not items[0]['publication_allowed']
    assert client.post(WORK+'/builds',json=request).status_code==503
    assert target.read_bytes()==changed


def test_changed_approval_after_build_is_stale_and_prior_package_is_preserved(builds):
    client, app, _, _, _, source, candidate, accepted = builds
    request=body(accepted)
    assert client.post(WORK+'/builds',json=request).status_code==201
    target=service(app).root/request['client_id']
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir()}
    assert revise(client,candidate,source).status_code==200
    item=client.get(WORK+'/builds').json()['items'][0]
    assert item['currently_stale'] and not item['published']
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir()}


def test_revoked_permission_during_build_cleans_staging_and_does_not_touch_previous_version(builds,monkeypatch):
    client, app, _, _, _, _, _, accepted = builds
    request=body(accepted)
    assert client.post(WORK+'/builds',json=request).status_code==201
    previous=service(app).root/request['client_id']
    before=(previous/'manifest.json').read_bytes()
    original=VectorStore.copy_cached
    def revoke(*args,**kwargs):
        result=original(*args,**kwargs)
        set_reviewer(app.state.database,'alice',False)
        return result
    monkeypatch.setattr(VectorStore,'copy_cached',revoke)
    result=client.post(WORK+'/builds',json=body(accepted))
    assert result.status_code==403,result.text
    assert sorted(p.name for p in service(app).root.iterdir() if p.is_dir())==[request['client_id']]
    assert (previous/'manifest.json').read_bytes()==before


def test_gate_failure_preserves_previous_version_and_does_not_write_new_package(builds,monkeypatch):
    import services.knowledge_builds as module
    client, app, _, _, _, _, _, accepted = builds
    original=module.regression
    def fail(*args,**kwargs):
        result=original(*args,**kwargs)
        return {**result,'passed':False,'regressions':1}
    monkeypatch.setattr(module,'regression',fail)
    result=client.post(WORK+'/builds',json=body(accepted))
    assert result.status_code==409 and result.json()['detail']['code']=='KNOWLEDGE_BUILD_REGRESSION'
    assert not [p for p in service(app).root.iterdir() if p.is_dir()]
    attempt=client.get(WORK+'/builds').json()['failures'][0]
    assert attempt['code']=='development_regression' and attempt['regressions']==1
    receipt=service(app).root/('failed-'+attempt['id']+'.json')
    report=json.loads(receipt.read_text())['regression']
    assert report['results'] and not any('question' in row for row in report['results'])


def test_same_client_id_with_different_request_and_concurrent_retry(builds):
    client, app, provider, _, _, _, _, accepted = builds
    request=body(accepted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(lambda _:client.post(WORK+'/builds',json=request),range(2)))
    assert all(item.status_code==201 for item in result)
    assert result[0].json()==result[1].json() and provider.calls==0
    assert len(list(service(app).root.iterdir()))==1
    request['submissions'][0]['version']-=1
    assert client.post(WORK+'/builds',json=request).status_code==409


def test_unknown_existing_target_and_path_escape_are_never_overwritten(builds):
    client, app, _, _, _, _, _, accepted=builds
    request=body(accepted);root=service(app).root;root.mkdir()
    target=root/request['client_id'];target.write_text('keep unrelated file')
    assert client.post(WORK+'/builds',json=request).status_code==503
    assert target.read_text()=='keep unrelated file'
    with pytest.raises(ValueError,match='escapes'):
        service(app)._child('..')
