from copy import deepcopy
from types import SimpleNamespace

import pytest

from model.factory import ModelError
from services import meal_balance as balance
from services import meal_nutrient_reference as nutrients
from services.plan_foods import FOODS
from test_quick_meals import quick, ask, recommend, current_plans, confirm
from test_quick_meals import plans, application, client, library
from test_intake_targets import save
from test_meal_intake import review
from test_foundation import meal
from test_coach import BASE


def item(key, amount):
    name, group, unit, basis, _, _ = FOODS[key]
    return {"food_id": key, "name": name, "group": group, "unit": unit, "basis": basis, "lower": amount, "upper": amount}


def sample(kcal=3300):
    data = nutrients.reference_data()
    info = {"target_state": "active", "target_kcal": kcal, "record_state": "complete", "intake_status": "ready",
            "macro": nutrients.macro_reference({"weight_kg": 90, "nutrition_reference": "regular_training"}, kcal, data),
            "recorded": {"kcal": {"lower": 1450, "upper": 1650}, "protein": {"lower": 45, "upper": 70},
                         "carbs": {"lower": 175, "upper": 213}, "fat": {"lower": 60, "upper": 80}}}
    result = []
    for key in ("lunch", "dinner"):
        budget, note = nutrients.meal_budget(info, key, ["lunch", "dinner"])
        items = [item("rice", 180), item("chicken", 120), item("spinach", 120), item("olive-oil", 10)]
        result.append({"id": key, "items": items, "excluded_foods": [], "quick_nutrition": nutrients.nutrition_result(items, budget, note, info, data)})
    return result, info, data


def assert_within(plans, info, data):
    check = balance.evaluate(plans, info, data)
    assert check["status"] == "within", check
    for key in balance.MACROS:
        assert check["projected"][key]["lower"] >= info["macro"]["ranges"][key]["lower"]
        assert check["projected"][key]["upper"] <= info["macro"]["ranges"][key]["upper"]


def test_high_carbs_deficit_is_corrected_not_just_reported():
    plans, info, data = sample()
    original = deepcopy(plans)
    assert balance.evaluate(plans, info, data)["projected"]["carbs"]["upper"] < info["macro"]["ranges"]["carbs"]["lower"]
    result, check = balance.balance(plans, {"lunch", "dinner"}, info, data)
    assert_within(result, info, data)
    assert check["policy"] == balance.POLICY
    assert plans == original


@pytest.mark.parametrize("key", balance.MACROS)
@pytest.mark.parametrize("kind,code", [("unknown", "MEAL_MACROS_UNKNOWN"), ("wide", "MEAL_MACROS_UNCERTAIN"), ("over", "MEAL_MACROS_EXCEEDED")])
def test_unknown_wide_and_over_limit_cannot_be_rounded_away(key, kind, code):
    plans, info, data = sample()
    limit = info["macro"]["ranges"][key]
    info["recorded"][key] = None if kind == "unknown" else {"lower": 0, "upper": limit["upper"] + .001 if kind == "over" else limit["upper"] - limit["lower"] + .001}
    with pytest.raises(ModelError) as caught:
        balance.balance(plans, {"lunch", "dinner"}, info, data)
    assert caught.value.code == code


def test_uncertainty_is_preserved_and_overlap_is_not_success():
    plans, info, data = sample()
    result, _ = balance.balance(plans, {"lunch", "dinner"}, info, data)
    check = balance.evaluate(result, info, data)
    for key in balance.MACROS:
        assert check["projected"][key]["upper"] - check["projected"][key]["lower"] == pytest.approx(info["recorded"][key]["upper"] - info["recorded"][key]["lower"])
    info["macro"]["ranges"]["carbs"]["lower"] = check["projected"]["carbs"]["lower"] + .01
    assert balance.evaluate(result, info, data)["status"] == "outside"


def test_fallback_respects_allergy_and_user_selected_foods():
    plans, info, data = sample()
    for plan in plans:
        plan["items"][0] = item("oats", 80)
        plan["excluded_foods"] = [key for key, food in FOODS.items() if food[1] == "protein" and key != "chicken"] + ["rice", "brown-rice", "milk"]
    result, _ = balance.balance(plans, {"lunch", "dinner"}, info, data)
    assert_within(result, info, data)
    assert any(any(it["food_id"] not in {"oats", "chicken", "spinach", "olive-oil"} for it in plan["items"]) for plan in result)
    for plan in result:
        assert not {it["food_id"] for it in plan["items"]} & set(plan["excluded_foods"])
        assert next(it for it in plan["items"] if it["group"] == "protein")["food_id"] == "chicken"


def test_all_fixed_infeasible_is_not_silently_modified():
    plans, info, data = sample()
    with pytest.raises(ModelError) as caught:
        balance.balance(plans, set(), info, data)
    assert caught.value.code == "MEAL_BALANCE_INFEASIBLE"


