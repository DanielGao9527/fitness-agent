"""General frequency versus personal prescription; all providers are fixed doubles."""
import json

import pytest

import api.coach as coach_api
import api.knowledge as knowledge_api
from schemas import CoachUnderstanding
from services.coach_reviews import CoachReviewService
from test_coach import conversation, send
from test_coach_knowledge import knowledge_chat, answer
from test_knowledge_answers import qa, ask, calls
from test_foundation import application, client
from test_knowledge import library
from test_coach_reviews import ReviewModel, understand
from test_knowledge_recovery import Pages
from test_knowledge_loop import ScriptedModel, answered, revise, SECOND


FREQUENCY_QUESTIONS = [
    ("一般成年人，肌肉强化活动每周最低要安排几天？", "answered"),
    ("健康成人一周通常推荐几次力量训练？", "answered"),
    ("力量训练每星期安排多少次比较合适？", "answered"),
    # This lexical fixture does not offer the strength source for this wording.
    # Route to knowledge, but a fixed insufficient judgment must remain valid.
    ("抗阻锻炼通常隔多久安排一次？", "insufficient"),
    ("我想了解，一般成年人每周推荐几次力量训练？", "answered"),
    ("我想知道力量训练每周安排几天比较合适？", "answered"),
    ("我想问一下力量训练每周推荐几次？", "answered"),
]


def business_rows(app):
    with app.state.database.connect() as connection:
        return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table)]
                for table in ("profiles", "meals", "workouts", "coach_memory_entries")}


@pytest.mark.parametrize("question,status", FREQUENCY_QUESTIONS)
def test_general_frequency_reaches_main_chat_evidence_without_fact_writes(knowledge_chat, question, status):
    client, app, model = knowledge_chat
    before = business_rows(app)
    model.output = json.dumps({"status": status, "chunk_ids": ["cdc-adults:strength"] if status == "answered" else []})
    data = send(client, conversation(client), question).json()
    assert data["request_kind"] == "knowledge" and not data["pending"]
    response = answer(client, data)
    assert response.status_code == 200, response.text
    result = response.json()["turns"][-1]["response"]["knowledge"]
    assert result["status"] == status
    if status == "answered":
        assert result["sources"][0]["chunk_id"] == "cdc-adults:strength"
        assert result["sources"][0]["url"] and result["sources"][0]["scope"]
    else:
        assert result["sources"] == []
    assert model.messages[0]["question"] == question
    assert business_rows(app) == before
    assert calls(app) == 1


@pytest.mark.parametrize("question,status", FREQUENCY_QUESTIONS)
def test_direct_knowledge_scope_uses_same_general_frequency_boundary(qa, question, status):
    client, app, model, _ = qa
    model.output = json.dumps({"status": status, "chunk_ids": ["cdc-adults:strength"] if status == "answered" else []})
    response = ask(client, question)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == status
    assert bool(result["sources"]) == (status == "answered")
    assert model.messages[0]["question"] == question and calls(app) == 1


@pytest.mark.parametrize("question,status", FREQUENCY_QUESTIONS)
def test_understanding_guard_accepts_general_frequency_without_plan_operations(question, status):
    result = CoachUnderstanding.model_validate({
        "scope": "knowledge", "command": "", "clarification": "none",
        "notes": [{"version": 1, "avoid": [], "preferences": [], "training_caution": False,
                   "diet_caution": False, "unresolved": False}],
    })
    facts = {"version": 1, "day": "2026-10-17", "messages": [{"version": 1, "message": question}]}
    validated = CoachReviewService.validate(result, facts)
    assert validated["scope"] == "knowledge" and validated["clarification"] == "none"


@pytest.mark.parametrize("message", [
    "我今天怎么安排训练", "结合我的记录安排今天", "一般成年人每周几天力量训练，帮我安排今天",
    "请根据训练记录推荐每周几次力量训练", "三分化计划怎么安排",
    "一般成年人每周如何安排力量训练计划", "近期肩伤，一般成年人每周推荐几次力量训练",
    "一般成年人每周安排几天力量训练，我对牛肉过敏", "每周推荐几次力量训练，只有哑铃",
    "他每周推荐几次力量训练？", "28岁、75公斤，每周推荐几次力量训练？",
    "每周推荐几次力量训练，周二练胸周四练背",
    "我想了解适合我的力量训练每周安排几次", "我想知道结合我的记录每周推荐几次力量训练",
])
def test_general_words_do_not_erase_personal_requests_or_health(knowledge_chat, monkeypatch, message):
    client, app, model = knowledge_chat
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda _: model)
    before = business_rows(app)
    data = send(client, conversation(client), message).json()
    assert data["request_kind"] != "knowledge"
    assert answer(client, data).status_code == 409
    result = ask(client, message).json()
    assert result["status"] == "personal_scope"
    assert result["sources"] == [] and not model.messages and calls(app) == 0
    assert business_rows(app) == before


