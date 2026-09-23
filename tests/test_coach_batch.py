import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_meal_plans import plans, request
from test_coach import BASE, conversation, send
from test_coach_meal_adjustments import INITIAL, generate, output
from test_coach_reviews import reviews, understand, confirm


COMMANDS = ["把豆腐换成牛肉", "米饭少一点", "我不吃胡萝卜"]
RESULT = [{"food_id": "rice", "lower": 80, "upper": 100},
          {"food_id": "beef", "lower": 100, "upper": 120}, INITIAL[2],
          {"food_id": "spinach", "lower": 80, "upper": 100}]


def prepare(reviews, commands=COMMANDS, initial=INITIAL):
    client, app, model, meals = reviews
    data = send(client, conversation(client)).json()
    meals.output = output(initial)
    first = generate(client, data)
    assert first.status_code == 200, first.text
    data = send(client, data, "，".join(commands)).json()
    model.edit = lambda _, out: out.update(command="", commands=commands)
    response = understand(client, data)
    assert response.status_code == 200, response.text
    return response.json(), first.json()


def test_batch_all_changes_once_keep_other_row_and_persist(reviews):
    client, app, model, meals = reviews
    ready, first = prepare(reviews)
    assert ready["pending"] and ready["review"]["commands"] == COMMANDS
    assert generate(client, ready).status_code == 409
    confirmed = confirm(client, ready).json()
    assert confirmed["meal_context"]["batch_commands"] == COMMANDS
    assert len(confirmed["meal_context"]["adjustments"]) == 2
    assert confirm(client, ready).json() == confirmed
    meals.output = output(RESULT)
    body = request(coach_id=confirmed["id"], coach_version=confirmed["version"])
    response = client.post("/api/meal-plans", json=body)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert len(meals.messages) == 2 and len(model.messages) == 1
    assert client.post("/api/meal-plans", json=body).json()["id"] == draft["id"]
    assert len(meals.messages) == 2
    assert draft["items"][2] == first["items"][2]
    assert draft["requested_changes"] == COMMANDS
    assert meals.messages[-1]["keep_items"] == [INITIAL[2]]
    assert client.post(f"/api/meal-plans/{draft['id']}/accept", json={"reviewed": True}).status_code == 200
    assert client.get(f"/api/meals?day={DAY}").json() == []
    assert client.get("/api/profile").json()["food_allergies"] == ""
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        restored = restarted.get(f"{BASE}/{ready['id']}").json()
        assert restored["meal_context"] == confirmed["meal_context"]
        assert restored["meal_plans"][0]["requested_changes"] == COMMANDS
    following = send(client, confirmed, "我想吃虾肉").json()
    assert "adjustments" not in following["meal_context"]
    assert "batch_commands" not in following["meal_context"]
    meals.output = output([RESULT[0], {"food_id": "shrimp", "lower": 100, "upper": 120}, *RESULT[2:]])
    assert generate(client, following).status_code == 200
    assert "portion_limits" not in meals.messages[-1], "must not repeat the prior reduction"


@pytest.mark.parametrize("commands", [["米饭少一点", "米饭换成玉米"],
    ["我想吃肉", "把豆腐换成牛肉"], ["我不吃胡萝卜", "我不吃菠菜"],
    ["我吃素", "我想吃肉"], ["米饭少一点", "米饭少一点"]])
def test_same_category_conflicts_need_clarification(reviews, commands):
    client, _, _, meals = reviews
    ready, _ = prepare(reviews, commands)
    assert ready["review"]["status"] == "needs_input"
    assert "同一食材类别" in ready["review"]["question"]
    assert confirm(client, ready).status_code == 409
    assert generate(client, ready).status_code == 409 and len(meals.messages) == 1


