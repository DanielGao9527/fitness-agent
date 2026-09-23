import json
from datetime import date
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.coach as coach_api
from app import create_app
from model.factory import ModelError
from services import meal_nutrient_reference as nutrients
from services.meal_plans import REQUIRED
from services.plan_foods import FOODS
from test_foundation import DAY, PASSWORD, application, client, meal, register
from test_knowledge import library
from test_meal_plans import plans
from test_coach import conversation, send, BASE
from test_intake_targets import save
from test_meal_intake import review


class ChoiceModel:
    def __init__(self):
        self.messages = []
        self.fail_at = None
        self.callback = None

    def generate(self, *, system_prompt, message):
        data = json.loads(message)
        self.messages.append(data)
        if self.callback:
            self.callback()
        if len(self.messages) == self.fail_at:
            raise ModelError("MODEL_TIMEOUT", "Synthetic timeout", 504)
        assert "nutrition_per_100g" in data and "快速餐单" in system_prompt
        allowed = {food["id"]: food for food in data["allowed_foods"]}
        items = list(data.get("keep_items", []))
        for limit in data.get("portion_limits", []):
            items.append({"food_id": limit["food_id"], "lower": allowed[limit["food_id"]]["min"], "upper": limit["max_upper"]})
        groups = data.get("required_groups", {"starch": 1, "protein": 1, "vegetable": 1, "fat": 1})
        for group in data.get("requested_groups", []):
            groups.setdefault(group, 1)
        for group, count in groups.items():
            for _ in range(count - sum(FOODS[item["food_id"]][1] == group for item in items)):
                food = next((food for key, food in allowed.items() if food["group"] == group and key not in {item["food_id"] for item in items}), None)
                if food:
                    value = max(food["min"], min(food["max"], 150))
                    items.append({"food_id": food["id"], "lower": value, "upper": value})
        return json.dumps({"items": items, "chunk_ids": list(REQUIRED)})


@pytest.fixture
def quick(plans, monkeypatch):
    client, app, _, knowledge = plans
    model = ChoiceModel()
    monkeypatch.setattr(coach_api, "create_meal_plan_model", lambda settings: model)
    confirm(client)
    assert save(client).status_code == 200
    assert review(client).status_code == 200
    return client, app, model, knowledge


def confirm(client):
    consent = client.get("/api/meal-plans/consent").json()
    response = client.put("/api/meal-plans/consent", json={"context_hash": consent["context_hash"], "adult_general_diet": True, "constraints_reviewed": True})
    assert response.status_code == 200, response.text


def ask(client, data=None, message="安排午餐和晚餐"):
    response = send(client, data or conversation(client), message)
    assert response.status_code == 200, response.text
    return response.json()


def recommend(client, data, **changes):
    return client.post(f"{BASE}/{data['id']}/recommend-meals", json={"version": data["version"], "client_id": str(uuid4()), **changes})


def current_plans(data):
    context = data["meal_context"]
    schedule = context.get("meal_types") or [context.get("meal_type") or "dinner"]
    return [next(plan for plan in data["meal_plans"] if plan["meal_type"] == key and plan["coach_version"] == context.get("meal_versions", {}).get(key, data["version"]) and not plan["stale"]) for key in schedule]


def test_curated_data_coverage_units_and_unknown_handling(tmp_path, monkeypatch):
    data = nutrients.reference_data()
    assert set(data["foods"]) == set(FOODS) and len(FOODS) == 75
    assert nutrients.amount_nutrients("egg", 2, data)["kcal"] == 143
    assert nutrients.amount_nutrients("rice", 100, data)["kcal"] == 131
    assert "50g" in data["foods"]["egg"]["note"]
    path = tmp_path / "broken.json"
    data["foods"]["rice"]["kcal"] = "N"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(nutrients, "DATA_PATH", path)
    with pytest.raises(ModelError, match="成分参考"):
        nutrients.reference_data()


