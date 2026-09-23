"""Small sourced composition subset and transparent, non-clinical portion fitting."""
import hashlib
import json
import math
from datetime import date
from services.business_time import business_today

from config import ROOT
from model.factory import ModelError
from services.intake_targets import target_state
from services.meal_intake import planning_context
from services.nutrition_totals import nutrition_totals
from services.plan_foods import FOODS, QUICK_MAX

DATA_PATH = ROOT / "data/nutrition/reference.json"
NAMES = ("kcal", "protein", "carbs", "fat")
POLICY = "quick-meal-reference-v4-centered"
SHARES = {"breakfast": .25, "lunch": .35, "dinner": .40}


def reference_data():
    try:
        raw = DATA_PATH.read_bytes()
        data = json.loads(raw)
        if data["status"] != "approved" or business_today() >= date.fromisoformat(data["review_due"]):
            raise ValueError("Review required")
        if set(data["foods"]) != set(FOODS):
            raise ValueError("Incomplete catalog")
        for food in data["foods"].values():
            if any(isinstance(food[key], bool) or not math.isfinite(food[key]) or food[key] < 0 for key in NAMES):
                raise ValueError("Unknown nutrient")
        return {**data, "fingerprint": hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, KeyError, TypeError):
        raise ModelError("FOOD_REFERENCE_UNAVAILABLE", "食材成分参考缺失或待复核，暂不能核算新餐单。原记录保留。", 503) from None


def macro_reference(profile, kcal, data):
    adult = data["principles"]["adult"]
    mode = profile.get("nutrition_reference", "general")
    weight = profile.get("weight_kg")
    sources = [adult]
    if not kcal:
        return {"status": "no_target", "ranges": None, "center": None, "sources": sources,
                "note": "请先核对每日能量目标，暂不生成看似个性化的固定份量。"}
    ranges = {key: {"lower": kcal * adult[key + "_energy"][0] / factor,
                    "upper": kcal * adult[key + "_energy"][1] / factor}
              for key, factor in (("protein", 4), ("carbs", 4), ("fat", 9))}
    protein = .20 * kcal / 4
    if mode == "regular_training":
        sport = data["principles"]["regular_training"]
        sources.append(sport)
        if weight is None or not 40 <= weight <= 200:
            return {"status": "weight_required", "ranges": None, "center": None, "sources": sources,
                    "note": "规律运动蛋白参考需要可核对的体重（当前工程范围40–200kg），暂不套用每公斤标准。"}
        low, high = sport["protein_g_kg"]
        ranges["protein"]["lower"] = max(ranges["protein"]["lower"], weight * low)
        ranges["protein"]["upper"] = min(ranges["protein"]["upper"], weight * high)
        protein = weight * 2
    elif weight is not None:
        ranges["protein"]["lower"] = max(ranges["protein"]["lower"], weight * adult["protein_min_g_kg"])
    if ranges["protein"]["lower"] > ranges["protein"]["upper"]:
        return {"status": "incompatible", "ranges": None, "center": None, "sources": sources,
                "note": "体重蛋白参考与当前能量目标不兼容，请先核对目标；不强行凑比例。"}
    protein = min(ranges["protein"]["upper"], max(ranges["protein"]["lower"], protein))
    fat_energy = .275 * kcal
    if mode == "regular_training":
        ranges["carbs"] = {"lower": max(0, (kcal - ranges["protein"]["upper"] * 4 - ranges["fat"]["upper"] * 9) / 4),
                           "upper": (kcal - ranges["protein"]["lower"] * 4 - ranges["fat"]["lower"] * 9) / 4}
    else:
        fat_energy = min(fat_energy, kcal - protein * 4 - ranges["carbs"]["lower"] * 4)
    center = {"kcal": kcal, "protein": protein, "fat": fat_energy / 9,
              "carbs": (kcal - protein * 4 - fat_energy) / 4}
    return {"status": "ready", "ranges": ranges, "center": center, "sources": sources,
            "preferred": {"protein_g_kg": 2 if mode == "regular_training" else None, "fat_energy": [.25, .30]},
            "note": "规律训练蛋白配量优先靠近2g/kg，参考1.6–2.2g/kg；脂肪优先25%–30%能量，碳水承接余量。默认配量偏好不代表个人精确需要。" if mode == "regular_training"
                    else "一般健康成人宏量参考；未默认套用运动人群的每公斤蛋白标准。"}


