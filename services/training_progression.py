"""Optional actual working sets, never model-inferred or auto-written load history."""
import json
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from schemas import WorkoutPerformance
from services.business_time import business_today
from services.training_catalog import read_catalog

SOURCE = "acsm-2009:progression"
# These actions have a documented 12-repetition target/upper bound in our catalog.
# Unilateral/assisted/band/Smith and dual-stack actions deliberately stay untracked.
BASES = {
    "db-bench": "each", "incline-db": "each", "floor-press": "each",
    "db-shoulder": "each", "db-rdl": "each", "goblet-squat": "total",
    "barbell-bench": "total", "barbell-row": "total", "barbell-squat": "total",
    "barbell-rdl": "total", "barbell-curl": "total", "incline-barbell": "total",
    "machine-chest": "stack", "machine-shoulder": "stack", "machine-row": "stack",
    "machine-curl": "stack", "lat-pulldown": "stack", "leg-press": "stack",
    "knee-extension": "stack", "hamstring-curl": "stack", "seated-leg-curl": "stack",
    "machine-adduction": "stack", "machine-abduction": "stack",
    "machine-rear-delt": "stack", "machine-triceps": "stack", "machine-crunch": "stack",
}
BASIS_LABELS = {"each": "每只哑铃", "total": "总负重（含杆）", "stack": "该器械配重刻度"}


def tracking_catalog():
    return [{"id": item["id"], "name": item["name"], "load_basis": BASES[item["id"]],
             "basis_label": BASIS_LABELS[BASES[item["id"]]]}
            for item in read_catalog()[0] if item["id"] in BASES]


def validate_performance(payload):
    performance = payload.get("performance")
    if not performance:
        return
    item = next((item for item in tracking_catalog() if item["id"] == performance["exercise_id"]), None)
    if not item or payload["name"] != item["name"] or performance["load_basis"] != item["load_basis"]:
        raise HTTPException(422, "动作表现与项目名称或负重口径不匹配，请核对；普通训练可不填动作表现。")


def performance_history(connection, user_id, day):
    anchor = date.fromisoformat(day)
    end = min(anchor, business_today())
    start = anchor - timedelta(days=min(6, anchor.toordinal() - 1))
    rows = connection.execute(
        "SELECT id,day,payload FROM workouts WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day DESC,id DESC LIMIT 3001",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    if len(rows) > 3000:
        return []
    result = []
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("status") == "completed" and payload.get("performance"):
            result.append({"id": row["id"], "day": row["day"], "name": payload.get("name"),
                           "performance": payload["performance"]})
    return result


def guidance(action, history, day, evidence):
    if action["id"] not in BASES:
        return None
    result = {"status": "insufficient", "message": "未有两次可比较的实际组次，不预设重量。", "basis": BASIS_LABELS[BASES[action["id"]]],
              "reference_days": [], "source_id": SOURCE}
    if SOURCE not in evidence:
        return {**result, "status": "unavailable", "message": "负重进阶依据不可用，未给出加重建议。", "source_id": None}
    relevant = [row for row in history if row.get("performance", {}).get("exercise_id") == action["id"]][:2]
    if len(relevant) < 2:
        return result
    result["reference_days"] = [row["day"] for row in relevant]
    try:
        for row in relevant:
            validated = WorkoutPerformance.model_validate(row["performance"])
            validate_performance({"name": row["name"], "performance": validated.model_dump()})
        first, second = [row["performance"] for row in relevant]
        ages = [(date.fromisoformat(day) - date.fromisoformat(row["day"])).days for row in relevant]
    except (ValidationError, HTTPException, ValueError, KeyError):
        return result
    if relevant[0]["day"] == relevant[1]["day"] or not all(0 <= age <= 6 for age in ages):
        return result
    if (any(first[key] != second[key] for key in ("equipment_label", "load_basis", "increment_kg"))
            or len(first["sets"]) != len(second["sets"]) or len(first["sets"]) < action["sets"]):
        return {**result, "message": "两次器械设置、增量或组数不同，不能直接比较负重。"}
    sets = first["sets"] + second["sets"]
    weights = {Decimal(str(item["load_kg"])) for item in sets}
    if len(weights) != 1:
        return {**result, "message": "两次工作组重量不一致，未推算下次加重。"}
    current = next(iter(weights))
    result.update(current_kg=float(current), equipment_label=first["equipment_label"], status="hold",
                  message="暂不建议加重；先核对动作控制、余力与次数，不必为达标练到力竭。")
    if not all(p["technique_stable"] for p in (first, second)) or not all(
            13 <= item["reps"] <= 14 and item.get("rir") is not None and item["rir"] >= 2 for item in sets):
        return result
    increment = first.get("increment_kg")
    if increment is None:
        return {**result, "message": "表现满足复核条件，但缺少该器械最小可用增量，未给出新重量。"}
    step = Decimal(str(increment))
    if not Decimal("0.02") <= step / current <= Decimal("0.10") or current + step > 500:
        return {**result, "message": "所填最小增量不在当前重量的2%至10%范围内，不自动跳重。"}
    return {**result, "status": "consider", "suggested_kg": float(current + step), "increment_kg": float(step),
            "message": "仅在本次无异常疲劳、动作仍可控时，考虑一个最小增量；先从建议次数下限尝试，不强制完成。",
            "rule": "参考ACSM 2009的2%至10%进阶原则；两日全部工作组13至14次、余力至少2次及七日窗口是本产品的保守筛选，不是已恢复证明。"}


def attach_guidance(actions, history, day, evidence):
    # Copy: today's load hints must not propagate through shared weekly action objects.
    return [{**action, **({"load_guidance": hint} if (hint := guidance(action, history, day, evidence)) else {})}
            for action in actions]
