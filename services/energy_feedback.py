"""Bounded, read-only feedback from reviewed historical facts, never calorie debt."""
from copy import deepcopy
from datetime import timedelta
import json
import math

from fastapi import HTTPException
from schemas import EnergyEstimateInputs
from services.business_time import business_today
from services.energy_estimates import maintenance_reference
from services.nutrition_totals import nutrition_totals

POLICY = "reviewed-seven-days-v1"


def unadjusted(state):
    result = deepcopy(state)
    result.pop("energy_feedback", None)
    if result.get("target") and "base_kcal" in result["target"]:
        result["target"]["kcal"] = result["target"].pop("base_kcal")
    return result


def training_increment(workouts, calculation):
    completed = [item for item in workouts if item.get("status") == "completed"]
    if not completed:
        return (0., 0.)
    if any(not item.get("calorie_estimate") or item["calorie_estimate"].get("basis") != "gross_activity" for item in completed):
        return None
    maintenance = calculation["maintenance"]
    try:
        inactive = maintenance_reference(calculation["measurements"], EnergyEstimateInputs.model_validate(
            {**maintenance["inputs"], "activity": "inactive"}))["kcal"]
        durations = [item["minutes"] for item in completed]
        if any(not math.isfinite(value) or value <= 0 for value in durations):
            return None
        minutes = sum(durations)
        if not 0 < minutes <= 360:
            return None
        gross = [sum(item["calorie_estimate"]["kcal"][side] for item in completed) for side in ("lower", "upper")]
        if any(not math.isfinite(value) or value < 0 for value in gross) or gross[1] < gross[0]:
            return None
        # Replace that part of an inactive day, then take max: never add gross to active EER.
        return tuple(max(0., inactive * (1 - minutes / 1440) + value - maintenance["kcal"]) for value in gross)
    except (KeyError, TypeError, ValueError, HTTPException):
        return None


def apply_feedback(connection, user_id, day, state):
    from services.intake_targets import target_state
    from services.meal_intake import intake_fingerprint

    if day != business_today() or state["status"] != "active" or not state.get("target"):
        return state
    result = deepcopy(state)
    base = state["target"]["kcal"]
    feedback = {"policy": POLICY, "adjustment_kcal": 0, "eligible_days": 0, "status": "insufficient_history",
                "notice": "仅按近期已核对记录小幅调整；消耗仍为估算，不要求补吃或禁食来追平。"}
    result["energy_feedback"] = feedback
    result["target"]["base_kcal"] = base
    if state["day_override"]:
        feedback["status"] = "manual_override"
        return result
    baseline = state.get("baseline") or {}
    if not (baseline.get("standard") or {}).get("mode") == "calculated":
        feedback["status"] = "baseline_required"
        return result
    differences = [0., 0.]
    evidence = []
    for offset in range(1, 8):
        past = day - timedelta(days=offset)
        if past.isoformat() < baseline["effective_from"]:
            break
        historical = target_state(connection, user_id, past, with_feedback=False)
        if historical["status"] != "active" or historical["day_override"]:
            continue
        calculation = (historical.get("target") or {}).get("calculation") or {}
        if not calculation.get("maintenance") or calculation.get("goal") != state["measurements"]["goal"]:
            continue
        meals = [{"id": row["id"], **json.loads(row["payload"])} for row in connection.execute(
            "SELECT id,payload FROM meals WHERE user_id=? AND day=? ORDER BY id", (user_id, past.isoformat()))]
        # A review means 'complete so far'; breakfast-only reviews are not whole-day evidence.
        if not {"breakfast", "lunch", "dinner"} <= {item.get("meal_type") for item in meals}:
            continue
        review = connection.execute("SELECT * FROM meal_intake_reviews WHERE user_id=? AND day=?", (user_id, past.isoformat())).fetchone()
        if not review or review["record_state"] != "complete" or review["context_hash"] != intake_fingerprint(user_id, past, historical, meals):
            continue
        known, estimated = nutrition_totals(meals)
        calories = estimated["kcal"]
        if calories["unknown_count"]:
            continue
        intake = [(known["kcal"]["known_total"] or 0) + (calories[side + "_total"] or 0) for side in ("lower", "upper")]
        if intake[1] - intake[0] > base * .20:
            continue
        workouts = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM workouts WHERE user_id=? AND day=? ORDER BY id", (user_id, past.isoformat()))]
        increment = training_increment(workouts, calculation)
        if increment is None:
            continue
        expected = historical["target"]["kcal"]
        differences[0] += expected + increment[0] - intake[1]
        differences[1] += expected + increment[1] - intake[0]
        evidence.append({"day": past.isoformat(), "intake": intake, "training_increment": increment, "target": expected})
    feedback["eligible_days"] = len(evidence)
    feedback["evidence"] = evidence
    if not evidence:
        return result
    error = differences[0] if differences[0] > 0 else differences[1] if differences[1] < 0 else 0
    cap = min(150, base * .05)
    adjustment = int(math.copysign(math.floor(min(abs(error) / 3, cap) / 10) * 10, error))
    adjustment = max(1201 - base, min(5000 - base, adjustment))
    feedback.update(adjustment_kcal=adjustment, status="adjusted" if adjustment else "within_uncertainty",
                    deviation_kcal={"lower": round(differences[0], 2), "upper": round(differences[1], 2)})
    result["target"]["kcal"] = base + adjustment
    return result
