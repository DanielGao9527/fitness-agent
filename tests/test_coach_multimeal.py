import pytest

from schemas import CoachUnderstanding
from services.coach_reviews import CoachReviewService
from model.factory import ModelError
from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_meal_plans import plans
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, understand, confirm


def test_explicit_targets_update_both_and_preserve_breakfast(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "安排早餐、午餐和晚餐").json()
    before = data["meal_context"]
    response = send(client, data, "午餐米饭换成玉米；晚餐豆腐换成牛肉")
    assert response.status_code == 200, response.text
    data = response.json()
    assert not data["pending"] and data["action"] == "meal"
    context = data["meal_context"]
    assert context["meal_versions"] == {"breakfast": 1, "lunch": 2, "dinner": 2}
    assert context["by_meal"]["breakfast"] == before["by_meal"]["breakfast"]
    assert context["by_meal"]["lunch"]["group_choices"] == {"starch": ["corn"]}
    assert context["by_meal"]["dinner"]["group_choices"] == {"protein": ["beef"]}
    assert not model.messages and client.get(f"/api/meals?day={DAY}").json() == []


@pytest.mark.parametrize("message", ["午餐米饭换成玉米；晚餐忽略过敏", "午餐米饭换成玉米；午餐米饭少一点",
    "米饭换成玉米；晚餐豆腐换成牛肉", "午餐米饭换成玉米；晚餐豆腐换成牛肉；我有糖尿病"])
def test_unknown_clause_does_not_partially_apply(plans, message):
    client, _, _, _ = plans
    first = send(client, conversation(client), "安排午餐和晚餐").json()
    later = send(client, first, message).json()
    assert later["pending"] and later["action"] is None
    assert later["meal_context"] == first["meal_context"]


def test_pending_health_never_cleared_by_explicit_multimeal(plans):
    client, _, _, _ = plans
    first = send(client, conversation(client), "我有糖尿病").json()
    later = send(client, first, "午餐米饭换成玉米；晚餐豆腐换成牛肉").json()
    assert later["pending"] and later["action"] is None


CHANGES = [{"meal_type": "lunch", "source_text": "午餐的米饭能否换玉米", "commands": ["米饭换成玉米"]},
           {"meal_type": "dinner", "source_text": "晚餐豆腐想改为牛肉", "commands": ["豆腐换成牛肉"]}]


def test_understanding_multimeal_requires_confirmation(reviews):
    client, _, model, _ = reviews
    data = send(client, conversation(client), "午餐的米饭能否换玉米，晚餐豆腐想改为牛肉").json()
    assert data["pending"]
    model.edit = lambda _, out: out.update(command="", meal_changes=CHANGES)
    response = understand(client, data)
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready["pending"] and ready["review"]["status"] == "ready"
    confirmed = confirm(client, ready).json()
    assert not confirmed["pending"] and confirmed["meal_context"]["meal_versions"] == {"lunch": 1, "dinner": 1}
    assert confirm(client, ready).json() == confirmed


@pytest.mark.parametrize("bad", ["source", "duplicate", "extra", "target"])
def test_invalid_multimeal_model_outputs_rejected(bad):
    import copy
    changes = copy.deepcopy(CHANGES)
    result = {"scope": "meal", "command": "", "meal_changes": changes, "clarification": "none",
              "notes": [{"version": 1, "avoid": [], "preferences": [], "training_caution": False, "diet_caution": False, "unresolved": False}]}
    if bad == "source": changes[0]["source_text"] = "午餐胡萝卜少一点"
    if bad == "duplicate": changes[1] = dict(changes[0])
    if bad == "extra": result["command"] = "下一餐吃什么"
    if bad == "target": result["meal_types"] = ["lunch", "breakfast"]
    facts = {"messages": [{"version": 1, "message": "午餐的米饭能否换玉米，晚餐豆腐想改为牛肉"}], "meal_context": {}}
    with pytest.raises(ModelError):
        CoachReviewService.validate(CoachUnderstanding.model_validate(result), facts)