def context(connection, user_id, day, profile, meals):
    data = reference_data()
    target = target_state(connection, user_id, date.fromisoformat(day))
    intake = planning_context(connection, user_id, date.fromisoformat(day))
    kcal = target["target"]["kcal"] if target["status"] == "active" else None
    macro = macro_reference(profile, kcal, data)
    known, estimated = nutrition_totals(meals)
    recorded = {}
    for key in NAMES:
        if estimated[key]["unknown_count"] or (not meals and intake["record_state"] != "none_yet"):
            recorded[key] = None
        else:
            recorded[key] = {side: round((known[key]["known_total"] or 0) + (estimated[key][side + "_total"] or 0), 2)
                             for side in ("lower", "upper")}
    from services.meal_variety import recent_foods
    return {"policy": POLICY, "data_version": data["version"], "data_hash": data["fingerprint"],
            "energy_feedback": target.get("energy_feedback"), "base_target_kcal": (target["target"] or {}).get("base_kcal", kcal),
            "food_history": recent_foods(connection, user_id, day),
            "target_state": target["status"], "target_kcal": kcal, "macro": macro,
            "target_scope": (target["target"] or {}).get("scope"), "goal": profile["goal"],
            "nutrition_reference": profile.get("nutrition_reference", "general"),
            "recorded": recorded, "record_count": len(meals), "intake_status": intake["status"],
            "unknown_kcal_foods": [{"name": meal["name"], "meal_type": meal["meal_type"]} for meal in meals
                                   if (meal.get("kcal_per_100g") is None or meal.get("grams") is None)
                                   and not meal.get("nutrition_estimate")],
            "record_state": intake["record_state"], "intake_hash": intake["context_hash"]}


def readiness(info):
    if info["target_state"] != "active":
        return {"ready": False, "reason": "target_required", "message": "每日目标未就绪，请在个人档案设置长期营养标准，或在今日总览核对本日目标。"}
    if info["macro"]["center"] is None:
        return {"ready": False, "reason": "macro_required", "message": info["macro"]["note"]}
    kcal = info["recorded"]["kcal"]
    if kcal is None:
        empty = not info["record_count"]
        unknown = info.get("unknown_kcal_foods", [])
        names = "、".join(item["name"] for item in unknown[:5]) + ("等" if len(unknown) > 5 else "")
        detail = f"（{names}）" if names else ""
        return {"ready": False, "reason": "confirm_empty" if empty else "unknown_intake",
                "message": "今天还没有饮食记录，请确认尚未进食，或先补录已吃内容。" if empty else f"已吃记录中有热量未知项{detail}，还不能计算剩余餐单。请到饮食记录中核对这些食物的份量并重新估算；未知热量不会按0扣除。"}
    if kcal["upper"] >= info["target_kcal"]:
        return {"ready": False, "reason": "target_reached", "message": "已记录摄入达到或估算区间跨过本日目标，暂不分配额外热量。可核对记录和目标；不建议为了数字强行进食或禁食。"}
    from services.meal_balance import precheck
    try:
        precheck(info)
    except ModelError as error:
        return {"ready": False, "reason": "macro_balance", "code": error.code, "message": str(error)}
    return {"ready": True, "reason": "ready", "message": "依据已录入摄入计算；尚未记录的饮食不在其中。"}


def require_ready(info):
    state = readiness(info)
    if not state["ready"]:
        raise ModelError(state.get("code", "MEAL_CALCULATION_REQUIRED"), state["message"], 409)


def current_context(connection, user_id, day):
    from services.profile_context import read_profile
    profile = read_profile(connection, user_id)
    meals = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM meals WHERE user_id=? AND day=? ORDER BY id", (user_id, str(day)))]
    info = context(connection, user_id, str(day), profile, meals)
    return {**info, **readiness(info)}