@pytest.mark.parametrize("weight,kcal", [(50, 1600), (70, 2200), (100, 3000)])
def test_macro_reference_coherent_and_weight_sensitive(weight, kcal):
    data = nutrients.reference_data()
    value = nutrients.macro_reference({"weight_kg": weight, "nutrition_reference": "regular_training"}, kcal, data)
    assert value["status"] == "ready"
    center = value["center"]
    assert center["protein"] == pytest.approx(weight * 2)
    assert center["fat"] * 9 == pytest.approx(kcal * .275)
    assert center["protein"] * 4 + center["carbs"] * 4 + center["fat"] * 9 == pytest.approx(kcal)
    for key in ("protein", "fat", "carbs"):
        assert value["ranges"][key]["lower"] <= center[key] <= value["ranges"][key]["upper"]


def test_macro_no_silent_weight_or_impossible_target():
    data = nutrients.reference_data()
    assert nutrients.macro_reference({}, None, data)["status"] == "no_target"
    assert nutrients.macro_reference({"nutrition_reference": "regular_training"}, 2000, data)["status"] == "weight_required"
    assert nutrients.macro_reference({"nutrition_reference": "regular_training", "weight_kg": 200}, 1000, data)["status"] == "incompatible"
    assert nutrients.macro_reference({}, 2000, data)["center"]["protein"] == 100


def test_one_request_multi_meal_nutrition_cached_and_no_actual_writes(quick):
    client, app, model, _ = quick
    data = ask(client)
    response = recommend(client, data)
    assert response.status_code == 200, response.text
    result = response.json()
    plans = current_plans(result)
    assert [p["meal_type"] for p in plans] == ["lunch", "dinner"]
    assert all(p["quick_nutrition"]["context"]["target_kcal"] == 2100 for p in plans)
    assert all(p["quick_nutrition"]["context"]["recorded"]["kcal"] == {"lower": 0, "upper": 0} for p in plans)
    assert not model.messages[0]["other_suggested_meals"]
    assert model.messages[1]["other_suggested_meals"][0]["meal_type"] == "lunch"
    assert recommend(client, data).status_code == 200 and len(model.messages) == 2
    for plan in plans:
        n = plan["quick_nutrition"]
        for key in nutrients.NAMES:
            assert n["totals"][key]["lower"] == pytest.approx(sum(row["nutrients"][key]["lower"] for row in n["rows"]), abs=.01)
    with app.state.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 1
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert len(current_plans(restarted.get(f"{BASE}/{data['id']}").json())) == 2


def test_daily_remaining_not_repeated_and_macro_totals(quick):
    client, _, model, _ = quick
    client.put("/api/profile", json={"weight_kg": 70, "nutrition_reference": "regular_training"})
    confirm(client)
    save(client, kcal=2200)
    client.post("/api/meals", json=meal(meal_type="breakfast", grams=100, kcal_per_100g=400, protein_per_100g=25, carbs_per_100g=50, fat_per_100g=10, source="Synthetic"))
    data = ask(client, message="今天已吃的都记录好了，安排午餐和晚餐")
    response = recommend(client, data)
    assert response.status_code == 200, response.text
    plans = current_plans(response.json())
    assert sum(p["quick_nutrition"]["portion_reference"]["kcal"] for p in plans) == pytest.approx(1800, abs=.1)
    assert sum(p["quick_nutrition"]["portion_reference"]["protein"] for p in plans) == pytest.approx(115, abs=.1)
    assert model.messages[0]["portion_reference"]["kcal"] != model.messages[1]["portion_reference"]["kcal"]
    assert any(item["lower"] != 150 for plan in plans for item in plan["items"] if item["unit"] == "g")
    assert all(p["quick_nutrition"]["context"]["record_state"] == "complete" for p in plans)


