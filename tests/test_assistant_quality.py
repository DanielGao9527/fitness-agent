import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from model.factory import ModelError
from schemas import CoachTrainingUpdate, CoachUnderstanding
from scripts.check_assistant import Budget
from services.coach_reviews import CoachReviewService, training_update
from services.training_context import merge_training
from services.training_recommendations import denied_equipment
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm
from test_coach_training import training_reviews
from test_foundation import application, client
from test_knowledge import library
from test_meal_plans import plans


def extraction(text, **changes):
    return CoachTrainingUpdate(source_text=text, **changes)


def facts(text):
    return {"messages": [{"version": 1, "message": text}], "current_training": [{"id": "db-bench", "name": "哑铃平板卧推"}]}


@pytest.mark.parametrize("label,key", [("三分化", "ppl"), ("四分化", "four"), ("五分化", "five")])
def test_reviewed_split_clears_old_focus_and_retains_time(label, key):
    text = f"我想试试{label}，帮我安排这一周"
    changes = training_update(extraction(text, split=key, weekly=True), "strength", facts(text))
    result = merge_training({"focus": "胸", "minutes": 40, "time_basis": "session", "replacements": {"db-bench": "barbell-bench"}}, changes)
    assert result["split"] == key and result["weekly"] and result["minutes"] == 40
    assert "focus" not in result and "replacements" not in result


@pytest.mark.parametrize("text,denied", [("没有训练凳", {"seat"}), ("没有卧推凳和保护杠", {"bench", "safeties"}),
    ("哑铃可用，但不能用杠铃", {"barbell"}), ("只有哑铃", set())])
def test_negative_equipment_extraction(text, denied):
    assert denied_equipment(text) == denied


def test_equipment_source_cannot_be_cropped_to_hide_unavailable_items():
    text = "今天是在家练，只有哑铃，没有训练凳，帮我调整一下"
    for source in (text, "只有哑铃"):
        with pytest.raises(ModelError, match="不可用"):
            training_update(extraction(source, equipment="哑铃"), "strength", facts(text))
    changes = training_update(extraction(text, equipment="只有哑铃，没有训练凳"), "strength", facts(text))
    result = merge_training({"replacements": {"db-bench": "barbell-bench"}}, changes)
    assert "replacements" not in result and "没有训练凳" in result["equipment"]


@pytest.mark.parametrize("changes", [{"split": "four"}, {"weekly": True}, {"weekly": False},
    {"replace_from": "哑铃平板卧推"}, {"replace_from": "哑铃平板卧推", "replace_with": "高脚杯深蹲"}])
def test_ungrounded_or_mixed_training_update_is_rejected(changes):
    text = "这次三分化，我想用哑铃平板卧推或者高脚杯深蹲"
    with pytest.raises(ModelError):
        training_update(extraction(text, **changes), "strength", facts(text))


def test_current_same_pattern_replacement_and_readable_names():
    text = "刚才的哑铃平板卧推我想改用杠铃平板卧推，其他动作保持"
    result = training_update(extraction(text, replace_from="哑铃平板卧推", replace_with="杠铃平板卧推"), "strength", facts(text))
    assert result["replacements"] == {"db-bench": "barbell-bench"}
    with pytest.raises(ModelError, match="当前建议"):
        training_update(extraction(text, replace_from="哑铃平板卧推", replace_with="杠铃平板卧推"), "strength", {**facts(text), "current_training": []})
    with pytest.raises(ModelError):
        training_update(extraction(text, replace_from="哑铃平板卧推", replace_with="杠铃平板卧推"), "aerobic", facts(text))


def test_swimming_stays_swimming_and_clears_strength_focus(training_reviews):
    client, _, model, _ = training_reviews
    client.put("/api/profile", json={"food_allergies": "鸡肉过敏", "preferences": "口味偏甜"})
    text = "这次先不练力量了，我想去泳池游一会儿，接下来还有30分钟"
    model.edit = lambda _, out: out.update(scope="aerobic", command="有氧建议", training={"source_text": text,
        "activity": "swim", "minutes": 30, "time_basis": "session"})
    data = send(client, conversation(client), "练胸").json()
    data = send(client, data, text).json()
    ready = understand(client, data).json()
    assert ready["review"]["status"] == "ready"
    assert "food_allergies" not in model.messages[-1]["profile"]
    assert "preferences" not in model.messages[-1]["profile"]
    assert client.get("/api/profile").json()["food_allergies"] == "鸡肉过敏"
    changed = confirm(client, ready).json()
    assert changed["training_context"]["activity"] == "swim" and "focus" not in changed["training_context"]
    assert changed["training_context"]["minutes"] == 30