def meal_budget(info, meal_type, schedule):
    require_ready(info)
    center = info["macro"]["center"]
    fraction = SHARES[meal_type] / sum(SHARES[key] for key in schedule)
    values = {key: max(0, center[key] - sum(info["recorded"][key].values()) / 2) * fraction
              for key in NAMES if info["recorded"][key] is not None}
    note = "按已确认完整的摄入估算余量。" if info["intake_status"] == "ready" else "仅按已录入摄入暂算余量，记录未确认完整，可能还有遗漏。"
    unknown = "未知宏量不当0，不参与对应项余量拟合。" if len(values) < len(NAMES) else ""
    return values, note + "假设所选餐覆盖后续进食，未另留加餐；餐次相对权重只是配量起点。" + unknown


def amount_nutrients(food_id, amount, data):
    food = data["foods"][food_id]
    scale = amount * food.get("grams_per_unit", 1) / 100
    return {key: food[key] * scale for key in NAMES}


def fit_portions(items, budget, kept, limits, data):
    """Discrete coordinate search; no fabricated precision or nutrition-model calls."""
    fixed = {item["food_id"] for item in kept}
    caps = {item["food_id"]: item["max_upper"] for item in limits}
    original = [(item["lower"] + item["upper"]) / 2 for item in items]
    options = []
    for index, item in enumerate(items):
        key = item["food_id"]
        low, high = FOODS[key][4], QUICK_MAX.get(key, FOODS[key][5])
        high = min(high, caps.get(key, high))
        step = 1 if item["unit"] == "个" or item["group"] == "fat" else 10
        opts = [value for value in range(low, high + 1, step)]
        options.append([original[index]] if key in fixed else opts)
    quantities = [min(opts, key=lambda value: abs(value - original[i])) for i, opts in enumerate(options)]

    def score(values):
        totals = {key: 0 for key in NAMES}
        for item, amount in zip(items, values):
            for key, value in amount_nutrients(item["food_id"], amount, data).items():
                totals[key] += value
        fit = sum((3 if key == "kcal" else 1) * ((totals[key] - budget[key]) / max(budget[key], 10)) ** 2 for key in budget)
        # A weak portion prior avoids changing vegetables simply to shave a calorie.
        prior = sum(.015 * ((value - old) / max(old, 10)) ** 2 for value, old in zip(values, original))
        return fit + prior

    if budget:
        for _ in range(12):
            before = list(quantities)
            for i, opts in enumerate(options):
                quantities[i] = min(opts, key=lambda value: score(quantities[:i] + [value] + quantities[i + 1:]))
            if before == quantities:
                break
    result = []
    for item, quantity in zip(items, quantities):
        result.append(dict(item) if item["food_id"] in fixed else {**item, "lower": int(quantity), "upper": int(quantity)})
    # Existing meal validation caps total vegetables at 400g.
    vegetables = [item for item in result if item["group"] == "vegetable"]
    excess = sum(item["upper"] for item in vegetables) - 400
    for item in reversed(vegetables):
        if excess > 0 and item["food_id"] not in fixed:
            reduction = min(excess, item["upper"] - FOODS[item["food_id"]][4])
            item["lower"] -= reduction
            item["upper"] -= reduction
            excess -= reduction
    return result


def nutrition_result(items, budget, note, info, data):
    totals = {key: {"lower": 0, "upper": 0} for key in NAMES}
    rows = []
    for item in items:
        values = {side: amount_nutrients(item["food_id"], item[side], data) for side in ("lower", "upper")}
        nutrients = {key: {side: round(values[side][key], 2) for side in values} for key in NAMES}
        for key in NAMES:
            for side in values:
                totals[key][side] += nutrients[key][side]
        food = data["foods"][item["food_id"]]
        rows.append({"food_id": item["food_id"], "nutrients": nutrients, "code": food["code"], "assumption": food["note"]})
    for value in totals.values():
        for side in value:
            value[side] = round(value[side], 2)
    return {"method": "sourced_composition_estimate", "totals": totals, "rows": rows,
            "portion_reference": {key: round(value, 1) for key, value in budget.items()} if budget else None,
            "allocation_note": note, "context": info, "source": data["source"], "notice": data["notice"],
            "data_version": data["version"], "estimated": True, "independently_verified": False}