def test_no_records_not_zero_and_no_invented_complete(quick):
    client, _, _, _ = quick
    save(client)
    data = ask(client)
    assert recommend(client, data).status_code == 409
    assert data["meal_calculation"]["reason"] == "confirm_empty"
    data = ask(client, data, "今天还没吃东西，安排午餐和晚餐")
    data = recommend(client, data).json()
    assert current_plans(data)[0]["quick_nutrition"]["context"]["recorded"]["kcal"] == {"lower": 0, "upper": 0}


def test_partial_failure_retains_and_retry_only_missing(quick):
    client, _, model, _ = quick
    model.fail_at = 2
    data = ask(client)
    key = str(uuid4())
    assert recommend(client, data, client_id=key).status_code == 504
    assert recommend(client, data, client_id=key).status_code == 409
    assert len(model.messages) == 2
    fresh = recommend(client, data)
    assert fresh.status_code == 200, fresh.text
    assert len(current_plans(fresh.json())) == 2 and len(model.messages) == 3


def test_followup_only_changes_requested_part_and_other_meal_kept(quick):
    client, _, model, _ = quick
    original = recommend(client, ask(client)).json()
    lunch, dinner = current_plans(original)
    protein = next(x for x in dinner["items"] if x["group"] == "protein")
    replacement = "beef" if protein["food_id"] != "beef" else "chicken"
    changed = ask(client, original, f"晚餐{protein['name']}换成{FOODS[replacement][0]}")
    result = recommend(client, changed)
    assert result.status_code == 200, result.text
    new_lunch, new_dinner = current_plans(result.json())
    assert new_lunch["id"] == lunch["id"] and new_lunch["items"] == lunch["items"]
    assert next(x for x in new_dinner["items"] if x["group"] == "protein")["food_id"] == replacement
    kept = [x for x in dinner["items"] if x["group"] != "protein"]
    remaining = [x for x in new_dinner["items"] if x["group"] != "protein"]
    assert {x["food_id"] for x in kept} <= {x["food_id"] for x in remaining}
    if kept != remaining:
        assert "保留食材可能重新配量" in new_dinner["quick_nutrition"]["allocation_note"]
        assert result.json()["turns"][-1]["response"]["quick_check"]["status"] == "within"
    assert len(model.messages) == 3


@pytest.mark.parametrize("condition", ["profile", "record", "target", "turn"])
def test_late_result_cannot_overwrite_changed_context(quick, condition):
    client, _, model, _ = quick
    data = ask(client, message="安排晚餐")
    def change():
        if condition == "profile":
            client.put("/api/profile", json={"food_allergies": "鸡肉过敏"})
        elif condition == "record":
            client.post("/api/meals", json=meal())
        elif condition == "target":
            save(client)
        else:
            ask(client, data, "安排午餐")
    model.callback = change
    assert recommend(client, data).status_code == 409
    view = client.get(f"{BASE}/{data['id']}").json()
    assert not view["meal_plans"]


def test_allergy_unknown_scope_and_ownership_before_call(quick):
    client, app, model, _ = quick
    client.put("/api/profile", json={"food_allergies": "鸡肉过敏；橄榄油过敏"})
    confirm(client)
    save(client)
    review(client)
    data = recommend(client, ask(client)).json()
    assert all(item["food_id"] not in {"chicken", "olive-oil"} for plan in current_plans(data) for item in plan["items"])
    count = len(model.messages)
    with TestClient(app) as other:
        register(other, "bob")
        confirm(other)
        assert recommend(other, data).status_code == 404
        assert len(model.messages) == count
    client.put("/api/profile", json={"food_allergies": "一些食物过敏不清楚"})
    confirm(client)
    assert recommend(client, ask(client)).status_code == 409
    assert len(model.messages) == count


@pytest.mark.parametrize("change", [{"version": True}, {"version": 0}, {"user_id": 1}, {"client_id": "bad"}, {"target_kcal": 2000}])
def test_strict_inputs(quick, change):
    client, _, model, _ = quick
    assert recommend(client, ask(client), **change).status_code == 422
    assert not model.messages