def test_only_requested_meal_can_change_and_explicit_reduction_is_respected():
    plans, info, data = sample()
    fitted, _ = balance.balance(plans, {"lunch", "dinner"}, info, data)
    lunch = deepcopy(fitted[0])
    dinner = fitted[1]
    rice = next(it for it in dinner["items"] if it["food_id"] == "rice")
    dinner["portion_limits"] = [{"food_id": "rice", "max_lower": rice["lower"], "max_upper": rice["upper"] - 10}]
    # Disqualify the unchanged quantities so a genuine reduced solution is needed.
    dinner["items"] = [dict(it, lower=150, upper=150) if it["food_id"] == "rice" else it for it in dinner["items"]]
    result, _ = balance.balance(fitted, {"dinner"}, info, data)
    assert result[0] == lunch
    assert next(it for it in result[1]["items"] if it["food_id"] == "rice")["upper"] <= dinner["portion_limits"][0]["max_upper"]
    assert_within(result, info, data)


def test_solver_timeout_is_not_reported_as_proven_infeasible(monkeypatch):
    plans, info, data = sample()
    monkeypatch.setattr(balance, "milp", lambda *a, **kw: SimpleNamespace(x=None, status=1))
    with pytest.raises(ModelError) as caught:
        balance.balance(plans, {"lunch", "dinner"}, info, data)
    assert caught.value.code == "MEAL_BALANCE_TIMEOUT"


def test_solver_serialization_and_single_native_thread(monkeypatch):
    plans, info, data = sample()
    calls = []
    original = balance.milp
    def checked(*args, **kwargs):
        assert balance._SOLVER_LOCK.locked()
        assert kwargs["options"]["threads"] == 1
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(balance, "milp", checked)
    balance.balance(plans, {"lunch", "dinner"}, info, data)
    assert calls and not balance._SOLVER_LOCK.locked()


def test_concurrent_synthetic_solutions_remain_separate():
    from concurrent.futures import ThreadPoolExecutor
    def run(index):
        plans, info, data = sample(3300 + index * 5)
        output, _ = balance.balance(plans, {"lunch", "dinner"}, info, data)
        assert_within(output, info, data)
        return info["target_kcal"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(run, range(12))) == [3300 + i * 5 for i in range(12)]


def test_even_feasible_solver_output_is_recomputed(monkeypatch):
    import numpy as np
    plans, info, data = sample()
    monkeypatch.setattr(balance, "milp", lambda c, **kw: SimpleNamespace(x=np.zeros(len(c)), status=0))
    with pytest.raises(ModelError) as caught:
        balance.balance(plans, {"lunch", "dinner"}, info, data)
    assert caught.value.code == "MEAL_BALANCE_INFEASIBLE"


def test_joint_api_high_target_and_exact_totals(quick):
    client, app, _, _ = quick
    client.put("/api/profile", json={"weight_kg": 90, "nutrition_reference": "regular_training"})
    confirm(client)
    save(client, kcal=3300)
    recorded = client.post("/api/meals", json=meal(grams=500, kcal_per_100g=310, protein_per_100g=12, carbs_per_100g=40, fat_per_100g=14, source="Synthetic"))
    assert recorded.status_code == 201, recorded.text
    review(client)
    response = recommend(client, ask(client))
    assert response.status_code == 200, response.text
    plans = current_plans(response.json())
    assert_within(plans, plans[0]["quick_nutrition"]["context"], nutrients.reference_data())
    assert response.json()["turns"][-1]["response"]["quick_check"]["status"] == "within"
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 1


def test_partial_model_failure_does_not_publish_unchecked_lunch(quick):
    client, app, model, _ = quick
    model.fail_at = 2
    data = ask(client)
    assert recommend(client, data).status_code == 504
    assert not client.get(f"{BASE}/{data['id']}").json()["meal_plans"]
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM meal_plans WHERE status='draft'").fetchone()[0] == 0
    response = recommend(client, data)
    assert response.status_code == 200, response.text
    assert len(model.messages) == 3
    assert len(current_plans(response.json())) == 2


def test_failed_joint_check_keeps_candidates_private_and_does_not_recall_model(quick, monkeypatch):
    client, _, model, _ = quick
    data = ask(client)
    def fail(*args):
        raise ModelError("MEAL_BALANCE_INFEASIBLE", "Synthetic infeasible", 409)
    monkeypatch.setattr(balance, "balance", fail)
    assert recommend(client, data).status_code == 409
    assert recommend(client, data).status_code == 409
    assert len(model.messages) == 2
    assert not client.get(f"{BASE}/{data['id']}").json()["meal_plans"]


def test_context_change_during_joint_solve_publishes_nothing(quick, monkeypatch):
    client, _, _, _ = quick
    original = balance.balance
    def changed(*args):
        result = original(*args)
        save(client, kcal=2500)
        return result
    monkeypatch.setattr(balance, "balance", changed)
    data = ask(client)
    response = recommend(client, data)
    assert response.status_code == 409
    assert not client.get(f"{BASE}/{data['id']}").json()["meal_plans"]


def test_direct_quick_endpoint_cannot_skip_joint_check(quick):
    from uuid import uuid4
    client, _, model, _ = quick
    data = ask(client)
    response = client.post("/api/meal-plans", json={"client_id": str(uuid4()), "day": data["day"], "meal_type": "lunch", "coach_id": data["id"], "coach_version": data["version"], "quick_recommendation": True})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "JOINT_MEAL_REQUIRED"
    assert not model.messages
