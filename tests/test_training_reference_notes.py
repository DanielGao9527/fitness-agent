import json
from datetime import date

import pytest
from pydantic import ValidationError

from config import ROOT
from rag.catalog import Corpus, Source
from rag.rag_service import LocalKnowledgeRetriever
from services.training_catalog import read_systems, SystemCatalog
from services.training_recommendations import build
from test_foundation import DAY, application, client
from test_knowledge import library, mutate
from test_training_plans import plans
from test_training_program import facts, evidence, VENUE
from test_training_recommendations import setup, recommend, result
from test_coach import BASE


TODAY = date(2026, 9, 23)
NOTES = {'ppl':'training-notes-ppl', 'four':'training-notes-four-split', 'five':'training-notes-five-split'}
SOURCE_FIELDS = {'id', 'title', 'original_title', 'publisher', 'url', 'origin', 'topic',
                 'source_version', 'reviewed_on', 'review_due', 'status', 'scope',
                 'license', 'license_url', 'notice', 'sections'}
HIT_FIELDS = (SOURCE_FIELDS - {'id', 'sections', 'status'}) | {'source_id', 'chunk_id', 'locator', 'excerpt'}


def corpus():
    return Corpus.model_validate_json((ROOT / 'data/knowledge/sources.json').read_bytes())


@pytest.mark.parametrize('split', NOTES)
def test_notes_are_indexed_with_honest_provenance_and_bound_to_structure(library, split):
    source_id = NOTES[split]
    source = next(item for item in corpus().sources if item.id == source_id)
    assert source.origin == 'curated_text'
    assert set(source.model_dump()) == SOURCE_FIELDS
    assert not source.url and not source.license_url and '未核实' in source.publisher
    key = source_id + ':adaptation'
    hit = library.get_chunks([key], topic='training', today=TODAY)[0]
    assert set(hit.model_dump()) == HIT_FIELDS
    assert '不是' in source.scope or '不照搬' in source.scope
    assert key in read_systems()[0][split]['source_ids']
    assert any(h.source_id == source_id for h in library.search(source.title, 20, today=TODAY))
    restarted = LocalKnowledgeRetriever(library.source_path, library.index_path)
    assert restarted.get_chunks([key], today=TODAY) == [hit]
    assert not restarted.get_chunks([key], today=date(2026,9,22))
    assert not restarted.get_chunks([key], today=date(2027,3,23))
    assert not restarted.get_chunks([key], topic='nutrition', today=TODAY)
    mutate(library, lambda doc: next(s for s in doc['sources'] if s['id']==source_id).update(status='withdrawn'))
    assert not restarted.get_chunks([key], today=TODAY)


def test_catalog_counts_and_transcription_boundaries(library):
    info = library.catalog(today=TODAY)
    assert (info['source_count'], info['chunk_count']) == (90, 133)
    data = {item.id:item for item in corpus().sources}
    assert len(data['training-notes-five-split'].sections) == 6
    assert '未提供组数和次数' in data['training-notes-five-split'].sections[-1].summary
    push = next(s for s in data['training-notes-four-split'].sections if s.id=='push').summary
    assert '1组热身加5组正式组' in push
    assert '力竭' in next(s for s in data['training-notes-ppl'].sections if s.id=='push').summary
    assert all(set(data[key].model_dump()) == SOURCE_FIELDS for key in NOTES.values())


@pytest.mark.parametrize('changes', [
    {'url':'https://www.cdc.gov/'}, {'license_url':'https://www.cdc.gov/'},
    {'url':'javascript:alert(1)'}, {'unpublished_metadata':''}, {'internal_reference':{'value':'test-only'}},
    {'topic':'nutrition'}, {'origin':'public_web'},
])
def test_curated_notes_cannot_forge_external_provenance_or_include_private_metadata(changes):
    source = next(item for item in corpus().sources if item.origin == 'curated_text')
    with pytest.raises(ValidationError):
        Source.model_validate({**source.model_dump(), **changes})


@pytest.mark.parametrize('url', ['', 'http://www.cdc.gov/', 'https://evil.example/', 'file:///private.jpg'])
def test_web_source_still_requires_approved_https(url):
    source = next(item for item in corpus().sources if item.origin == 'public_web')
    with pytest.raises(ValidationError):
        Source.model_validate({**source.model_dump(), 'url':url})


@pytest.mark.parametrize('split', NOTES)
@pytest.mark.parametrize('experience', ['beginner', 'experienced'])
@pytest.mark.parametrize('minutes', [15,30,45,60,90,120])
def test_reference_working_sets_respect_experience_time_and_no_duplicate_actions(split, experience, minutes):
    f = facts(training_split=split)
    f['profile'].update(experience=experience, minutes_per_session=minutes)
    rec = build(f, evidence())
    assert rec['exercises']
    assert rec['estimated_minutes'] <= minutes
    assert rec['estimated_minutes'] == 10 + 3 * sum(row['sets'] for row in rec['exercises'])
    assert len({row['id'] for row in rec['exercises']}) == len(rec['exercises'])
    assert all(1 <= row['sets'] <= row['reference_sets'] <= (3 if experience=='beginner' else 4)
               for row in rec['exercises'])
    assert '正式组，不含热身' in rec['message']
    if any(row['sets'] < row['reference_sets'] for row in rec['exercises']):
        assert '受本次时间限制' in rec['message']
    if minutes == 120:
        assert all(row['sets'] == row['reference_sets'] for row in rec['exercises'])
        assert any(row['sets'] == (3 if experience=='beginner' else 4) for row in rec['exercises'])


def test_three_and_four_split_reference_differences_apply_to_explicit_focus():
    f = facts(training_split='ppl')
    f['profile']['minutes_per_session'] = 120
    f['request'] = {'kind':'strength', 'focus':'胸', 'split':'four'}
    rows = build(f, evidence())['exercises']
    assert next(row for row in rows if row['pattern']=='horizontal-press')['sets'] == 4
    assert next(row for row in rows if row['pattern']=='incline-press')['sets'] == 3
    f['request']['split'] = 'ppl'
    assert next(row for row in build(f, evidence())['exercises'] if row['pattern']=='incline-press')['sets'] == 4


@pytest.mark.parametrize('value', [0,2,5,True,3.5])
def test_working_set_configuration_is_bounded(value):
    raw = json.loads((ROOT / 'data/training/systems.json').read_text(encoding='utf-8'))
    raw['systems'][2]['working_sets']['horizontal-press'] = value
    with pytest.raises(ValidationError):
        SystemCatalog.model_validate(raw)


def test_notes_reach_live_path_and_withdrawal_invalidates_without_writes(plans):
    client, _, model, library = plans
    data = setup(client, training_split='ppl', equipment=VENUE, minutes_per_session=120)
    first = result(recommend(client, data))
    assert any(row['sets'] == 4 for row in first['exercises'])
    note = next(source for source in first['sources'] if source['origin']=='curated_text')
    assert note['source_id'] == 'training-notes-ppl' and not note['url']
    assert set(note) <= HIT_FIELDS
    mutate(library, lambda doc: next(s for s in doc['sources'] if s['id']=='training-notes-ppl').update(status='withdrawn'))
    current = client.get(f"{BASE}/{data['id']}").json()
    assert current['turns'][-1]['response']['training_recommendation']['stale']
    assert result(recommend(client, current))['status'] == 'unavailable'
    assert client.get('/api/workouts', params={'day':DAY}).json() == []
    assert client.get('/api/training-plans', params={'day':DAY}).json() == []
    assert not model.messages
