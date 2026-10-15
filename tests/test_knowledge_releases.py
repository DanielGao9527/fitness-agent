import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from database import Database
from rag.rag_service import KnowledgeUnavailable
from rag.rag_service import LocalKnowledgeRetriever
from rag.vector_store import VectorStore
from schema_fixtures import strip_releases_v21
from scripts.migrate_memory import migrate
from services.backups import create_backup, fingerprints, restore_backup, table_columns
from services.knowledge_feedback import encode
from services.knowledge_review import set_reviewer
from test_foundation import application, client, register
from test_knowledge import library, mutate
from test_coach_knowledge import knowledge_chat
from test_knowledge_workbench import workbench, WORK
from test_knowledge_review import review, grant, revise
from test_knowledge_builds import builds, body, service
from services.knowledge_builds import FILES, digest


@pytest.fixture
def release(builds):
    client, app, _, _, _, _, _, accepted = builds
    response = client.post(WORK+'/builds', json=body(accepted))
    assert response.status_code == 201, response.text
    return builds, response.json()


def registration(package, **change):
    return {'id': package['id'], 'manifest_hash': package['manifest_hash'], 'confirmed': True, **change}


def register_package(client, package):
    response = client.post(WORK+'/releases', json=registration(package))
    assert response.status_code == 201, response.text
    return response.json()


def check_release(client, row):
    return client.post(WORK+'/releases/'+row['id']+'/check', json={'version': row['version']})


def test_registry_and_readiness_are_persistent_bound_and_never_publish(release):
    (client, app, provider, _, feedback, _, _, _), package = release
    retriever = app.state.knowledge
    before = fingerprints(app.state.database.path)
    source = retriever.lexical.source_path.read_bytes()
    vector = retriever.vector_store.path.read_bytes()
    directory = service(app).root/package['id']
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()}
    item = register_package(client, package)
    assert item['status'] == 'registered' and item['version'] == 1 and item['manifest_hash'] == package['manifest_hash']
    assert register_package(client, package) == item
    response = check_release(client, item)
    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked['status'] == 'blocked' and checked['version'] == 2
    assert not checked['published'] and not checked['publication_allowed']
    report = checked['report']
    assert all(report['engineering_checks'].values()) and len(report['blockers']) == 2
    assert report['real_provider_calls'] == provider.calls == 0
    assert report['independent_semantic_quality'] == report['hybrid_ranking_quality'] == 'not_evaluated'
    assert report['manifest_sha256'] == package['manifest_hash']
    assert len(report['binding_hash']) == 64
    assert check_release(client, item).status_code == 409  # Lost response retry cannot append another check.
    # Restart initializes idempotently and retains the register, report, and ordered history.
    Database(app.state.database.path).initialize()
    listing = client.get(WORK+'/releases').json()
    assert listing == {'items': [checked], 'publication_enabled': False}
    public = json.dumps(listing, ensure_ascii=False)
    assert not any(key in public for key in ('registrant_id','actor_id','candidate_id','conversation_id','answer_hash','password_hash'))
    assert feedback['comment'] not in public
    after = fingerprints(app.state.database.path)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {'knowledge_releases','knowledge_release_members','knowledge_release_events'}
    assert app.state.knowledge is retriever and retriever.lexical.source_path.read_bytes() == source
    assert retriever.vector_store.path.read_bytes() == vector
    assert files == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()}
    for action in ('publish','activate','rollback'):
        assert client.post(WORK+'/releases/'+item['id']+'/'+action, json={'version':2,'passed':True}).status_code == 404


@pytest.mark.parametrize('change', [
    {'confirmed':False}, {'confirmed':1}, {'id':'../../outside'}, {'manifest_hash':'bad'},
    {'user_id':1}, {'passed':True}, {'quality_report':{'passed':True}}, {'output_path':'x'}, {'live':True},
])
def test_registration_rejects_client_authority_and_unconfirmed_input(release, change):
    (client, app, provider, *_), package = release
    assert client.post(WORK+'/releases',json=registration(package,**change)).status_code == 422
    assert client.get(WORK+'/releases').json()['items'] == [] and provider.calls == 0


def test_wrong_hash_or_missing_bundle_does_not_create_registration(release):
    (client, app, provider, *_), package = release
    assert client.post(WORK+'/releases',json=registration(package,manifest_hash='0'*64)).status_code == 409
    assert client.post(WORK+'/releases',json=registration(package,id=str(uuid4()))).status_code == 503
    assert client.get(WORK+'/releases').json()['items'] == [] and provider.calls == 0


