"""User-directed portion edits and an explicit bridge to actual-food drafts."""
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from model.factory import ModelError
from schemas import DraftCreate, MealPlanSelection
from services.meal_drafts import MealDraftStore
from services.meal_plan_nutrition import MealPlanNutritionService
from services.meal_plans import REQUIRED, encode, meal_restrictions
from services import meal_nutrient_reference as nutrient_reference


class MealPlanActions(MealPlanNutritionService):
    def edit_portions(self, plan_id, body, guard):
        guard()
        sources = self.plans.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            original_input = {"kind": "user_portions", "parent_id": str(plan_id), "request": body.model_dump(mode="json")}
            replay = connection.execute("SELECT * FROM meal_plans WHERE user_id=? AND client_id=?", (self.user_id, str(body.client_id))).fetchone()
            if replay:
                saved = json.loads(replay["input_payload"])
                if saved.get("portion_edit") != original_input:
                    raise ModelError("PLAN_REQUEST_CONFLICT", "这次提交标识已用于其他餐单修改。", 409)
                _, fingerprint, latest = self.plans.snapshot(connection, replay["day"], replay["meal_type"], sources, saved)
                return self.plans.view(replay, fingerprint, latest)
            row, plan = self.read(connection, plan_id, sources)
            original = json.loads(row["input_payload"])
            facts, fingerprint, version = self.plans.snapshot(connection, row["day"], row["meal_type"], sources, original)
            blocked, _, _ = meal_restrictions(facts, original)
            if {item.food_id for item in body.items} != {item["food_id"] for item in plan["items"]}:
                raise ModelError("PLAN_PORTION_SCOPE", "份量编辑只能调整原食材；更换食材请在对话中明确说明。", 422)
            items = self.plans.validate(MealPlanSelection(items=body.items, chunk_ids=list(REQUIRED)), blocked,
                                        quick=bool(original.get("quick_recommendation")))
            by_id = {item["food_id"]: item for item in items}
            items = [by_id[item["food_id"]] for item in plan["items"]]
            payload = json.loads(row["payload"])
            for key in ("nutrition_review", "portion_limits", "kept_items"):
                payload.pop(key, None)
            payload.update(items=items, base_version=version, replaces_plan_id=str(plan_id), portion_source="user",
                           requested_changes=["用户核对并调整食材份量"],
                           generated_at=datetime.now(timezone.utc).isoformat(),
                           portion_basis="用户填写的计划份量，尚非实际食用量；营养需按新份量重新估算，不保证符合每日目标。")
            if original.get("quick_recommendation"):
                reference = nutrient_reference.reference_data()
                budget, note = nutrient_reference.meal_budget(facts["nutrient_context"], row["meal_type"], facts["requested_meals"])
                payload["quick_nutrition"] = nutrient_reference.nutrition_result(items, budget, note, facts["nutrient_context"], reference)
                payload["portion_basis"] = "用户填写计划份量，按公共成分表重新核算；不是已吃或达标认证。"
            original.update(client_id=str(body.client_id), portion_edit=original_input)
            identifier = str(uuid4())
            connection.execute("INSERT INTO meal_plans(id,user_id,client_id,day,meal_type,input_payload,context_hash,status,payload) VALUES (?,?,?,?,?,?,?,'draft',?)",
                               (identifier, self.user_id, str(body.client_id), row["day"], row["meal_type"], encode(original), fingerprint, encode(payload)))
            result = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=?", (identifier, self.user_id)).fetchone()
            return self.plans.view(result, fingerprint, version)

    def record_draft(self, plan_id):
        # Historical meals may be recorded truthfully even when a suggestion is now stale.
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=? AND status IN ('draft','accepted')",
                                     (str(plan_id), self.user_id)).fetchone()
            if row is None:
                raise ModelError("PLAN_NOT_FOUND", "餐单不存在或不可访问。", 404)
            plan = json.loads(row["payload"])
        store = MealDraftStore(self.database, self.user_id)
        draft = store.create(DraftCreate(client_id=uuid5(UUID(str(plan_id)), "actual-food-draft"), day=row["day"],
                                        meal_type=row["meal_type"], text="实际食用待核对：" + "、".join(item["name"] for item in plan["items"])))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = store._read(connection, draft["id"])
            payload = json.loads(current["payload"])
            if current["version"] == 1 and not payload.get("record_plan_id"):
                payload.update(record_plan_id=str(plan_id), input_type="plan", items=[{
                    "name": item["name"], "grams": None, "amount_description": "", "confidence": "needs_confirmation"
                } for item in plan["items"]], questions=["请填写实际吃过的份量，删除没吃的食物，并补充用油、饮料或其他实际吃过的内容。计划生重不能直接当成熟食重量。"])
                store._write(connection, current, payload, current["day"], current["meal_type"])
            return store._view(store._read(connection, draft["id"]))
