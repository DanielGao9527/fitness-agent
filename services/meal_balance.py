"""Joint, bounded meal fitting. A feasible solver result is independently checked."""
from collections import Counter
from copy import deepcopy
import math
from threading import Lock
import warnings

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from model.factory import ModelError
from services.meal_nutrient_reference import NAMES, amount_nutrients, nutrition_result
from services.plan_foods import FOODS, QUICK_MAX
from services.meal_variety import STAPLE_PORTIONS, RICE_FAMILY, quality

POLICY = "joint-macros-2026-09-23.3-centered"
MACROS = ("protein", "carbs", "fat")
LABELS = {"protein": "蛋白质", "carbs": "碳水", "fat": "脂肪"}
ENERGY_TOLERANCE = .05
_SOLVER_LOCK = Lock()


def precheck(info):
    """Proven impossibilities can be reported without calling the model."""
    for key in MACROS:
        value = info["recorded"][key]
        if value is None:
            raise ModelError("MEAL_MACROS_UNKNOWN", f"已吃记录的{LABELS[key]}尚未知，无法核对三项营养范围。请先在饮食记录中补全或估算营养。", 409)
        target = info["macro"]["ranges"][key]
        if value["upper"] > target["upper"]:
            raise ModelError("MEAL_MACROS_EXCEEDED", f"已记录{LABELS[key]}的估算上界已超过全天参考上界，后续餐单不能抵消已经吃下的部分。请核对记录；不建议通过禁食强行凑数。", 409)
        if value["upper"] - value["lower"] > target["upper"] - target["lower"]:
            raise ModelError("MEAL_MACROS_UNCERTAIN", f"已记录{LABELS[key]}的估算范围太宽，无法让整个区间落入参考范围。请先核对已吃份量或营养估算，不需要强行称重。", 409)


def evaluate(plans, info, data):
    projected = deepcopy(info["recorded"])
    for plan in plans:
        # Recompute from items, not a cached model or solver total.
        total = nutrition_result(plan["items"], None, "", info, data)["totals"]
        for key in NAMES:
            if projected[key] is not None:
                for side in ("lower", "upper"):
                    projected[key][side] += total[key][side]
    within = all(projected[key] is not None and
                 projected[key]["lower"] >= info["macro"]["ranges"][key]["lower"] - 1e-7 and
                 projected[key]["upper"] <= info["macro"]["ranges"][key]["upper"] + 1e-7 for key in MACROS)
    midpoint = sum(projected["kcal"].values()) / 2
    energy_ok = abs(midpoint - info["target_kcal"]) <= info["target_kcal"] * ENERGY_TOLERANCE + 1e-7
    return {"policy": POLICY, "status": "within" if within and energy_ok else "outside",
            "projected": projected, "energy_midpoint": round(midpoint, 2),
            "energy_tolerance": ENERGY_TOLERANCE, "record_state": info["record_state"]}