def test_repacked_unapproved_source_cannot_register_even_with_consistent_artifact_hashes(release):
    (client, app, provider, *_), package = release
    directory = service(app).root/package['id']
    path = directory/'sources.json'
    corpus = json.loads(path.read_bytes())
    corpus['sources'][0]['sections'][0]['summary'] += ' Unapproved synthetic fact.'
    path.write_text(json.dumps(corpus,ensure_ascii=False),encoding='utf-8')
    lexical = LocalKnowledgeRetriever(path,directory/'knowledge.sqlite3',tokenizer=app.state.knowledge.lexical.tokenizer)
    # Explicit synthetic fixture encoding, not a production registration call.
    VectorStore(directory/'knowledge-vectors.sqlite3').build(lexical,provider)
    provider.calls = 0
    manifest = json.loads((directory/'manifest.json').read_bytes())
    manifest['files'] = {name:digest(directory/name) for name in FILES}
    (directory/'manifest.json').write_text(encode(manifest),encoding='utf-8')
    result = client.post(WORK+'/releases',json=registration(package,manifest_hash=digest(directory/'manifest.json')))
    assert result.status_code == 409, result.text
    assert client.get(WORK+'/releases').json()['items'] == [] and provider.calls == 0


def test_non_reviewer_and_unauthenticated_cannot_see_or_change_registry(release):
    (client, app, _, *_), package = release
    item = register_package(client, package)
    with TestClient(app) as other:
        for path in ('/releases', '/releases/'+item['id']+'/check', '/releases/'+item['id']+'/withdraw'):
            response = other.get(WORK+path) if path == '/releases' else other.post(WORK+path,json={'version':1})
            assert response.status_code == 401
        register(other, 'bob')
        assert other.get(WORK+'/releases').status_code == 403
        assert other.post(WORK+'/releases',json=registration(package)).status_code == 403
        assert check_release(other,item).status_code == 403
        assert other.post(WORK+'/releases/'+item['id']+'/withdraw',json={'version':1}).status_code == 403


def test_concurrent_register_and_stale_updates_do_not_duplicate_events(release):
    (client, app, _, *_), package = release
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _:client.post(WORK+'/releases',json=registration(package)), range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json() == responses[1].json()
    item = responses[0].json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _:check_release(client,item), range(2)))
    assert sorted(response.status_code for response in responses) == [200,409]
    assert len(client.get(WORK+'/releases').json()['items'][0]['events']) == 2


def test_withdraw_works_even_with_source_outage_and_registration_cannot_resurrect(release,monkeypatch):
    (client, app, _, *_), package = release
    item = register_package(client,package)
    def unavailable(*args,**kwargs):
        raise KnowledgeUnavailable('synthetic outage')
    monkeypatch.setattr(app.state.knowledge,'all_chunks',unavailable)
    response = client.post(WORK+'/releases/'+item['id']+'/withdraw',json={'version':1})
    assert response.status_code == 200 and response.json()['status'] == 'withdrawn'
    assert register_package(client,package) == response.json()
    assert check_release(client,response.json()).status_code == 409
    assert len(response.json()['events']) == 2


def test_temporary_source_outage_does_not_invalidate_or_report_success(release,monkeypatch):
    (client, app, provider, *_), package = release
    item = register_package(client,package)
    original = app.state.knowledge.all_chunks
    def unavailable(*args,**kwargs):
        raise KnowledgeUnavailable('synthetic outage')
    monkeypatch.setattr(app.state.knowledge,'all_chunks',unavailable)
    listing = client.get(WORK+'/releases').json()['items'][0]
    assert listing['status'] == 'registered' and listing['source_unavailable']
    assert check_release(client,item).status_code == 503 and not listing['report']
    monkeypatch.setattr(app.state.knowledge,'all_chunks',original)
    assert check_release(client,item).status_code == 200 and provider.calls == 0


@pytest.mark.parametrize('cause', ['candidate','withdraw_submission','delete_feedback','delete_conversation'])
def test_submission_change_and_deletion_invalidate_without_listing(release,cause):
    (client, app, _, data, feedback, source, candidate, accepted), package = release
    item = register_package(client,package)
    if cause == 'candidate':
        assert revise(client,candidate,source).status_code == 200
    elif cause == 'withdraw_submission':
        assert client.post(WORK+'/submissions/'+accepted['id']+'/withdraw',json={'version':accepted['version']}).status_code == 200
    else:
        with app.state.database.connect() as connection:
            table,key = ('knowledge_feedback',feedback['id']) if cause == 'delete_feedback' else ('coach_conversations',feedback['conversation_id'])
            connection.execute('DELETE FROM '+table+' WHERE id=?',(key,))
    with app.state.database.connect() as connection:
        status = connection.execute('SELECT status FROM knowledge_releases WHERE id=?',(item['id'],)).fetchone()[0]
    assert status == 'invalidated'
    assert check_release(client,item).status_code == 409
    result = register_package(client,package)
    assert result['status'] == 'invalidated' and result['version'] == 2 and len(result['events']) == 2


def test_external_source_change_is_latched_after_observation_and_restore_does_not_revive(release):
    (client, app, _, *_), package = release
    item = register_package(client,package)
    path = app.state.knowledge.lexical.source_path
    original = path.read_bytes()
    data = json.loads(original)
    data['version'] += '-changed'
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    assert check_release(client,item).status_code == 409
    path.write_bytes(original)
    result = register_package(client,package)
    assert result['status'] == 'invalidated' and len(result['events']) == 2


