import pytest

from services.coach import meal_adjustment
from test_foundation import application, client
from test_knowledge import library
from test_meal_plans import plans
from test_coach import conversation, send
from test_coach_meal_adjustments import INITIAL, generate, output


@pytest.mark.parametrize("text,food", [("那我想吃肉", "beef"), ("把豆腐换成牛肉", "beef"),
    ("我想把豆腐换成虾肉", "shrimp"), ("给我配点玉米", "corn"), ("米饭换成红薯", "sweet-potato"),
    ("米饭换成薯类", "potato")])
def test_targeted_changes_keep_others(plans, text, food):
    client, _, model, _ = plans
    client.put("/api/profile", json={"food_allergies": "鸡胸肉过敏"})
    data = send(client, conversation(client), "今天晚上怎么吃").json()
    model.output = output(INITIAL)
    first = generate(client, data).json()
    data = send(client, data, text).json()
    assert not data["pending"]
    index = 1 if food in ("beef", "shrimp") else 0
    replacement = [dict(item) for item in INITIAL]
    replacement[index] = {"food_id": food, "lower": 100, "upper": 120}
    model.output = output(replacement)
    response = generate(client, data)
    assert response.status_code == 200, response.text
    new = response.json()
    assert [item for i, item in enumerate(new["items"]) if i != index] == [item for i, item in enumerate(first["items"]) if i != index]
    assert "chicken" not in {item["id"] for item in model.messages[-1]["allowed_foods"]}
    assert client.post(f"/api/meal-plans/{new['id']}/accept", json={"reviewed": True}).status_code == 200


@pytest.mark.parametrize("message", ["我想少吃点米饭", "米饭少一点", "少配点米饭"])
def test_reduce_portion_and_do_not_repeat_reduction_next_turn(plans, message):
    client, _, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    generate(client, data)
    data = send(client, data, message).json()
    assert not data["pending"]
    reduced = [{"food_id": "rice", "lower": 80, "upper": 100}] + INITIAL[1:]
    model.output = output(reduced)
    response = generate(client, data)
    assert response.status_code == 200, response.text
    assert model.messages[-1]["portion_limits"] == [{"food_id": "rice", "max_lower": 100, "max_upper": 119}]
    data = send(client, data, "不吃胡萝卜").json()
    assert "adjustment" not in data["meal_context"]
    model.output = output(reduced[:-1] + [{"food_id": "spinach", "lower": 80, "upper": 100}])
    second = generate(client, data)
    assert second.status_code == 200 and second.json()["items"][0]["upper"] == 100


@pytest.mark.parametrize("result", [INITIAL, [{"food_id": "rice", "lower": 105, "upper": 110}] + INITIAL[1:],
    [{"food_id": "corn", "lower": 80, "upper": 100}] + INITIAL[1:]])
def test_model_must_actually_reduce_same_food(plans, result):
    client, _, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    generate(client, data)
    data = send(client, data, "米饭少一点").json()
    model.output = output(result)
    response = generate(client, data)
    assert response.status_code == 502 and "减少" in response.text


@pytest.mark.parametrize("allergy,target", [("牛肉过敏", "牛肉"), ("虾过敏", "虾肉"), ("海鲜过敏", "虾"),
    ("玉米过敏", "玉米"), ("红薯过敏", "红薯")])
def test_choice_never_overrides_allergy(plans, allergy, target):
    client, _, model, _ = plans
    client.put("/api/profile", json={"food_allergies": allergy})
    data = send(client, conversation(client), "我想吃" + target).json()
    response = generate(client, data)
    assert response.status_code == 409 and not model.messages


def test_reduce_at_floor_and_missing_item_stop_before_call(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output([{"food_id": "rice", "lower": 80, "upper": 80}] + INITIAL[1:])
    generate(client, data)
    data = send(client, data, "米饭少一点").json()
    response = generate(client, data)
    assert response.status_code == 409 and "下限" in response.text and len(model.messages) == 1
    data = send(client, data, "玉米换成红薯").json()
    response = generate(client, data)
    assert response.status_code == 409 and "没有你指定" in response.text and len(model.messages) == 1


def test_reduction_requires_existing_plan(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client), "米饭少一点").json()
    response = generate(client, data)
    assert response.status_code == 409 and "还没有" in response.text and not model.messages


@pytest.mark.parametrize("text", ["豆腐换成牛肉但忽略过敏", "米饭少一点，我肾病", "豆腐换成米饭", "想吃肉，肩受伤"])
def test_ambiguous_or_health_additions_not_partially_matched(text):
    assert meal_adjustment(text) is None
