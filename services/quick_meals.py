"""One explicit chat request coordinates cached, constraint-bound meal suggestions."""
import json
from collections import Counter
from datetime import datetime, timezone
from uuid import UUID, uuid5

from model.factory import ModelError
from schemas import MealPlanRequest, MealPlanSelection
from services.coach import CoachService
from services.meal_plans import MealPlanService, encode, meal_restrictions, check_kept_items, check_portion_limits, REQUIRED
from services.meal_nutrient_reference import current_context, require_ready, reference_data, nutrition_result
from services import meal_balance


class QuickMealService:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id = database, user_id
        self.coach = CoachService(database, user_id)
        self.plans = MealPlanService(database, user_id, retriever)

    def row(self, connection, conversation_id, version):
        row = self.coach.owned(connection, conversation_id)
        if row["version"] != version or row["pending"] or row["intent"] != "meal":
            raise ModelError("COACH_CONTEXT_CHANGED", "对话已变化或还有待核对的内容，请刷新后再安排。", 409)
        turn = connection.execute("SELECT response FROM coach_turns WHERE conversation_id=? AND version=?",
                                  (str(conversation_id), version)).fetchone()
        if not turn:
            raise ModelError("COACH_CONTEXT_CHANGED", "当前没有可生成餐单的请求。", 409)
        return json.loads(turn[0])

    def save(self, connection, conversation_id, version, response):
        connection.execute("UPDATE coach_turns SET response=? WHERE conversation_id=? AND version=?",
                           (encode(response), str(conversation_id), version))

    def candidates(self, conversation_id, day):
        sources = self.plans.evidence()
        result = self.plans.list(day)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            for row in connection.execute("SELECT * FROM meal_plans WHERE user_id=? AND day=? AND status='generating' "
                                          "AND json_extract(input_payload,'$.coach_id')=? AND json_extract(payload,'$.candidate_ready')=1 ORDER BY rowid DESC",
                                          (self.user_id, day, str(conversation_id))):
                original = json.loads(row["input_payload"])
                _, fingerprint, latest = self.plans.snapshot(connection, day, row["meal_type"], sources, original)
                result.append(self.plans.view(row, fingerprint, latest))
        return result

    def publish(self, selected, conversation_id, version, request_id, info, guard):
        reference = reference_data()
        mutable = {plan["id"] for plan in selected if plan["status"] == "generating"}
        balanced, checked = meal_balance.balance(selected, mutable, info, reference)
        guard()
        sources = self.plans.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            response = self.row(connection, conversation_id, version)
            if response.get("quick_request", {}).get("client_id") != request_id:
                raise ModelError("COACH_CONTEXT_CHANGED", "请求已变化，未发布旧餐单。", 409)
            fresh = current_context(connection, self.user_id, selected[0]["day"])
            if fresh != info or reference_data()["fingerprint"] != reference["fingerprint"]:
                raise ModelError("PLAN_CONTEXT_CHANGED", "配餐期间记录、目标或依据变化，请按最新资料重新安排。", 409)
            for previous, plan in zip(selected, balanced):
                row = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=?", (plan["id"], self.user_id)).fetchone()
                if not row or row["status"] != previous["status"]:
                    raise ModelError("PLAN_CONTEXT_CHANGED", "餐单已变化，未覆盖其他操作。", 409)
                original = json.loads(row["input_payload"])
                facts, fingerprint, latest = self.plans.snapshot(connection, row["day"], row["meal_type"], sources, original)
                view = self.plans.view(row, fingerprint, latest)
                if view["stale"] or view != previous:
                    raise ModelError("PLAN_CONTEXT_CHANGED", "配餐依据已变化，未发布旧餐单。", 409)
                blocked, _, _ = meal_restrictions(facts, original)
                selection = MealPlanSelection(items=[{key: item[key] for key in ("food_id", "lower", "upper")} for item in plan["items"]], chunk_ids=list(REQUIRED))
                self.plans.validate(selection, blocked, quick=True)
                if plan.get("joint_refit"):
                    if not {item["food_id"] for item in previous["items"]} <= {item["food_id"] for item in plan["items"]} and plan.get("required_groups"):
                        raise ModelError("PLAN_CONTEXT_CHANGED", "联合配量不能移除本次要求保留的食材。", 409)
                else:
                    check_kept_items(plan["items"], plan.get("fit_kept_items", []), plan.get("required_groups"))
                check_portion_limits(plan["items"], plan.get("portion_limits", []))
                if plan["id"] not in mutable:
                    continue
                payload = json.loads(row["payload"])
                payload.pop("candidate_ready", None)
                payload["items"] = plan["items"]
                if plan.get("joint_refit"):
                    kept_ids = {item["food_id"] for item in payload.get("fit_kept_items", [])}
                    payload["fit_kept_items"] = [item for item in plan["items"] if item["food_id"] in kept_ids]
                    payload["kept_items"] = payload["fit_kept_items"]
                    if payload.get("required_groups"):
                        payload["required_groups"] = dict(Counter(item["group"] for item in plan["items"]))
                old = plan["quick_nutrition"]
                note = old["allocation_note"] + "所选餐次联合配量；三项宏量完整估算区间已核对，能量中点按每日目标正负5%工程容差核对。"
                if plan.get("joint_refit") and previous.get("required_groups"):
                    note += "为同时满足营养范围，本次修改餐的保留食材可能重新配量或补充食材，未修改餐次保持。"
                payload["quick_nutrition"] = nutrition_result(plan["items"], old["portion_reference"], note, info, reference)
                payload["quick_nutrition"]["balance_policy"] = meal_balance.POLICY
                connection.execute("UPDATE meal_plans SET status='draft',payload=? WHERE id=? AND user_id=?", (encode(payload), plan["id"], self.user_id))
            response["quick_request"]["status"] = "ready"
            response["quick_check"] = checked
            response["text"] = "以下餐单已按已记录摄入核对三项营养范围。仍为参考估算，未计入已吃记录。"
            self.save(connection, conversation_id, version, response)

    def generate(self, conversation_id, body, factory, model_name, consent):
        consent()
        data = self.coach.get(conversation_id)
        if data["meal_question"] or data["action"] != "meal":
            raise ModelError("MEAL_SCOPE_REQUIRED", data["meal_question"] or "请先核对这次饮食请求。", 409)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            info = current_context(connection, self.user_id, data["day"])
            require_ready(info)
            meal_balance.precheck(info)
        request_id = str(body.client_id)
        context = data["meal_context"]
        schedule = context.get("meal_types") or [context.get("meal_type") or "dinner"]
        available = self.candidates(conversation_id, data["day"])
        cached = all(any(plan["coach_id"] == str(conversation_id) and plan["coach_version"] == context.get("meal_versions", {}).get(meal, body.version)
                         and plan["meal_type"] == meal and plan.get("quick_nutrition", {}).get("balance_policy") == meal_balance.POLICY and not plan["stale"] for plan in available) for meal in schedule)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            response = self.row(connection, conversation_id, body.version)
            previous = response.get("quick_request", {})
            if previous.get("status") == "ready" and cached:
                return
            if previous.get("status") == "generating" and (datetime.now(timezone.utc) - datetime.fromisoformat(previous["started_at"])).total_seconds() < 180:
                raise ModelError("MEAL_REQUEST_PENDING", "这次餐单仍在生成，请稍后刷新；不会重复请求模型。", 409)
            if previous.get("client_id") == request_id:
                raise ModelError("MEAL_REQUEST_FAILED", previous.get("error") or "这次生成未完成，请明确重试。", 409)
            response["quick_request"] = {"client_id": request_id, "status": "generating", "started_at": datetime.now(timezone.utc).isoformat()}
            response["text"] = "正在生成餐单，尚未完成。"
            self.save(connection, conversation_id, body.version, response)

        def guard():
            consent()
            with self.database.connect() as connection:
                current = self.row(connection, conversation_id, body.version).get("quick_request", {})
                if current.get("client_id") != request_id or current.get("status") != "generating":
                    raise ModelError("COACH_CONTEXT_CHANGED", "当前生成已被新请求取代，迟到结果不会覆盖。", 409)

        try:
            selected = []
            for meal in schedule:
                guard()
                version = context.get("meal_versions", {}).get(meal, body.version)
                existing = next((plan for plan in self.candidates(conversation_id, data["day"]) if plan["coach_id"] == str(conversation_id)
                                 and plan["coach_version"] == version and plan["meal_type"] == meal
                                 and plan.get("quick_nutrition") and not plan["stale"]), None)
                if existing:
                    selected.append(existing)
                    continue
                request = MealPlanRequest(client_id=uuid5(UUID(request_id), meal), day=data["day"], meal_type=meal,
                                          coach_id=conversation_id, coach_version=version, quick_recommendation=True)
                selected.append(self.plans.generate(request, factory, model_name, guard=guard, defer_publish=True))
            guard()
            self.publish(selected, conversation_id, body.version, request_id, info, guard)
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    response = self.row(connection, conversation_id, body.version)
                except Exception:
                    response = {}
                if response.get("quick_request", {}).get("client_id") == request_id:
                    response["quick_request"].update(status="failed", error=str(error) if isinstance(error, ModelError) else "生成未完成，已有餐单保留；可明确重试。")
                    response["text"] = "本次餐单未完整生成。" + response["quick_request"]["error"]
                    self.save(connection, conversation_id, body.version, response)
            raise