@pytest.mark.parametrize("message", ["我今天怎么安排训练", "结合我的记录安排今天"])
def test_personal_training_can_still_be_understood_as_training(knowledge_chat, monkeypatch, message):
    from dataclasses import replace
    client, app, model = knowledge_chat
    app.state.settings = replace(app.state.settings, coach_enabled=True)
    understanding = ReviewModel()
    understanding.edit = lambda _, result: result.update(scope="strength", command="")
    monkeypatch.setattr(coach_api, "create_coach_model", lambda _: understanding)
    before = business_rows(app)
    data = send(client, conversation(client), message).json()
    response = understand(client, data, auto_apply=True)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["action"] == "training" and not result["pending"]
    assert not model.messages and len(understanding.messages) == 1
    assert business_rows(app) == before


@pytest.mark.parametrize("profile", [{"preferences": "近期肩伤"}, {"preferences": "近期肩部疼痛"}])
def test_recent_injury_profile_still_blocks_general_frequency(knowledge_chat, profile):
    client, app, model = knowledge_chat
    assert client.put("/api/profile", json=profile).status_code == 200
    data = send(client, conversation(client), FREQUENCY_QUESTIONS[0][0]).json()
    result = answer(client, data).json()["turns"][-1]["response"]["knowledge"]
    assert result["status"] == "personal_scope" and result["sources"] == []
    assert not model.messages and calls(app) == 0


def test_pending_injury_is_not_consumed_by_a_general_frequency_question(knowledge_chat):
    client, app, model = knowledge_chat
    first = send(client, conversation(client), "近期肩伤，稍后补充").json()
    data = send(client, first, FREQUENCY_QUESTIONS[0][0]).json()
    result = answer(client, data).json()
    assert result["pending"]
    assert result["turns"][-1]["response"]["knowledge"]["status"] == "context_pending"
    assert not model.messages and calls(app) == 0


@pytest.mark.parametrize("second_page,status,judgments", [([SECOND], "answered", 2), ([], "insufficient", 1)])
def test_new_frequency_route_keeps_bounded_retrieval_and_original_question(knowledge_chat, monkeypatch, second_page, status, judgments):
    client, app, _ = knowledge_chat
    question = FREQUENCY_QUESTIONS[0][0]
    retriever = Pages(app.state.knowledge, [[], second_page])
    model = ScriptedModel(revise("一周力量训练频率"), answered())
    app.state.knowledge = retriever
    monkeypatch.setattr(coach_api, "create_knowledge_model", lambda _: model)
    before = business_rows(app)
    data = send(client, conversation(client), question).json()
    response = answer(client, data)
    assert response.status_code == 200, response.text
    result = response.json()["turns"][-1]["response"]["knowledge"]
    assert result["status"] == status
    assert result["retrieval"]["calls"] == {"retrieval": 2, "embedding": 2, "judgment": judgments}
    assert [item["question"] for item in model.messages] == [question] * judgments
    assert len(retriever.queries) == 2
    assert answer(client, data).status_code == 200  # HTTP retry reuses the saved result.
    assert len(retriever.queries) == 2 and len(model.messages) == judgments
    assert business_rows(app) == before


@pytest.mark.parametrize("question", [
    "一般力量训练如何降低运动损伤风险？",
    "一般力量训练如何预防肌肉拉伤？",
    "近期训练资料如何解释运动损伤的预防？",
])
def test_general_prevention_is_not_an_injury_declaration(knowledge_chat, monkeypatch, question):
    client, app, model = knowledge_chat
    monkeypatch.setattr(knowledge_api, "create_knowledge_model", lambda _: model)
    model.output = json.dumps({"status": "insufficient", "chunk_ids": []})
    before = business_rows(app)
    data = send(client, conversation(client), question).json()
    assert data["request_kind"] == "knowledge" and not data["pending"]
    result = answer(client, data).json()["turns"][-1]["response"]["knowledge"]
    assert result["status"] == "insufficient" and not result["sources"]
    direct = ask(client, question).json()
    assert direct["status"] == "insufficient" and not direct["sources"]
    understanding = CoachUnderstanding.model_validate({
        "scope": "knowledge", "command": "", "clarification": "none",
        "notes": [{"version": 1, "avoid": [], "preferences": [], "training_caution": False,
                   "diet_caution": False, "unresolved": False}],
    })
    assert CoachReviewService.validate(understanding, {
        "version": 1, "day": "2026-10-17", "messages": [{"version": 1, "message": question}],
    })["clarification"] == "none"
    assert len(model.messages) == calls(app) == 2
    assert business_rows(app) == before