def test_withdrawn_data_stale_not_deleted(quick, tmp_path, monkeypatch):
    client, _, model, _ = quick
    data = recommend(client, ask(client)).json()
    path = tmp_path / "data.json"
    source = nutrients.reference_data()
    source["status"] = "withdrawn"
    path.write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setattr(nutrients, "DATA_PATH", path)
    loaded = client.get(f"{BASE}/{data['id']}").json()
    assert all(plan["stale"] for plan in loaded["meal_plans"])
    count = len(model.messages)
    assert recommend(client, loaded).status_code == 503
    assert len(model.messages) == count


def test_concurrent_new_id_does_not_double_call(quick):
    client, _, model, _ = quick
    data = ask(client, message="安排晚餐")
    nested = []
    def concurrent():
        nested.append(recommend(client, data).status_code)
    model.callback = concurrent
    assert recommend(client, data).status_code == 200
    assert nested == [409] and len(model.messages) == 1


def test_unknown_macros_prevent_unverifiable_recommendation(quick):
    client, _, model, _ = quick
    save(client, kcal=2000)
    client.post("/api/meals", json=meal(grams=100, kcal_per_100g=500, source="Synthetic"))
    assert review(client).status_code == 200
    response = recommend(client, ask(client))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEAL_MACROS_UNKNOWN"
    assert not model.messages
    again = ask(client)
    assert again["meal_calculation"]["ready"] is False
    assert again["meal_calculation"]["reason"] == "macro_balance"


def test_anonymous_cross_origin_and_no_key(quick, monkeypatch):
    client, app, model, _ = quick
    data = ask(client)
    url = f"{BASE}/{data['id']}/recommend-meals"
    body = {"version": data["version"], "client_id": str(uuid4())}
    assert client.post(url, json=body, headers={"origin": "https://evil.example"}).status_code == 403
    with TestClient(app) as anonymous:
        assert anonymous.post(url, json=body).status_code == 401
    from model.factory import create_meal_plan_model
    monkeypatch.setattr(coach_api, "create_meal_plan_model", create_meal_plan_model)
    app.state.settings = replace(app.state.settings, qwen_api_key="")
    assert recommend(client, data).status_code == 503
    assert not model.messages


def test_fitting_typical_day_reduces_error_and_not_all_150(quick):
    client, _, _, _ = quick
    client.put("/api/profile", json={"weight_kg": 70, "nutrition_reference": "regular_training"})
    confirm(client)
    save(client, kcal=2200)
    data = ask(client, message="今天还没吃东西，安排早餐和午餐和晚餐")
    result = recommend(client, data)
    assert result.status_code == 200, result.text
    plans = current_plans(result.json())
    total = {key: sum(plan["quick_nutrition"]["totals"][key]["lower"] for plan in plans) for key in nutrients.NAMES}
    assert abs(total["kcal"] - 2200) < 220
    assert 98 <= total["protein"] <= 140
    assert 48.8 <= total["fat"] <= 85.6
    assert any(item["upper"] > FOODS[item["food_id"]][5] for plan in plans for item in plan["items"])


def test_reconfirm_profile_regenerates_stale_cache(quick):
    client, _, model, _ = quick
    data = recommend(client, ask(client)).json()
    client.put("/api/profile", json={"food_allergies": "鸡肉过敏"})
    assert recommend(client, data).status_code == 409
    confirm(client)
    save(client)
    review(client)
    response = recommend(client, data)
    assert response.status_code == 200, response.text
    assert len(model.messages) == 4
    assert all(item["food_id"] != "chicken" for plan in current_plans(response.json()) for item in plan["items"])


def test_no_target_never_calls_model_or_gives_generic_plan(quick):
    client, _, model, _ = quick
    save(client, kcal=None, source="")
    data = ask(client)
    assert data["meal_calculation"]["reason"] == "target_required"
    assert recommend(client, data).status_code == 409
    assert not model.messages and not data["meal_plans"]


