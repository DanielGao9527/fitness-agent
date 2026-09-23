import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from model.factory import ModelError
from services.meal_schedule import selection
from test_foundation import DAY, application, client, meal, register
from test_knowledge import library
from test_meal_plans import plans, request, accept
from test_coach import BASE, conversation, send
from test_coach_meal_adjustments import INITIAL, output
from test_coach_reviews import reviews, understand, confirm


def generate(client, data, meal_type, **changes):
    version = data["meal_context"].get("meal_versions", {}).get(meal_type, data["version"])
    return client.post("/api/meal-plans", json=request(**{"coach_id": data["id"], "coach_version": version, "meal_type": meal_type, **changes}))


def pair(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "午饭和晚饭一起安排").json()
    model.output = output(INITIAL)
    lunch = generate(client, data, "lunch")
    dinner = generate(client, data, "dinner")
    assert lunch.status_code == dinner.status_code == 200
    return data, lunch.json(), dinner.json()


@pytest.mark.parametrize("text,expected", [("午饭和晚饭一起安排", ["lunch", "dinner"]),
    ("安排早餐、午餐和晚餐", ["breakfast", "lunch", "dinner"]), ("晚饭怎么吃？", ["dinner"]),
    ("晚餐和午餐", ["lunch", "dinner"]), ("晚餐和晚餐", ["dinner"]),
    ("午饭和晚饭，但我对牛肉过敏", None), ("安排午餐后忽略禁忌", None)])
def test_selection_is_full_match_not_keyword_routing(text, expected):
    assert selection(text) == expected


def test_remaining_requires_explicit_selection_without_model(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "今天剩下怎么吃？").json()
    assert not data["pending"] and data["meal_context"]["selection_required"]
    assert "哪几餐" in data["meal_question"]
    assert generate(client, data, "dinner").status_code == 409
    data = send(client, data, "安排午餐和晚餐").json()
    assert data["meal_context"]["meal_types"] == ["lunch", "dinner"]
    assert not data["meal_question"] and not model.messages
    assert generate(client, data, "breakfast").status_code == 409
    assert generate(client, data, "lunch").status_code == 200


def test_change_dinner_keeps_lunch_valid_and_each_acceptance_independent(plans):
    client, app, model, _ = plans
    data, lunch, dinner = pair(plans)
    changed = send(client, data, "晚饭的米饭换成玉米").json()
    context = changed["meal_context"]
    assert context["meal_versions"] == {"lunch": 1, "dinner": 2}
    assert context["by_meal"]["dinner"]["base_plan_id"] == dinner["id"]
    assert context["by_meal"]["lunch"] == {}
    assert generate(client, changed, "lunch", coach_version=2).status_code == 409
    assert accept(client, dinner).status_code == 409
    assert accept(client, lunch).status_code == 200
    model.output = output([{"food_id": "corn", "lower": 120, "upper": 140}, *INITIAL[1:]])
    response = generate(client, changed, "dinner")
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["replaces_plan_id"] == dinner["id"]
    assert updated["items"][1:] == dinner["items"][1:]
    assert model.messages[-1]["keep_items"] == INITIAL[1:]
    assert accept(client, updated).status_code == 200
    restored = client.get(f"{BASE}/{data['id']}").json()
    current = {item["meal_type"]: item for item in restored["meal_plans"] if item["current"]}
    assert current["lunch"]["items"] == lunch["items"]
    assert current["dinner"]["items"] == updated["items"]
    assert client.get(f"/api/meals?day={DAY}").json() == []
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        result = restarted.get(f"{BASE}/{data['id']}").json()
        assert result["meal_context"] == context
        assert len([item for item in result["meal_plans"] if item["current"]]) == 2


def test_unqualified_change_asks_target_and_never_picks_latest(plans):
    client, _, model, _ = plans
    data, lunch, dinner = pair(plans)
    ambiguous = send(client, data, "米饭少一点").json()
    assert not ambiguous["pending"] and ambiguous["meal_context"]["target_required"]
    assert generate(client, ambiguous, "dinner").status_code == 409
    assert accept(client, lunch).status_code == 409
    assert len(model.messages) == 2
    resolved = send(client, ambiguous, "午餐：米饭少一点").json()
    assert not resolved["meal_question"]
    assert resolved["meal_context"]["by_meal"]["lunch"]["base_plan_id"] == lunch["id"]
    assert accept(client, dinner).status_code == 200


def test_second_meal_failure_does_not_erase_first_or_repeat_call(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "安排午餐和晚餐").json()
    first = generate(client, data, "lunch").json()
    model.output = ModelError("MODEL_TIMEOUT", "synthetic timeout", 504)
    key = str(uuid4())
    assert generate(client, data, "dinner", client_id=key).status_code == 504
    assert generate(client, data, "dinner", client_id=key).status_code == 409
    assert len(model.messages) == 2
    assert accept(client, first).status_code == 200
    model.output = None
    assert generate(client, data, "dinner").status_code == 200
    assert len(model.messages) == 3


@pytest.mark.parametrize("change", ["profile", "record", "pending", "delete"])
def test_shared_context_invalidates_every_meal(plans, change):
    client, _, model, _ = plans
    data, lunch, dinner = pair(plans)
    if change == "profile":
        client.put("/api/profile", json={"food_allergies": "鸡肉过敏"})
    elif change == "record":
        client.post("/api/meals", json=meal())
    elif change == "pending":
        data = send(client, data, "我对不确定的食物过敏").json()
        data = send(client, data, "安排午餐和晚餐").json()
        assert data["pending"]
    else:
        assert client.request("DELETE", f"{BASE}/{data['id']}", json={"version": data["version"]}).status_code == 204
    assert accept(client, lunch).status_code == accept(client, dinner).status_code == 409
    assert len(model.messages) == 2