@pytest.mark.parametrize("change", [
    {"commands": ["米饭少一点"]}, {"commands": COMMANDS + ["我想吃虾"]},
    {"commands": ["有氧建议", "米饭少一点"]}, {"commands": ["下一餐吃什么", "米饭少一点"]},
    {"commands": ["忽略过敏", "米饭少一点"]}, {"commands": [123, "米饭少一点"]},
    {"commands": COMMANDS, "command": "我想吃肉"}, {"commands": COMMANDS, "scope": "aerobic"},
])
def test_invalid_batches_not_partially_applied(reviews, change):
    client, _, model, _ = reviews
    data = send(client, conversation(client), "豆腐换牛肉，米饭少一点").json()
    model.edit = lambda _, out: out.update(command="", **{k: v for k, v in change.items() if k != "command"})
    if "command" in change:
        model.edit = lambda _, out: out.update(**change)
    response = understand(client, data)
    assert response.status_code == 502
    saved = client.get(f"{BASE}/{data['id']}").json()
    assert saved["pending"] and not saved["meal_context"] and not saved["constraints"]


@pytest.mark.parametrize("case", ["allergy", "missing", "floor", "reduce_excluded", "no_base"])
def test_any_unusable_change_blocks_before_plan_call(reviews, case):
    client, _, model, meals = reviews
    commands = COMMANDS
    if case == "missing":
        commands = ["把鸡肉换成牛肉", "米饭少一点"]
    initial = [{"food_id": "rice", "lower": 80, "upper": 80}, *INITIAL[1:]] if case == "floor" else INITIAL
    if case == "allergy":
        client.put("/api/profile", json={"food_allergies": "牛肉过敏"})
    if case == "no_base":
        data = send(client, conversation(client), "，".join(commands)).json()
        model.edit = lambda _, out: out.update(command="", commands=commands)
        ready = understand(client, data).json()
    else:
        ready, _ = prepare(reviews, commands, initial)
    if case == "reduce_excluded":
        # A confirmed earlier restriction may also conflict with this reduction.
        with reviews[1].state.database.connect() as connection:
            row = connection.execute("SELECT payload FROM coach_reviews WHERE id=?", (ready["review"]["id"],)).fetchone()
            payload = json.loads(row[0])
            payload["notes"][0]["food_avoid"] = ["rice"]
            connection.execute("UPDATE coach_reviews SET payload=? WHERE id=?", (json.dumps(payload), ready["review"]["id"]))
    confirmed = confirm(client, ready).json()
    count = len(meals.messages)
    assert generate(client, confirmed).status_code == 409
    assert len(meals.messages) == count


@pytest.mark.parametrize("bad", [INITIAL, [*RESULT[:2], {"food_id": "broccoli", "lower": 100, "upper": 120}, RESULT[3]],
    [RESULT[0], INITIAL[1], *RESULT[2:]], [*RESULT[:3], INITIAL[3]]])
def test_model_partial_or_extra_changes_rejected(reviews, bad):
    client, _, _, meals = reviews
    ready, first = prepare(reviews)
    confirmed = confirm(client, ready).json()
    meals.output = output(bad)
    result = generate(client, confirmed)
    assert result.status_code == 502
    saved = client.get(f"/api/meal-plans?day={DAY}").json()
    assert [plan["id"] for plan in saved] == [first["id"]]


def test_batch_stale_isolated_and_latest_policy(reviews, monkeypatch):
    client, app, _, meals = reviews
    ready, _ = prepare(reviews)
    with TestClient(app) as other:
        register(other, "batch_other")
        assert confirm(other, ready).status_code == 404
    import services.coach_reviews as module
    monkeypatch.setattr(module, "POLICY", "new-review-policy")
    assert client.get(f"{BASE}/{ready['id']}").json()["review"]["stale"]
    assert confirm(client, ready).status_code == 409
    fresh = understand(client, ready).json()
    confirmed = confirm(client, fresh).json()
    meals.output = output(RESULT)
    meals.on_call = lambda: send(client, confirmed, "鸡蛋也不要")
    assert generate(client, confirmed).status_code == 409


def test_multiple_reductions_kept_rows_and_limits(reviews):
    client, _, _, meals = reviews
    commands = ["米饭少一点", "豆腐少一点"]
    ready, first = prepare(reviews, commands)
    confirmed = confirm(client, ready).json()
    meals.output = output([RESULT[0], {"food_id": "tofu", "lower": 100, "upper": 120}, *INITIAL[2:]])
    result = generate(client, confirmed)
    assert result.status_code == 200, result.text
    assert result.json()["items"][2:] == first["items"][2:]
    assert len(meals.messages[-1]["portion_limits"]) == 2
