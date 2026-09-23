from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from services.business_time import business_today
from services.meal_balance import balance, evaluate
from services.meal_variety import recent_foods, quality, STAPLE_PORTIONS
from services import meal_nutrient_reference as nutrients
from services.plan_foods import ON_REQUEST_FOODS
from test_foundation import application, client, register, meal
from test_meal_balance import item
from test_quick_meals import quick, plans, library, ask, recommend, current_plans
from test_coach import BASE


def test_macro_centers_remain_inside_their_ranges_across_supported_inputs():
    data = nutrients.reference_data()
    for mode in ("general", "regular_training"):
        for weight in range(40, 201, 5):
            for kcal in range(1250, 5001, 50):
                ref = nutrients.macro_reference({"weight_kg": weight, "nutrition_reference": mode}, kcal, data)
                if ref["status"] == "incompatible":
                    assert ref["center"] is None
                    continue
                assert ref["status"] == "ready"
                center = ref["center"]
                assert center["protein"] * 4 + center["carbs"] * 4 + center["fat"] * 9 == pytest.approx(kcal)
                for key in ("protein", "carbs", "fat"):
                    assert ref["ranges"][key]["lower"] - 1e-6 <= center[key] <= ref["ranges"][key]["upper"] + 1e-6, (mode, weight, kcal)


def sample(kcal=2700):
    data = nutrients.reference_data()
    info = {"target_kcal": kcal, "record_state": "complete", "target_state": "active", "intake_status": "ready",
            "macro": nutrients.macro_reference({"weight_kg": 90, "nutrition_reference": "regular_training"}, kcal, data),
            "recorded": {key: {"lower": value, "upper": value} for key, value in dict(kcal=500, protein=30, carbs=60, fat=15).items()},
            "food_history": {"counts": {"broccoli": 2, "carrot": 2, "chicken": 2}}}
    plans = []
    for meal_type in ("lunch", "dinner"):
        items = [item("rice", 400), item("chicken", 150), item("broccoli", 200), item("carrot", 150), item("rapeseed-oil", 15)]
        budget, note = nutrients.meal_budget(info, meal_type, ["lunch", "dinner"])
        plans.append({"id": meal_type, "items": items, "excluded_foods": sorted(ON_REQUEST_FOODS),
                      "quick_nutrition": nutrients.nutrition_result(items, budget, note, info, data)})
    return plans, info, data


@pytest.mark.parametrize("kcal", [2700, 3300])
def test_daily_centers_portion_sum_and_variety_are_optimized_together(kcal):
    plans, info, data = sample(kcal)
    original = deepcopy(plans)
    result, checked = balance(plans, {"lunch", "dinner"}, info, data)
    assert plans == original
    assert checked["status"] == "within"
    assert quality(result, info, data) < quality(plans, info, data)
    protein = checked["projected"]["protein"]["lower"]
    assert abs(protein - 180) < 12
    assert sum(i["food_id"] in {"broccoli", "carrot"} for p in result for i in p["items"]) < 4
    assert {i["food_id"] for i in result[0]["items"] if i["group"] in {"protein", "vegetable"}} != {i["food_id"] for i in result[1]["items"] if i["group"] in {"protein", "vegetable"}}
    for plan in result:
        assert not {i["food_id"] for i in plan["items"]} & ON_REQUEST_FOODS
        assert sum(i["upper"] / STAPLE_PORTIONS[i["food_id"]] for i in plan["items"] if i["group"] == "starch") <= 2.1
        assert not {"rice", "brown-rice"} <= {i["food_id"] for i in plan["items"]}


def test_valid_but_repetitive_result_is_still_improved():
    plans, info, data = sample()
    first, _ = balance(plans, {"lunch", "dinner"}, info, data)
    counts = {i["food_id"]: 20 for p in first for i in p["items"] if i["group"] in {"protein", "vegetable"}}
    info["food_history"]["counts"] = counts
    assert evaluate(first, info, data)["status"] == "within"
    changed, checked = balance(first, {"lunch", "dinner"}, info, data)
    assert checked["status"] == "within"
    assert quality(changed, info, data) < quality(first, info, data)


def test_variety_cannot_remove_explicit_choice_or_modify_other_meal():
    plans, info, data = sample()
    first, _ = balance(plans, {"lunch", "dinner"}, info, data)
    dinner = first[-1]
    dinner["required_groups"] = {group: sum(i["group"] == group for i in dinner["items"]) for group in {i["group"] for i in dinner["items"]}}
    info["food_history"]["counts"] = {i["food_id"]: 100 for i in dinner["items"]}
    changed, checked = balance(first, {"dinner"}, info, data)
    assert checked["status"] == "within"
    assert changed[0] == first[0]
    assert {i["food_id"] for i in dinner["items"]} <= {i["food_id"] for i in changed[-1]["items"]}


def test_history_only_uses_owned_actual_recent_records(client, application):
    user = register(client)
    today = business_today()
    client.post("/api/meals", json=meal(day=today.isoformat(), name="鸡肉"))
    client.post("/api/meals", json=meal(day=(today-timedelta(days=1)).isoformat(), name="鸡肉"))
    client.post("/api/meals", json=meal(day=(today-timedelta(days=7)).isoformat(), name="西兰花"))
    with TestClient(application) as other:
        register(other, "bob")
        other.post("/api/meals", json=meal(day=today.isoformat(), name="胡萝卜"))
    with application.state.database.connect() as connection:
        result = recent_foods(connection, user["id"], today)
        assert result["counts"]["chicken"] == 1.5
        assert "broccoli" not in result["counts"] and "carrot" not in result["counts"]


def test_prompt_receives_history_and_history_edit_invalidates_result(quick):
    client, _, model, _ = quick
    conversation = ask(client)
    day = (business_today() - timedelta(days=1)).isoformat()
    # Fixture coach date is fixed; use a date relative to that conversation instead.
    from datetime import date
    day = (date.fromisoformat(conversation["day"]) - timedelta(days=1)).isoformat()
    client.post("/api/meals", json=meal(day=day, name="鸡肉"))
    response = recommend(client, conversation)
    assert response.status_code == 200, response.text
    assert model.messages[0]["recent_food_frequency"]["chicken"] == .5
    assert model.messages[0]["daily_macro_reference"]["center"]["protein"] > 0
    current_plans(response.json())
    client.post("/api/meals", json=meal(day=day, name="菠菜"))
    updated = client.get(f"{BASE}/{conversation['id']}").json()
    assert all(plan["stale"] for plan in updated["meal_plans"])