def test_breakfast_edit_recomputes_remaining_without_complete_confirmation(quick):
    client, _, model, _ = quick
    food = client.post('/api/meals', json=meal(grams=100, kcal_per_100g=400, protein_per_100g=20, carbs_per_100g=50, fat_per_100g=10, source='Synthetic')).json()
    assert review(client).status_code == 200
    data = recommend(client, ask(client)).json()
    before = current_plans(data)
    assert sum(p['quick_nutrition']['portion_reference']['kcal'] for p in before) == pytest.approx(1700, abs=.1)
    body = {key: value for key, value in food.items() if key not in ('id', 'client_id')}
    body.update(grams=225)
    assert client.put(f'/api/meals/{food["id"]}', json=body).status_code == 200
    refreshed = client.get(f'{BASE}/{data["id"]}').json()
    assert all(plan['stale'] for plan in refreshed['meal_plans'])
    assert refreshed['meal_calculation']['record_state'] is None
    result = recommend(client, refreshed)
    assert result.status_code == 200, result.text
    after = current_plans(result.json())
    assert sum(p['quick_nutrition']['portion_reference']['kcal'] for p in after) == pytest.approx(1200, abs=.1)
    assert sum(p['quick_nutrition']['totals']['kcal']['lower'] for p in after) < sum(p['quick_nutrition']['totals']['kcal']['lower'] for p in before)
    assert before[-1]['items'] != after[-1]['items']
    assert '未确认完整' in after[-1]['quick_nutrition']['allocation_note']
    assert len(model.messages) == 4


def test_new_requested_scope_does_not_keep_old_breakfast(quick):
    client, _, _, _ = quick
    data = recommend(client, ask(client, message='安排早餐和午餐和晚餐')).json()
    data = ask(client, data, '安排午餐和晚餐')
    assert data['meal_context']['meal_types'] == ['lunch', 'dinner']
    plans = current_plans(recommend(client, data).json())
    assert sum(p['quick_nutrition']['portion_reference']['kcal'] for p in plans) == pytest.approx(2100, abs=.1)


def test_changed_budget_refits_unchanged_foods_during_swap(quick):
    client, _, _, _ = quick
    data = recommend(client, ask(client)).json()
    old = current_plans(data)[-1]
    food = client.post('/api/meals', json=meal(grams=200, kcal_per_100g=400, protein_per_100g=25, carbs_per_100g=50, fat_per_100g=10, source='synthetic')).json()
    assert food['id']
    protein = next(x for x in old["items"] if x["group"] == "protein")
    replacement = "beef" if protein["food_id"] != "beef" else "chicken"
    data = ask(client, data, f"晚餐{protein['name']}换成{FOODS[replacement][0]}")
    result = recommend(client, data)
    assert result.status_code == 200, result.text
    new = current_plans(result.json())[-1]
    assert new['quick_nutrition']['portion_reference']['kcal'] == pytest.approx(1300 * .4 / .75, abs=.1)
    assert new['quick_nutrition']['totals']['kcal']['upper'] < old['quick_nutrition']['totals']['kcal']['upper']
    assert {x['food_id'] for x in new['items']} == {(replacement if x['food_id']==protein['food_id'] else x['food_id']) for x in old['items']}


def test_unknown_heat_or_reached_target_blocks_before_remote(quick):
    client, _, model, _ = quick
    food = client.post('/api/meals', json=meal()).json()
    data = ask(client)
    assert data['meal_calculation']['reason'] == 'unknown_intake'
    assert recommend(client, data).status_code == 409
    body = {key: value for key, value in food.items() if key not in ('id','client_id')}
    body.update(grams=1000, kcal_per_100g=400, source='synthetic')
    assert client.put(f'/api/meals/{food["id"]}', json=body).status_code == 200
    data = client.get(f'{BASE}/{data["id"]}').json()
    assert data['meal_calculation']['reason'] == 'target_reached'
    assert recommend(client, data).status_code == 409
    assert not model.messages
