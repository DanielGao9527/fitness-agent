"""Snapshot-bound, user-confirmed intake context; not per-meal calorie allocation."""
import hashlib
import json
from decimal import Decimal

from model.factory import ModelError
from services.intake_targets import encode, intake_comparison, target_state
from services.nutrition_totals import nutrition_totals


def intake_fingerprint(user_id, day, target, meals):
    from services.energy_feedback import unadjusted
    return hashlib.sha256(encode({"policy": "meal-intake-v1", "user_id": user_id,
                                  "day": day.isoformat(), "target": unadjusted(target), "meals": meals}).encode()).hexdigest()


def planning_context(connection, user_id, day):
    target = target_state(connection, user_id, day)
    meals = [{"id": row["id"], **json.loads(row["payload"])} for row in connection.execute(
        "SELECT id,payload FROM meals WHERE user_id=? AND day=? ORDER BY id", (user_id, day.isoformat()))]
    fingerprint = intake_fingerprint(user_id, day, target, meals)
    totals, estimates = nutrition_totals(meals)
    comparison = intake_comparison(totals, estimates, len(meals), target)
    row = connection.execute("SELECT * FROM meal_intake_reviews WHERE user_id=? AND day=?", (user_id, day.isoformat())).fetchone()
    record_state = (row["record_state"] if row and row["context_hash"] == fingerprint else None)
    confirmed = record_state == ("complete" if meals else "none_yet")
    recorded = comparison["recorded_kcal"]
    difference = None
    if target["status"] != "active":
        status = target["status"]
    elif estimates["kcal"]["unknown_count"]:
        status = "unknown_intake"
    elif not confirmed:
        status = "confirm_records" if meals else "confirm_empty"
    else:
        # Empty means zero only after an explicit statement that no intake has occurred.
        recorded = recorded or {"lower": 0, "upper": 0}
        kcal = Decimal(target["target"]["kcal"])
        difference = {"lower": float(kcal - Decimal(str(recorded["upper"]))),
                      "upper": float(kcal - Decimal(str(recorded["lower"])))}
        status = "target_reached" if difference["upper"] <= 0 else "uncertain_remaining" if difference["lower"] <= 0 else "ready"
    return {"day": day.isoformat(), "context_hash": fingerprint, "version": row["version"] if row else 0,
            "status": status, "target_status": target["status"], "target_kcal": target["target"]["kcal"] if target["target"] else None,
            "record_count": len(meals), "unknown_count": estimates["kcal"]["unknown_count"], "estimated_count": estimates["kcal"]["count"],
            "recorded_kcal": recorded, "remaining_kcal": difference,
            "record_state": record_state if confirmed else None, "confirmed_at": row["confirmed_at"] if confirmed else None,
            "basis": "target_minus_confirmed_intake", "exercise_added": False, "allocation": "not_allocated"}


def require_planning_context(context, fingerprint):
    if context["context_hash"] != fingerprint or context["status"] != "ready":
        raise ModelError("PLAN_INTAKE_CHANGED", "目标或已吃记录尚未核对、已变化或暂不支持数值参考，请刷新后核对；未调用模型。", 409)


def model_reference(context, meal_types):
    return {"daily_target_kcal": context["target_kcal"], "recorded_kcal": context["recorded_kcal"],
            "day_difference_kcal": context["remaining_kcal"], "record_state": context["record_state"],
            "requested_meals": meal_types, "allocation": "not_allocated", "exercise_added": False}


class MealIntakeService:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id

    def get(self, day):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return planning_context(connection, self.user_id, day)

    def confirm(self, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.confirm_in_transaction(connection, body)

    def confirm_in_transaction(self, connection, body):
        current = planning_context(connection, self.user_id, body.day)
        if current["context_hash"] != body.context_hash:
            raise ModelError("MEAL_INTAKE_CHANGED", "目标或饮食记录已变化，请刷新并重新核对。", 409)
        if current["target_status"] != "active" or current["unknown_count"]:
            raise ModelError("MEAL_INTAKE_UNAVAILABLE", "目标暂不可用或仍有热量未知的记录，不能确认数值参考；原记录保留。", 409)
        if body.record_state != ("complete" if current["record_count"] else "none_yet"):
            raise ModelError("MEAL_INTAKE_CONFLICT", "确认内容与现有饮食记录不一致，请刷新并核对。", 409)
        if current["record_state"] == body.record_state:
            return current
        if current["version"] != body.version:
            raise ModelError("MEAL_INTAKE_CHANGED", "核对状态已变化，请刷新；旧确认不会覆盖新状态。", 409)
        connection.execute("INSERT INTO meal_intake_reviews(user_id,day,version,context_hash,record_state) VALUES (?,?,?,?,?) "
                           "ON CONFLICT(user_id,day) DO UPDATE SET version=excluded.version,context_hash=excluded.context_hash,"
                           "record_state=excluded.record_state,confirmed_at=CURRENT_TIMESTAMP",
                           (self.user_id, body.day.isoformat(), current["version"] + 1, body.context_hash, body.record_state))
        return planning_context(connection, self.user_id, body.day)

    def revoke(self, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = planning_context(connection, self.user_id, body.day)
            row = connection.execute("SELECT record_state FROM meal_intake_reviews WHERE user_id=? AND day=?", (self.user_id, body.day.isoformat())).fetchone()
            if row and row[0] is not None:
                if current["version"] != body.version:
                    raise ModelError("MEAL_INTAKE_CHANGED", "核对状态已变化，请刷新后再撤回。", 409)
                connection.execute("UPDATE meal_intake_reviews SET record_state=NULL,version=version+1 WHERE user_id=? AND day=?",
                                   (self.user_id, body.day.isoformat()))
            return planning_context(connection, self.user_id, body.day)