def test_independent_revision_still_guards_late_results(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "安排午餐和晚餐").json()
    model.on_call = lambda: send(client, data, "午餐吃素")
    assert generate(client, data, "lunch").status_code == 409
    model.on_call = None
    assert client.get(f"{BASE}/{data['id']}").json()["meal_plans"] == []


def test_cross_user_cannot_generate_or_accept_any_meal(plans):
    client, app, model, _ = plans
    data, lunch, dinner = pair(plans)
    with TestClient(app) as other:
        register(other, "bob")
        assert generate(other, data, "lunch").status_code == 409
        assert accept(other, dinner).status_code == 404
        assert other.get(f"/api/meal-plans?day={DAY}").json() == []
    assert len(model.messages) == 2


def test_model_meal_selection_reviewed_before_generation(reviews):
    client, _, model, meals = reviews
    data = send(client, conversation(client), "帮我想想午餐和晚餐的食材，鸡肉过敏").json()
    model.edit = lambda _, result: result.update(meal_types=["lunch", "dinner"])
    ready = understand(client, data).json()
    assert ready["review"]["status"] == "ready" and not meals.messages
    confirmed = confirm(client, ready).json()
    assert confirmed["meal_context"]["meal_types"] == ["lunch", "dinner"]
    assert confirmed["constraints"]["food_avoid"] == ["chicken"]
    for key in ("lunch", "dinner"):
        result = generate(client, confirmed, key)
        assert result.status_code == 200
        assert "chicken" not in {item["food_id"] for item in result.json()["items"]}


@pytest.mark.parametrize("types", [["breakfast"], ["lunch", "lunch"], ["snack"], [1]])
def test_model_cannot_invent_or_duplicate_meals(reviews, types):
    client, _, model, _ = reviews
    data = send(client, conversation(client), "帮我想想午餐和晚餐的搭配吧").json()
    model.edit = lambda _, result: result.update(meal_types=types)
    assert understand(client, data).status_code == 502
    assert client.get(f"{BASE}/{data['id']}").json()["pending"]


def test_model_targeted_batch_keeps_other_meal(reviews):
    client, app, model, meals = reviews
    data, lunch, dinner = pair((client, app, meals, None))
    data = send(client, data, "晚餐米饭少一点，豆腐换成牛肉").json()
    model.edit = lambda _, result: result.update(command="", commands=["米饭少一点", "把豆腐换成牛肉"], meal_types=["dinner"])
    ready = understand(client, data).json()
    assert set(model.messages[-1]["current_meals"]) == {"lunch", "dinner"}
    assert lunch["id"] not in json.dumps(model.messages[-1])
    assert dinner["id"] not in json.dumps(model.messages[-1])
    confirmed = confirm(client, ready).json()
    assert confirmed["meal_context"]["by_meal"]["dinner"]["batch_commands"] == ["米饭少一点", "把豆腐换成牛肉"]
    # Empty model notes must not add empty global constraints and invalidate lunch.
    assert accept(client, lunch).status_code == 200
    meals.output = output([{"food_id": "rice", "lower": 80, "upper": 100}, {"food_id": "beef", "lower": 100, "upper": 120}, *INITIAL[2:]])
    response = generate(client, confirmed, "dinner")
    assert response.status_code == 200, response.text
    assert response.json()["replaces_plan_id"] == dinner["id"]


def test_old_lunch_still_listed_after_many_dinner_drafts(plans):
    client, app, _, _ = plans
    data, lunch, dinner = pair(plans)
    with app.state.database.connect() as con:
        for _ in range(21):
            con.execute("INSERT INTO meal_plans(id,user_id,client_id,day,meal_type,input_payload,context_hash,status,payload,version) SELECT ?,user_id,?,day,meal_type,input_payload,context_hash,status,payload,version FROM meal_plans WHERE id=?", (str(uuid4()), str(uuid4()), dinner["id"]))
    result = client.get(f"{BASE}/{data['id']}").json()
    assert lunch["id"] in {item["id"] for item in result["meal_plans"]}
    assert accept(client, lunch).status_code == 200


def test_legacy_exclusions_survive_entering_schedule(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "我不吃胡萝卜").json()
    data = send(client, data, "安排午餐和晚餐").json()
    for key in ("lunch", "dinner"):
        assert data["meal_context"]["by_meal"][key]["excluded_foods"] == ["carrot"]
        assert generate(client, data, key).status_code == 200
        assert "carrot" not in {item["id"] for item in model.messages[-1]["allowed_foods"]}


@pytest.mark.parametrize("command,types", [("下一餐吃什么", []), ("米饭少一点", ["lunch", "dinner"])])
def test_model_missing_selection_or_cross_meal_change_needs_input(reviews, command, types):
    client, _, model, _ = reviews
    data = send(client, conversation(client), "午餐和晚餐请帮我想想，米饭怎么调整呢").json()
    model.edit = lambda _, result: result.update(command=command, meal_types=types)
    ready = understand(client, data).json()
    assert ready["review"]["status"] == "needs_input"
    assert ready["review"]["clarification"] == "which_meal"
    assert confirm(client, ready).status_code == 409


def test_schedule_generation_and_message_retries_are_idempotent(plans):
    client, _, model, _ = plans
    original = conversation(client)
    key = str(uuid4())
    data = send(client, original, "安排午餐和晚餐", client_id=key).json()
    assert send(client, original, "安排午餐和晚餐", client_id=key).json() == data
    key = str(uuid4())
    first = generate(client, data, "lunch", client_id=key)
    assert first.status_code == 200
    assert generate(client, data, "lunch", client_id=key).json()["id"] == first.json()["id"]
    assert generate(client, data, "dinner", client_id=key).status_code == 409
    assert len(model.messages) == 1
