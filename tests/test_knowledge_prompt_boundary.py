"""Fixed-provider wiring and source contracts, never real semantic-quality scores."""
import json

import pytest

import api.knowledge as knowledge_api
from config import ROOT
from test_foundation import application, client
from test_knowledge import library
from test_knowledge_answers import AnswerModel, qa, ask, calls
from test_knowledge_frequency_routing import business_rows
from test_knowledge_recovery import Pages


class CapturingModel(AnswerModel):
    def __init__(self, output):
        super().__init__()
        self.output = json.dumps(output)
        self.prompts = []

    def generate(self, *, system_prompt, message):
        self.prompts.append(system_prompt)
        return super().generate(system_prompt=system_prompt, message=message)


@pytest.mark.parametrize('offered,status,selected,http_status', [
    ('phe-eatwell:vegetables', 'answered', ['phe-eatwell:vegetables'], 200),
    ('phe-eatwell:fruit', 'insufficient', [], 200),
    ('phe-eatwell:fruit', 'answered', ['phe-eatwell:vegetables'], 502),
])
def test_active_prompt_reaches_judgment_and_only_valid_selection_is_displayed(
        qa, monkeypatch, offered, status, selected, http_status):
    client, app, _, _ = qa
    model = CapturingModel({'status': status, 'chunk_ids': selected})
    monkeypatch.setattr(knowledge_api, 'create_knowledge_model', lambda _: model)
    app.state.knowledge = Pages(app.state.knowledge, [[offered]])
    before = business_rows(app)
    question = '通用饮食指南能否直接确定个人每餐的具体克数？'
    response = ask(client, question)
    assert response.status_code == http_status, response.text
    assert len(model.prompts) == len(model.messages) == 1 and calls(app) == 2
    assert model.prompts[0].split('\nJSON Schema:\n')[0] == (ROOT/'prompts/knowledge_answer.md').read_text(encoding='utf-8')
    assert model.messages[0]['question'] == question
    assert [v['chunk_id'] for v in model.messages[0]['evidence']] == [offered]
    if http_status == 200:
        result = response.json()
        assert result['status'] == status and result['saved'] is False
        assert [v['chunk_id'] for v in result['sources']] == selected
        if selected:
            assert result['sources'][0]['excerpt'] == model.messages[0]['evidence'][0]['excerpt']
    else:
        assert response.json()['detail']['code'] == 'MODEL_INVALID_OUTPUT'
    assert business_rows(app) == before


@pytest.mark.parametrize('question', [
    '请告诉我今天每餐应该吃多少克米饭',
    '一般蔬果建议能够直接给出我每顿应该吃多少克吗？我对牛奶过敏，还请给我配餐。',
])
def test_personal_portions_and_mixed_health_requests_still_stop_before_model(qa, question):
    client, app, model, _ = qa
    before = business_rows(app)
    result = ask(client, question).json()
    assert result['status'] == 'personal_scope' and not result['sources']
    assert not model.messages and calls(app) == 0
    assert business_rows(app) == before


def test_clinical_request_can_still_receive_model_scope_refusal(qa, monkeypatch):
    client, app, _, _ = qa
    model = CapturingModel({'status': 'personal_scope', 'chunk_ids': []})
    monkeypatch.setattr(knowledge_api, 'create_knowledge_model', lambda _: model)
    app.state.knowledge = Pages(app.state.knowledge, [['mayo-strength:basics']])
    before = business_rows(app)
    result = ask(client, '医生诊断我有肩袖撕裂，什么时候可以恢复大重量推肩？').json()
    assert result['status'] == 'personal_scope' and not result['sources']
    assert len(model.messages) == 1 and calls(app) == 2
    assert business_rows(app) == before
