"""Explicit, snapshot-bound estimates for planned portions, never food records."""
import json
import time
from decimal import Decimal

from model.factory import ModelError
from services.meal_plans import MealPlanService, digest, encode
from services.nutrition import NUTRIENTS, estimate_foods

RETRY_SECONDS = 180


def portion_food(item):
    return {"name": item["name"], "grams": None,
            "amount_description": f'{item["lower"]}{item["unit"]}至{item["upper"]}{item["unit"]}；{item["basis"]}；不含额外用油和调味料'}


def result_for(items, nutrition):
    results = []
    for item, estimate in zip(items, nutrition):
        if estimate["status"] == "estimated" and item["unit"] == "g":
            if estimate["kcal"]["upper"] > item["upper"] * 10 or sum(estimate[key]["lower"] for key in NUTRIENTS[1:]) > item["upper"]:
                raise ModelError("MODEL_INVALID_OUTPUT", "餐单营养估算未通过份量检查，原餐单保留。")
        results.append({**item, **{key: value for key, value in estimate.items() if key != "index"}})
    unknown = sum(item["status"] == "unknown" for item in results)
    totals = None if unknown else {name: {side: float(sum((Decimal(str(item[name][side])) for item in results), Decimal(0)))
                                          for side in ("lower", "upper")} for name in NUTRIENTS}
    return {"source_type": "model_estimate", "items": results, "totals": totals, "unknown_count": unknown,
            "basis": "planned_portions_without_extra_oil", "independently_verified": False}


class MealPlanNutritionService:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id = database, user_id
        self.plans = MealPlanService(database, user_id, retriever)

    def read(self, connection, plan_id, sources):
        row = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=? AND status IN ('draft','accepted')",
                                 (str(plan_id), self.user_id)).fetchone()
        if row is None:
            raise ModelError("PLAN_NOT_FOUND", "餐单不存在或不可访问。", 404)
        original = json.loads(row["input_payload"])
        _, fingerprint, version = self.plans.snapshot(connection, row["day"], row["meal_type"], sources, original)
        view = self.plans.view(row, fingerprint, version)
        latest = connection.execute("SELECT id FROM meal_plans WHERE user_id=? AND day=? AND meal_type=? "
                                    "AND json_extract(input_payload,'$.coach_id') IS ? AND status IN ('draft','accepted') "
                                    "ORDER BY rowid DESC LIMIT 1", (self.user_id, row["day"], row["meal_type"], original.get("coach_id"))).fetchone()
        if view["stale"] or latest[0] != row["id"]:
            raise ModelError("PLAN_CONTEXT_CHANGED", "餐单已失效或已有更新版本，请刷新后估算当前餐单。", 409)
        return row, view

    def estimate(self, body, model_factory, model_name):
        request_id = str(body.client_id)
        request_hash = digest(body.model_dump(mode="json"))
        sources = self.plans.evidence()
        missing, originals, cached = [], {}, []
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in body.items:
                row, view = self.read(connection, item.plan_id, sources)
                review = view.get("nutrition_review", {})
                if review.get("client_id") == request_id:
                    if review["request_hash"] != request_hash:
                        raise ModelError("PLAN_NUTRITION_CONFLICT", "估算请求内容已改变，请刷新后重新核对。", 409)
                    if review["status"] != "ready":
                        raise ModelError("PLAN_NUTRITION_PENDING", "这次估算尚未完成；请刷新核对状态，失败后可明确重新估算。", 409)
                elif item.version != review.get("version", 0):
                    raise ModelError("PLAN_NUTRITION_CHANGED", "餐单估算状态已变化，请刷新；旧请求不会覆盖新结果。", 409)
                if review.get("status") == "ready":
                    cached.append(view)
                else:
                    if review.get("status") == "generating" and time.time() - review["started_at"] < RETRY_SECONDS:
                        raise ModelError("PLAN_NUTRITION_PENDING", "餐单正在估算，请稍后刷新；超过3分钟可明确重新估算。", 409)
                    missing.append(view)
                originals[view["id"]] = row["context_hash"]
            all_plans = cached + missing
            if len({(p["day"], p["coach_id"]) for p in all_plans}) != 1 or len({p["meal_type"] for p in all_plans}) != len(all_plans):
                raise ModelError("PLAN_NUTRITION_SCOPE", "一次只估算同日同一对话的不同餐次，不能累计同餐多个版本。", 422)
            for plan in missing:
                row = connection.execute("SELECT payload FROM meal_plans WHERE id=? AND user_id=?", (plan["id"], self.user_id)).fetchone()
                payload = json.loads(row[0])
                payload["nutrition_review"] = {"version": payload.get("nutrition_review", {}).get("version", 0) + 1,
                    "client_id": request_id, "request_hash": request_hash, "status": "generating", "started_at": time.time()}
                connection.execute("UPDATE meal_plans SET payload=? WHERE id=? AND user_id=?", (encode(payload), plan["id"], self.user_id))
        if not missing:
            return self.plans.list(all_plans[0]["day"])
        try:
            foods = [portion_food(item) for plan in missing for item in plan["items"]]
            nutrition = estimate_foods(foods, model_factory, model_name, planned=True)
            results, offset = {}, 0
            for plan in missing:
                size = len(plan["items"])
                results[plan["id"]] = result_for(plan["items"], nutrition["items"][offset:offset + size])
                offset += size
            current_sources = self.plans.evidence()
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Validate the complete batch before storing any result, including cached meals.
                for plan in all_plans:
                    row, current = self.read(connection, plan["id"], current_sources)
                    if row["context_hash"] != originals[plan["id"]]:
                        raise ModelError("PLAN_CONTEXT_CHANGED", "估算期间餐单依据或记录已变化，未使用旧结果。", 409)
                    if plan["id"] in results:
                        payload = json.loads(row["payload"])
                        review = payload["nutrition_review"]
                        if review["client_id"] != request_id or review["status"] != "generating":
                            raise ModelError("PLAN_NUTRITION_CHANGED", "已有更新的估算，未覆盖新状态。", 409)
                        review.update(status="ready", result=results[plan["id"]])
                        connection.execute("UPDATE meal_plans SET payload=? WHERE id=? AND user_id=?", (encode(payload), plan["id"], self.user_id))
        except Exception:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for plan in missing:
                    row = connection.execute("SELECT payload FROM meal_plans WHERE id=? AND user_id=?", (plan["id"], self.user_id)).fetchone()
                    if row:
                        payload = json.loads(row[0])
                        review = payload.get("nutrition_review", {})
                        if review.get("client_id") == request_id and review.get("status") == "generating":
                            review["status"] = "failed"
                            connection.execute("UPDATE meal_plans SET payload=? WHERE id=? AND user_id=?", (encode(payload), plan["id"], self.user_id))
            raise
        return self.plans.list(all_plans[0]["day"])
