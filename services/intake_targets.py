"""Confirmed targets, optional maintenance references and recorded-intake comparisons."""
import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException

from schemas import Profile
from services.plan_foods import MEDICAL
from services.energy_estimates import maintenance_reference, adjusted_reference
from services.profile_context import read_profile, ENERGY_FIELDS


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def target_state(connection, user_id, day, *, with_feedback=True):
    profile = read_profile(connection, user_id)
    fields = ("goal", "height_cm", "weight_kg", "preferences", "food_allergies")
    context_hash = hashlib.sha256(encode({"policy": "manual-intake-v1", **{key: profile[key] for key in fields},
                                         **{key: profile[key] for key in ENERGY_FIELDS if profile[key] is not None}}).encode()).hexdigest()
    scope_blocked = bool(MEDICAL.search(" ".join(str(profile[key]) for key in fields)))
    version = connection.execute("SELECT COALESCE(MAX(version),0) FROM intake_targets WHERE user_id=?", (user_id,)).fetchone()[0]
    rows = connection.execute("SELECT * FROM intake_targets WHERE user_id=? AND effective_from<=? "
                              "ORDER BY effective_from DESC,version DESC", (user_id, day.isoformat())).fetchall()
    baseline = next((row for row in rows if json.loads(row["input_payload"]).get("scope") != "day"), None)
    daily = next((row for row in rows if row["effective_from"] == day.isoformat() and json.loads(row["input_payload"]).get("scope") == "day"), None)
    row = daily if daily and daily["kcal"] is not None else baseline
    baseline_payload = json.loads(baseline["input_payload"]) if baseline else {}
    payload = json.loads(row["input_payload"]) if row else {}
    target = {key: row[key] for key in ("id", "version", "effective_from", "kcal", "source", "confirmed_at")} if row else None
    if target:
        target["estimate"] = payload.get("estimate")
        target["calculation"] = payload.get("calculation")
        target["scope"] = payload.get("scope", "baseline")
    status = "unset" if row is None else "paused" if row["kcal"] is None else "needs_review" if row["context_hash"] != context_hash else "active"
    if payload.get("scope"):
        status = payload.get("status", "active")
    if status == "active":
        review_on = (target["estimate"] or {}).get("adjustment", {}).get("review_on")
        if review_on and day.isoformat() >= review_on:
            status = "review_due"
    if scope_blocked:
        status = "out_of_scope"
    result = {"day": day.isoformat(), "version": version, "context_hash": context_hash, "status": status, "target": target,
            "measurements": {key: profile[key] for key in ("height_cm", "weight_kg", "goal")},
            "profile_inputs": {key: profile[key] for key in ENERGY_FIELDS},
            "baseline": {"kcal": baseline["kcal"], "standard": baseline_payload.get("standard"),
                         "calculation": baseline_payload.get("calculation"), "error": baseline_payload.get("error"),
                         "effective_from": baseline["effective_from"]} if baseline else None,
            "day_override": bool(daily and daily["kcal"] is not None), "error": payload.get("error")}
    if with_feedback:
        from services.energy_feedback import apply_feedback
        return apply_feedback(connection, user_id, day, result)
    return result


class IntakeTargetService:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id

    def get(self, day):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return target_state(connection, self.user_id, day)

    def estimate(self, body, *, adjusted=False):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            current = target_state(connection, self.user_id, body.day)
            if current["context_hash"] != body.context_hash:
                raise HTTPException(409, "档案已变化，请重新打开测算并核对身高体重")
            self._check_scope(current)
            estimate = (adjusted_reference(current["measurements"], body.inputs, body.adjustment, body.day)
                        if adjusted else maintenance_reference(current["measurements"], body.inputs))
            return {"target_state": current, "estimate": estimate}

    @staticmethod
    def _check_scope(current):
        if current["status"] == "out_of_scope":
            raise HTTPException(409, "档案涉及特殊饮食管理情况，暂不启用数值目标对比；原记录保留，请向合适的专业人士核对")

    def change(self, body, *, estimated=False, adjusted=False):
        payload = encode(body.model_dump(mode="json"))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = target_state(connection, self.user_id, body.effective_from)
            previous = connection.execute("SELECT input_payload FROM intake_targets WHERE user_id=? AND client_id=?",
                                          (self.user_id, str(body.client_id))).fetchone()
            if previous:
                stored = json.loads(previous[0])
                if encode(stored.get("request", stored)) != payload:
                    raise HTTPException(409, "这次提交标识已用于其他内容，请重新核对目标")
                return current
            if current["version"] != body.version or current["context_hash"] != body.context_hash:
                raise HTTPException(409, "目标或档案已变化；输入已保留，请重新打开目标设置后核对")
            if body.kcal is not None:
                self._check_scope(current)
            if estimated or adjusted:
                estimate = (adjusted_reference(current["measurements"], body.inputs, body.adjustment, body.effective_from)
                            if adjusted else maintenance_reference(current["measurements"], body.inputs))
                if body.method != estimate["method"] or body.kcal != estimate["kcal"]:
                    raise HTTPException(409, "估算结果或公式版本已变化，请重新测算并核对，原目标不变")
                source = estimate["source"] + ("（用户确认，非自动建议）" if adjusted else " · 维持参考估算（用户确认）")
                payload = encode({"request": json.loads(payload), "estimate": estimate})
            else:
                source = body.source
            connection.execute("INSERT INTO intake_targets(id,user_id,client_id,version,effective_from,kcal,source,context_hash,input_payload) "
                               "VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid4()), self.user_id, str(body.client_id), current["version"] + 1,
                               body.effective_from.isoformat(), body.kcal, source, current["context_hash"], payload))
            return target_state(connection, self.user_id, body.effective_from)


def intake_comparison(nutrition, estimates, meal_count, target):
    known = nutrition["kcal"]["known_total"]
    estimated = estimates["kcal"]
    has_value = known is not None or estimated["count"] > 0
    # Add recorded ranges; no statistical confidence or unrecorded intake is inferred.
    lower = Decimal(str(known or 0)) + Decimal(str(estimated["lower_total"] or 0))
    upper = Decimal(str(known or 0)) + Decimal(str(estimated["upper_total"] or 0))
    recorded = {"lower": float(round(lower, 2)), "upper": float(round(upper, 2))} if has_value else None
    status = target["status"] if target["status"] != "active" else "no_records" if not meal_count else "unknown_intake" if estimated["unknown_count"] else "ready"
    difference = None
    if status == "ready":
        kcal = Decimal(target["target"]["kcal"])
        difference = {"lower": float(round(kcal - upper, 2)), "upper": float(round(kcal - lower, 2))}
    return {"status": status, "recorded_kcal": recorded, "difference_kcal": difference,
            "unknown_count": estimated["unknown_count"], "estimated_count": estimated["count"],
            "basis": "target_minus_recorded_intake", "exercise_added": False}
