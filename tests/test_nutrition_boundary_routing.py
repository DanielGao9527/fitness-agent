"""General-guideline limits are knowledge; requests for actual portions stay personal."""
import json

import pytest

from schemas import CoachUnderstanding
from services.coach import route
from services.coach_reviews import CoachReviewService
from services.knowledge_answers import outside_scope
from test_foundation import application, client
from test_knowledge import library
from test_coach import conversation, send
from test_coach_knowledge import knowledge_chat, answer
from test_knowledge_answers import qa, ask, calls
from test_knowledge_frequency_routing import business_rows
from test_knowledge_recovery import Pages


QUESTION = "一般蔬果建议能够直接给出我每顿应该吃多少克吗？"
GENERAL = [QUESTION,
    "通用饮食指南能否直接确定个人每餐的具体克数？",
    "一般营养原则是否能够推断每个人每餐需要吃的份量？",
    "一般蔬果建议能不能直接给出每顿具体分量？"]


@pytest.mark.parametrize("question", GENERAL)
def test_general_guideline_limits_have_one_consistent_knowledge_boundary(question):
    assert route(question) == "knowledge"
    assert not outside_scope(question, {})
    result = CoachUnderstanding.model_validate({"scope": "knowledge", "command": "", "clarification": "none",
        "notes": [{"version": 1, "avoid": [], "preferences": [], "training_caution": False,
                   "diet_caution": False, "unresolved": False}]})
    facts = {"version": 1, "day": "2026-10-18", "messages": [{"version": 1, "message": question}]}
    assert CoachReviewService.validate(result, facts)["clarification"] == "none"


@pytest.mark.parametrize("question", [
    "请告诉我每餐应该吃多少克蔬菜", "我今天应该吃多少克米饭？",
    QUESTION + "帮我生成今天的食谱", QUESTION + "我对牛奶过敏",
    QUESTION + "我最近肩膀疼", QUESTION + "我正在怀孕",
    "28岁75公斤，" + QUESTION, "一般饮食建议能不能给我安排每餐吃多少克？",
    "根据我的训练记录，" + QUESTION,
])
def test_general_prefix_never_removes_personal_or_health_conditions(question):
    assert route(question) != "knowledge"
    assert outside_scope(question, {})


def test_main_chat_guideline_limit_reaches_judgment_without_fact_writes(knowledge_chat):
    client, app, model = knowledge_chat
    before = business_rows(app)
    app.state.knowledge = Pages(app.state.knowledge, [["phe-eatwell:vegetables"]])
    model.output = json.dumps({"status": "answered", "chunk_ids": ["phe-eatwell:vegetables"]})
    data = send(client, conversation(client), QUESTION).json()
    assert data["request_kind"] == "knowledge" and not data["pending"]
    response = answer(client, data)
    assert response.status_code == 200, response.text
    result = response.json()["turns"][-1]["response"]["knowledge"]
    assert result["status"] == "answered" and len(model.messages) == 1
    assert calls(app) == 2  # Pages reserves one simulated query encoding plus judgment.
    assert model.messages[0]["question"] == QUESTION
    assert result["sources"][0]["chunk_id"] == "phe-eatwell:vegetables"
    assert business_rows(app) == before


def test_direct_knowledge_uses_same_boundary(qa):
    client, app, model, _ = qa
    before = business_rows(app)
    app.state.knowledge = Pages(app.state.knowledge, [["phe-eatwell:vegetables"]])
    model.output = json.dumps({"status": "answered", "chunk_ids": ["phe-eatwell:vegetables"]})
    response = ask(client, QUESTION)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "answered" and len(model.messages) == 1
    assert calls(app) == 2
    assert business_rows(app) == before


def test_existing_health_profile_still_blocks_general_guideline_question():
    assert outside_scope(QUESTION, {"preferences": "肩膀疼"})
    assert outside_scope(QUESTION, {"food_allergies": "牛奶过敏"}, {"nutrition"})