@pytest.mark.parametrize('artifact', ['manifest.json','sources.json','knowledge.sqlite3','knowledge-vectors.sqlite3','regression.json'])
def test_tampered_package_invalidates_and_never_repairs_or_revives(release,artifact):
    (client, app, _, *_), package = release
    item = register_package(client,package)
    path = service(app).root/package['id']/artifact
    original = path.read_bytes()
    path.write_bytes(original+b'corrupt')
    listing = client.get(WORK+'/releases')
    assert listing.status_code == 200,listing.text
    assert listing.json()['items'][0]['status'] == 'invalidated'
    assert path.read_bytes() == original+b'corrupt'
    path.write_bytes(original)
    assert register_package(client,package)['status'] == 'invalidated'


def test_independent_registrant_role_revocation_is_latched_even_when_pre_reviewer_valid(release):
    (client, app, _, *_), package = release
    with TestClient(app) as other:
        register(other,'bob');grant(app,'bob')
        item = register_package(other,package)
        set_reviewer(app.state.database,'bob',False)
        set_reviewer(app.state.database,'bob',True)
        assert register_package(other,package)['status'] == 'invalidated'
        assert client.get(WORK+'/review-queue').json()['items'][0]['status'] == 'approved'
        assert check_release(other,item).status_code == 409


def test_changed_manifest_is_latched_before_reading_repacked_broken_database(release):
    (client, app, _, *_), package = release
    item = register_package(client,package)
    directory = service(app).root/package['id']
    manifest_path, index_path = directory/'manifest.json',directory/'knowledge.sqlite3'
    original_manifest, original_index = manifest_path.read_bytes(),index_path.read_bytes()
    index_path.write_bytes(b'broken-synthetic-index')
    manifest = json.loads(original_manifest)
    manifest['files']['knowledge.sqlite3'] = digest(index_path)
    manifest_path.write_text(encode(manifest),encoding='utf-8')
    response = client.get(WORK+'/releases')
    assert response.status_code == 200 and response.json()['items'][0]['status'] == 'invalidated'
    manifest_path.write_bytes(original_manifest);index_path.write_bytes(original_index)
    assert register_package(client,package)['status'] == 'invalidated'


def test_delete_registrant_account_revokes_registration_and_drops_identity(release):
    (client, app, _, *_), package = release
    with TestClient(app) as other:
        register(other,'bob');grant(app,'bob')
        item = register_package(other,package)
        with app.state.database.connect() as connection:
            connection.execute("DELETE FROM users WHERE username='bob'")
        with app.state.database.connect() as connection:
            row = connection.execute('SELECT * FROM knowledge_releases WHERE id=?',(item['id'],)).fetchone()
            assert row['status'] == 'invalidated' and row['registrant_id'] is None
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()


def test_gate_tampering_revokes_release_and_restoring_gate_cannot_revive(release):
    (client, app, _, _, _, _, _, approved), package = release
    item = register_package(client,package)
    with app.state.database.connect() as connection:
        connection.execute('UPDATE knowledge_review_gates SET passed=0 WHERE id=?',(approved['gate']['id'],))
    assert check_release(client,item).status_code == 409
    with app.state.database.connect() as connection:
        connection.execute('UPDATE knowledge_review_gates SET passed=1 WHERE id=?',(approved['gate']['id'],))
    assert register_package(client,package)['status'] == 'invalidated'


def test_restore_retains_nonempty_registry_and_ordered_history(release,tmp_path):
    (client, app, _, *_), package = release
    item = register_package(client,package)
    assert check_release(client,item).status_code == 200
    snapshot, restored = tmp_path/'snapshot.sqlite3',tmp_path/'recovered.sqlite3'
    before = fingerprints(app.state.database.path)
    create_backup(app.state.database.path,snapshot)
    restore_backup(snapshot,restored)
    Database(restored).initialize()
    after = fingerprints(restored)
    assert all(after[name] == before[name] for name in before.keys()-{'sessions','meal_consents'})
    with Database(restored).connect() as connection:
        assert connection.execute('SELECT status FROM knowledge_releases').fetchone()[0] == 'blocked'
        assert connection.execute('SELECT COUNT(*) FROM knowledge_release_members').fetchone()[0] == 1
        assert connection.execute('SELECT version FROM knowledge_release_events ORDER BY version').fetchall()[1][0] == 2


def test_schema20_migration_preserves_old_rows_and_recovery(tmp_path):
    path = tmp_path/'personal.sqlite3'
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO users(username,password_hash) VALUES ('synthetic_migration','fixture-placeholder')")
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES (1,'{\"display_name\":\"Synthetic only\"}')")
        strip_releases_v21(connection)
        connection.execute('PRAGMA user_version=20')
    columns,before = table_columns(path),fingerprints(path)
    backup = tmp_path/'before.sqlite3'
    result = migrate(path,backup)
    assert result['schema'] == 23 and result['tables'] == 39 and result['old_tables_preserved']
    assert fingerprints(path,columns=columns) == before
    with sqlite3.connect(str(path)+'.pre-v21.bak') as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 20
    Database(path).initialize()
    fresh = tmp_path/'fresh.sqlite3'
    create_backup(path,fresh)
    assert restore_backup(fresh,tmp_path/'restored.sqlite3')['schema'] == 23
