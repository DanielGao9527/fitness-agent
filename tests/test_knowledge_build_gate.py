import json

import pytest

import services.knowledge_build_gate as gate
from services.business_time import business_today
from test_knowledge import library, mutate


def test_identity_regression_uses_only_dev_and_does_not_claim_semantic_quality(library):
    report=gate.regression(library,library,{},business_today())
    assert report['passed'] and report['regressions']==0 and report['comparable']>0
    cases=[json.loads(line) for line in (gate.ROOT/'evals/fitness_qa.jsonl').read_text(encoding='utf-8').splitlines()]
    assert {item['id'] for item in report['results']}=={item['id'] for item in cases if item['split']=='dev'}
    assert report['independent_semantic_quality']=='not_evaluated' and not report['publication_allowed']
    assert not any('question' in item or 'reference_answer' in item for item in report['results'])


def test_losing_real_baseline_labelled_hits_blocks_build(library):
    class MissingHits:
        def export_documents(self,**kwargs):
            return library.export_documents(**kwargs)
        def search(self,*args,**kwargs):
            return []
    report=gate.regression(library,MissingHits(),{},business_today())
    assert report['original_documents_preserved'] and not report['passed'] and report['regressions']>0


def test_original_document_loss_blocks_even_if_search_ranking_is_unchanged(library):
    class MissingDocument:
        def export_documents(self,**kwargs):
            return library.export_documents(**kwargs)[1:]
        def search(self,*args,**kwargs):
            return library.search(*args,**kwargs)
    report=gate.regression(library,MissingDocument(),{},business_today())
    assert not report['original_documents_preserved'] and not report['passed']


def test_duplicate_aliases_do_not_earn_extra_credit_or_refill_the_actual_top_six(library):
    cases=[json.loads(line) for line in (gate.ROOT/'evals/fitness_qa.jsonl').read_text(encoding='utf-8').splitlines()]
    selected=next(row for row in cases if row['split']=='dev' and len(row['relevant_chunk_ids'])>1 and
        set(row['relevant_chunk_ids'])<= {hit.chunk_id for hit in library.search(row['question'],limit=6)})
    original=next(hit for hit in library.search(selected['question'],limit=6) if hit.chunk_id in selected['relevant_chunk_ids'])
    aliases={original.source_id+':alias-'+str(number):original.chunk_id for number in range(6)}
    class Duplicated:
        def export_documents(self,**kwargs):
            return library.export_documents(**kwargs)
        def search(self,query,**kwargs):
            if query==selected['question']:
                return [original.model_copy(update={'chunk_id':key}) for key in aliases]
            return library.search(query,**kwargs)
    report=gate.regression(library,Duplicated(),aliases,business_today())
    result=next(row for row in report['results'] if row['id']==selected['id'])
    assert result['outcome']=='regression' and not report['passed']


def test_unavailable_labels_are_reported_not_counted_as_success(library):
    mutate(library,lambda data:next(s for s in data['sources'] if s['id']=='cdc-adults').update(status='withdrawn'))
    report=gate.regression(library,library,{},business_today())
    assert report['not_comparable']>0
    assert sum(row['outcome']=='not_comparable' for row in report['results'])==report['not_comparable']


def test_changed_frozen_protocol_cannot_silently_use_another_split(library,tmp_path,monkeypatch):
    protocol=json.loads(gate.PROTOCOL_PATH.read_text())
    path=tmp_path/'manifest.json';path.write_text(json.dumps({**protocol,'split':'test'}))
    monkeypatch.setattr(gate,'PROTOCOL_PATH',path)
    with pytest.raises(ValueError,match='protocol'):
        gate.regression(library,library,{},business_today())
