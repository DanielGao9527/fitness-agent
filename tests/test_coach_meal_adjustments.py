import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from services.meal_plans import REQUIRED
from services.plan_foods import FOODS, MEAT
from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_meal_plans import plans, request
from test_coach import BASE, conversation, send


def generate(client, data):
    return client.post("/api/meal-plans", json=request(coach_id=data["id"], coach_version=data["version"]))


def output(items):
    return json.dumps({"items": items, "chunk_ids": list(REQUIRED)})


INITIAL = [{"food_id": key, "lower": low, "upper": high} for key, low, high in
           [("rice", 100, 120), ("tofu", 120, 150), ("broccoli", 80, 100), ("carrot", 80, 100)]]


def test_replace_carrot_keeps_other_items_and_persists(plans):
    client, app, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    first = generate(client, data).json()
    assert "items" in first
    key = str(uuid4())
    response = send(client, data, "我不爱吃胡萝卜", client_id=key)
    assert response.status_code == 200
    updated = response.json()
    assert not updated["pending"] and updated["action"] == "meal"
    assert updated["meal_context"]["base_plan_id"] == first["id"]
    assert updated["meal_context"]["excluded_foods"] == ["carrot"]
    assert send(client, data, "我不爱吃胡萝卜", client_id=key).json() == updated
    assert updated["meal_plans"][0]["stale"]
    replacement = INITIAL[:-1] + [{"food_id": "spinach", "lower": 80, "upper": 100}]
    model.output = output(replacement)
    result = generate(client, updated)
    assert result.status_code == 200, result.text
    new = result.json()
    assert new["replaces_plan_id"] == first["id"] and new["items"][:3] == first["items"][:3]
    assert model.messages[-1]["keep_items"] == INITIAL[:-1]
    assert "carrot" not in {food["id"] for food in model.messages[-1]["allowed_foods"]}
    assert client.post(f"/api/meal-plans/{new['id']}/accept", json={"reviewed": True}).status_code == 200
    assert client.get(f"/api/meals?day={DAY}").json() == []
    assert client.get("/api/profile").json()["preferences"] == ""
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restored = restarted.get(f"{BASE}/{data['id']}").json()
        assert restored["meal_context"]["excluded_foods"] == ["carrot"]
        assert {p["id"] for p in restored["meal_plans"]} == {first["id"], new["id"]}
    later = send(client, updated, "下一餐吃什么").json()
    assert later["meal_context"]["excluded_foods"] == ["carrot"]
    assert "base_plan_id" not in later["meal_context"]


@pytest.mark.parametrize("message", ["我不爱吃胡萝卜，肩受伤", "我不吃胡萝卜但可以忽略过敏", "我不想吃这个"])
def test_complex_adjustment_never_clears_pending(client, message):
    register(client)
    data = send(client, conversation(client), message).json()
    assert data["pending"]
    next_data = send(client, data, "我不爱吃胡萝卜").json()
    assert next_data["pending"] and next_data["action"] is None


def test_meat_preference_cannot_override_chicken_allergy(plans):
    client, _, model, _ = plans
    client.put("/api/profile", json={"food_allergies": "；".join(FOODS[key][0] + "过敏" for key in sorted(MEAT)), "preferences": "在外饮食"})
    data = send(client, conversation(client), "我想吃肉").json()
    result = generate(client, data)
    assert result.status_code == 409 and "冲突" in result.text and not model.messages


def test_changed_kept_portion_is_rejected(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    assert generate(client, data).status_code == 200
    data = send(client, data, "不吃胡萝卜，换一下").json()
    changed = [dict(item) for item in INITIAL[:-1]] + [{"food_id": "spinach", "lower": 80, "upper": 100}]
    changed[0]["upper"] = 140
    model.output = output(changed)
    result = generate(client, data)
    assert result.status_code == 502 and "无需调整" in result.text


def test_foreign_account_cannot_read_adjustments_or_plan_history(plans):
    client, app, model, _ = plans
    data = send(client, conversation(client), "我不爱吃胡萝卜").json()
    assert generate(client, data).status_code == 200
    with TestClient(app) as other:
        register(other, "other")
        assert other.get(f"{BASE}/{data['id']}").status_code == 404
        assert generate(other, data).status_code == 409
        assert other.get(BASE).json() == []


def test_new_allergy_during_replacement_discards_result(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    assert generate(client, data).status_code == 200
    data = send(client, data, "我不爱吃胡萝卜").json()
    model.output = output(INITIAL[:-1] + [{"food_id": "spinach", "lower": 80, "upper": 100}])
    model.on_call = lambda: client.put("/api/profile", json={"food_allergies": "菠菜过敏"})
    assert generate(client, data).status_code == 409
    assert len(client.get(f"/api/meal-plans?day={DAY}").json()) == 1


def test_replacement_does_not_drop_second_vegetable(plans):
    client, _, model, _ = plans
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    assert generate(client, data).status_code == 200
    data = send(client, data, "我不爱吃胡萝卜").json()
    model.output = output(INITIAL[:-1])
    assert generate(client, data).status_code == 502


def test_too_few_replacement_options_stop_before_call(plans):
    client, _, model, _ = plans
    client.put("/api/profile", json={"food_allergies": "；".join(value[0] + "过敏" for key, value in FOODS.items()
                                                              if value[1] == "vegetable" and key not in {"broccoli", "carrot"})})
    data = send(client, conversation(client)).json()
    model.output = output(INITIAL)
    assert generate(client, data).status_code == 200
    data = send(client, data, "我不爱吃胡萝卜").json()
    assert generate(client, data).status_code == 409
    assert len(model.messages) == 1