def _solve(plans, mutable, info, data, expand):
    costs, lower, upper, integral, rows, lows, highs = [], [], [], [], [], [], []
    variables = []
    constants = {key: dict(info["recorded"][key]) for key in NAMES}
    nutrient_terms = {key: {} for key in NAMES}
    repeated, fixed_counts = {}, Counter()
    recent = info.get("food_history", {}).get("counts", {})

    def variable(lo, hi, cost=0, integer=False):
        index = len(costs)
        costs.append(cost); lower.append(lo); upper.append(hi); integral.append(int(integer))
        return index

    def constrain(terms, lo=-np.inf, hi=np.inf):
        rows.append(terms); lows.append(lo); highs.append(hi)

    def deviation(terms, target, weight):
        error = variable(0, np.inf, weight / max(abs(target), 10))
        constrain({**terms, error: -1}, hi=target)
        constrain({**terms, error: 1}, lo=target)

    def excess(terms, limit, weight):
        error = variable(0, np.inf, weight)
        constrain({**terms, error: -1}, hi=limit)

    for meal_index, plan in enumerate(plans):
        original = {item["food_id"]: item for item in plan["items"]}
        fixed = original if plan["id"] not in mutable else ({} if expand else {item["food_id"]: item for item in plan.get("fit_kept_items", [])})
        for item in fixed.values():
            if item["group"] in {"protein", "vegetable"}:
                fixed_counts[item["food_id"]] += 1
            for side in ("lower", "upper"):
                for key, value in amount_nutrients(item["food_id"], item[side], data).items():
                    constants[key][side] += value
        if plan["id"] not in mutable:
            continue
        caps = {item["food_id"]: min(item["max_lower"], item["max_upper"]) for item in plan.get("portion_limits", [])}
        blocked = set(plan["excluded_foods"])
        candidates = [key for key in FOODS if key not in blocked and key not in fixed and (expand or key in original)]
        groups = {group: {} for group in ("starch", "protein", "vegetable", "fat", "fruit", "dairy")}
        counts = Counter(item["group"] for item in fixed.values())
        all_selected, vegetables, meal_energy, starch, rice = {}, {}, {}, {}, {}
        for food_id in candidates:
            name, group, unit, basis, minimum, maximum = FOODS[food_id]
            step = 1 if unit == "个" or group == "fat" else 10
            maximum = min(QUICK_MAX.get(food_id, maximum), caps.get(food_id, math.inf))
            if maximum < minimum:
                if food_id in caps:
                    return None
                continue
            required = not expand or food_id in caps or (bool(plan.get("required_groups")) and food_id in original)
            choice_cost = -.3 if food_id in original else .6
            if group in {"protein", "vegetable"}:
                choice_cost += recent.get(food_id, 0) * .35
            selected = variable(int(required), 1, choice_cost, True)
            quantity = variable(0, math.floor(maximum / step), 0, True)
            constrain({quantity: 1, selected: -math.ceil(minimum / step)}, lo=0)
            constrain({quantity: 1, selected: -math.floor(maximum / step)}, hi=0)
            all_selected[selected] = 1
            groups[group][selected] = 1
            if group in {"protein", "vegetable"}:
                repeated.setdefault(food_id, {})[selected] = 1
            if food_id in STAPLE_PORTIONS:
                starch[quantity] = step / STAPLE_PORTIONS[food_id]
            if food_id in RICE_FAMILY:
                rice[selected] = 1
            if group == "vegetable":
                vegetables[quantity] = step
            for key, value in amount_nutrients(food_id, step, data).items():
                nutrient_terms[key][quantity] = value
                if key == "kcal":
                    meal_energy[quantity] = value
            if food_id in original:
                deviation({quantity: step}, (original[food_id]["lower"] + original[food_id]["upper"]) / 2, .1)
            variables.append((meal_index, food_id, quantity, selected, step))
        required_groups = plan.get("required_groups")
        for group, terms in groups.items():
            if required_groups:
                lo = hi = required_groups.get(group, 0)
                if expand:
                    hi = {"starch": 2, "protein": 1, "vegetable": 2, "fat": 2, "fruit": 1, "dairy": 1}[group]
            elif not expand:
                lo = hi = sum(item["group"] == group for item in original.values())
            else:
                lo, hi = {"starch": (1, 2), "protein": (1, 1), "vegetable": (1, 2),
                          "fat": (0, 2), "fruit": (0, 1), "dairy": (0, 1)}[group]
                if group in plan.get("requested_groups", []):
                    lo = max(1, lo)
            constrain(terms, lo - counts[group], hi - counts[group])
        constrain(all_selected, hi=7 - len(fixed))
        constrain(vegetables, hi=400 - sum(item["upper"] for item in fixed.values() if item["group"] == "vegetable"))
        fixed_starch = sum((item["lower"] + item["upper"]) / 2 / STAPLE_PORTIONS[item["food_id"]]
                           for item in fixed.values() if item["food_id"] in STAPLE_PORTIONS)
        excess(starch, 2 - fixed_starch, 3)
        excess(rice, 1 - sum(item["food_id"] in RICE_FAMILY for item in fixed.values()), 2)
        fixed_energy = sum(sum(amount_nutrients(item["food_id"], item[side], data)["kcal"] for side in ("lower", "upper")) / 2 for item in fixed.values())
        deviation(meal_energy, plan["quick_nutrition"]["portion_reference"]["kcal"] - fixed_energy, .5)

    for key in MACROS:
        target = info["macro"]["ranges"][key]
        lo = target["lower"] - constants[key]["lower"]
        hi = target["upper"] - constants[key]["upper"]
        # A tiny interior margin protects per-item display rounding, not a relaxed bound.
        margin = min(.1, max(0, (hi - lo) / 4))
        constrain(nutrient_terms[key], lo + margin, hi - margin)
        desired = info["macro"]["center"][key] - sum(constants[key].values()) / 2
        # Normalize to the daily reference, not a tiny remaining amount after a large meal.
        weight = {"protein": 15, "carbs": 8, "fat": 8}[key] * max(abs(desired), 10) / max(info["macro"]["center"][key], 10)
        deviation(nutrient_terms[key], desired, weight)
    for key, terms in repeated.items():
        excess(terms, 1 - fixed_counts[key], 1.5)
    remaining_energy = info["target_kcal"] - sum(constants["kcal"].values()) / 2
    tolerance = info["target_kcal"] * ENERGY_TOLERANCE
    constrain(nutrient_terms["kcal"], remaining_energy - tolerance + .1, remaining_energy + tolerance - .1)
    deviation(nutrient_terms["kcal"], remaining_energy, 10)
    matrix = np.zeros((len(rows), len(costs)))
    for index, terms in enumerate(rows):
        for column, value in terms.items():
            matrix[index, column] = value
    # HiGHS' native parallel scheduler is unreliable on some Windows builds.
    # Use a bounded, serialized single-thread solve, including on server hosts.
    if not _SOLVER_LOCK.acquire(timeout=2):
        raise ModelError("MEAL_BALANCE_BUSY", "配餐计算正在处理其他请求，请稍后再试；未重复调用模型。", 503)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Unrecognized options detected:.*threads.*", category=RuntimeWarning)
            result = milp(np.array(costs), integrality=np.array(integral), bounds=Bounds(lower, upper),
                          constraints=LinearConstraint(matrix, lows, highs),
                          options={"time_limit": 3.0, "node_limit": 10000, "mip_rel_gap": .02, "threads": 1})
    finally:
        _SOLVER_LOCK.release()
    if result.x is None:
        if result.status == 2:
            return None
        raise ModelError("MEAL_BALANCE_TIMEOUT", "本次配餐计算未完成，未发布未经核对的餐单。已有建议保留，可以稍后再试。", 503)
    output = deepcopy(plans)
    for plan in output:
        if plan["id"] in mutable:
            plan["items"] = [] if expand else deepcopy(plan.get("fit_kept_items", []))
            plan["joint_refit"] = expand
    for meal_index, food_id, quantity, selected, step in variables:
        if result.x[selected] < .5:
            continue
        amount = int(round(result.x[quantity])) * step
        name, group, unit, basis, _, _ = FOODS[food_id]
        output[meal_index]["items"].append({"food_id": food_id, "name": name, "group": group,
                                           "unit": unit, "basis": basis, "lower": amount, "upper": amount})
    if evaluate(output, info, data)["status"] != "within":
        if result.status == 1:
            raise ModelError("MEAL_BALANCE_TIMEOUT", "本次计算尚未找到通过三项营养核对的结果，未发布新餐单。", 503)
        return None
    return output


def balance(plans, mutable, info, data):
    precheck(info)
    valid = evaluate(plans, info, data)["status"] == "within"
    if not mutable and valid:
        return deepcopy(plans), evaluate(plans, info, data)
    candidates = [deepcopy(plans)] if valid else []
    for expand in (False, True):
        try:
            result = _solve(plans, mutable, info, data, expand)
        except ModelError:
            if not candidates:
                raise
            continue
        if result is not None:
            candidates.append(result)
            if not expand and any(plan.get("fit_kept_items") for plan in plans if plan["id"] in mutable):
                return result, evaluate(result, info, data)
            if not expand and quality(result, info, data) < .6:
                break
    if candidates:
        result = min(candidates, key=lambda candidate: quality(candidate, info, data))
        return result, evaluate(result, info, data)
    raise ModelError("MEAL_BALANCE_INFEASIBLE", "现有食材、份量上限及保留餐次的条件下，未找到蛋白质、碳水、脂肪全部在参考范围内且热量接近目标的组合。没有发布未达标的新餐单；可改选食材或重新安排所有剩余餐次。过敏限制仍保留，不建议强行进食或禁食。", 409)