def test_natural_split_review_profile_context_confirmation_and_stale(training_reviews):
    client, app, model, _ = training_reviews
    client.put("/api/profile", json={"training_split": "five"})
    data = send(client, conversation(client), "练胸").json()
    text = "我还是想试试三分化，你帮我把这一周重新排一下"
    model.edit = lambda _, out: out.update(scope="strength", command="", training={"source_text": text, "split": "ppl", "weekly": True})
    data = send(client, data, text).json()
    ready = understand(client, data).json()
    assert ready["pending"] and ready["training_context"]["focus"] == "胸"
    assert model.messages[-1]["training_facts"]["profile"]["training_split"] == "five"
    changed = confirm(client, ready).json()
    assert changed["training_context"]["split"] == "ppl" and "focus" not in changed["training_context"]
    assert client.get("/api/profile").json()["training_split"] == "five"
    assert confirm(client, ready).json() == changed
    next_data = send(client, changed, text).json()
    next_ready = understand(client, next_data).json()
    client.put("/api/profile", json={"training_split": "four"})
    assert confirm(client, next_ready).status_code == 409
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 0


def test_omitted_split_is_not_silently_applied(training_reviews):
    client, _, model, _ = training_reviews
    model.edit = lambda _, out: out.update(scope="strength", command="", training=None)
    data = send(client, conversation(client), "我想改为三分化，安排一下").json()
    ready = understand(client, data).json()
    assert ready["review"]["status"] == "needs_input"
    assert ready["review"]["clarification"] == "which_training"
    assert confirm(client, ready).status_code == 409


@pytest.mark.parametrize("structured", [False, True])
def test_colloquial_dinner_changes_preserve_lunch(reviews, structured):
    client, _, model, _ = reviews
    data = send(client, conversation(client), "安排午餐和晚餐").json()
    lunch_version = data["meal_context"]["meal_versions"]["lunch"]
    text = "晚上想吃点虾肉，主食想换成玉米，中午那份别动"
    model.edit = lambda _, out: out.update(command="", commands=["我想吃虾肉", "我想吃玉米"], meal_types=["dinner"])
    if structured:
        model.edit = lambda _, out: out.update(command="", meal_changes=[{"meal_type": "dinner",
            "source_text": "晚上想吃点虾肉，主食想换成玉米", "commands": ["我想吃虾肉", "我想吃玉米"]}], meal_types=["dinner"])
    data = send(client, data, text).json()
    ready = understand(client, data).json()
    assert ready["review"]["status"] == "ready"
    changed = confirm(client, ready).json()
    context = changed["meal_context"]
    assert context["meal_versions"]["lunch"] == lunch_version
    assert context["by_meal"]["dinner"]["group_choices"] == {"protein": ["shrimp"], "starch": ["corn"]}


def test_swimming_drops_old_week_and_replacements_not_minutes():
    result = merge_training({"split": "five", "weekly": True, "focus": "胸", "replacements": {"a": "b"},
                             "minutes": 30, "time_basis": "session"}, {"kind": "aerobic", "activity": "swim"})
    assert result == {"kind": "aerobic", "activity": "swim", "minutes": 30, "time_basis": "session"}


def test_time_without_explicit_meal_cannot_become_dinner(reviews):
    client, _, model, _ = reviews
    model.edit = lambda _, out: out.update(meal_types=["dinner"])
    data = send(client, conversation(client), "晚点再安排吧").json()
    assert understand(client, data).status_code == 502


def test_separate_live_budget_counts_failures_persists_and_caps_parallel_requests(tmp_path):
    path = tmp_path / "calls.sqlite3"
    budget = Budget(path)
    first = budget.reserve("synthetic")
    budget.finish(first, "MODEL_TIMEOUT")
    assert Budget(path).count() == 1
    def reserve(_):
        try:
            return budget.reserve("synthetic")
        except ModelError as error:
            assert error.code == "TEST_BUDGET_EXHAUSTED"
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(reserve, range(33)))
    assert sum(value is not None for value in result) == 29
    assert Budget(path).count() == 30
