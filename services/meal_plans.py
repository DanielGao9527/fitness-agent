import hashlib
import json
from collections import Counter
from datetime import date, datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from rag.rag_service import KnowledgeUnavailable
from schemas import MealPlanSelection
from services.profile_context import read_profile
from services.plan_foods import ANIMAL_FOODS, CATALOG_VERSION, FOODS, QUICK_MAX, ON_REQUEST_FOODS, catalog, exclusions
from services.coach import plan_binding, require_plan_binding, meal_context
from services.meal_intake import planning_context, require_planning_context, model_reference
from services import meal_nutrient_reference as nutrient_reference

REQUIRED = ("phe-eatwell:balance", "phe-eatwell:starch", "phe-eatwell:protein", "phe-eatwell:vegetables",
            "fda-allergy-label:ingredients", "fda-allergy-label:cross-contact")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def meal_restrictions(facts, original):
    context = facts.get("coach", {}).get("meal_context", {})
    plant_only = bool(original.get("plant_only") or context.get("plant_only"))
    temporary = set(original.get("excluded_foods", [])) | set(context.get("excluded_foods", []))
    temporary.update(facts.get("coach", {}).get("constraints", {}).get("food_avoid", []))
    blocked = exclusions(facts["profile"], temporary, plant_only)
    choices = dict(context.get("group_choices", {}))
    if context.get("protein_choice"):
        choices.setdefault("protein", [context["protein_choice"]])
    # A broad request such as 'meat' must not reintroduce rare foods automatically.
    requested = {keys[0] for keys in choices.values() if len(keys) == 1}
    blocked = sorted(set(blocked) | (ON_REQUEST_FOODS - requested))
    for group, choices_in_group in choices.items():
        if not set(choices_in_group) - set(blocked):
            raise ModelError("PLAN_OPTIONS_INSUFFICIENT", "想换的食材与档案禁忌或本次偏好冲突，没有可用候选；不会用过敏食材替换。", 409)
        blocked = sorted(set(blocked) | {key for key, food in FOODS.items() if food[1] == group and key not in choices_in_group})
    if any(not any(food[1] == group and key not in blocked for key, food in FOODS.items())
           for group in ("starch", "protein", "vegetable")):
        raise ModelError("PLAN_OPTIONS_INSUFFICIENT", "当前常见食材无法满足这些限制，暂不生成；不会自动改用少见或禁忌食材。", 409)
    return blocked, plant_only, context


def check_kept_items(items, kept, groups=None):
    actual = {item["food_id"]: item for item in items}
    if any(actual.get(item["food_id"]) != item for item in kept):
        raise ModelError("MODEL_INVALID_OUTPUT", "替换时改变了无需调整的食材或份量，未接受这次结果。原建议保留。")
    if groups and Counter(item["group"] for item in items) != groups:
        raise ModelError("MODEL_INVALID_OUTPUT", "替换时遗漏了原餐单的食材类别或数量，未接受这次结果。")


def check_portion_limits(items, limits):
    actual = {item["food_id"]: item for item in items}
    for limit in limits:
        item = actual.get(limit["food_id"])
        if not item or item["lower"] > limit["max_lower"] or item["upper"] > limit["max_upper"]:
            raise ModelError("MODEL_INVALID_OUTPUT", "未按要求减少指定食材的份量，未接受这次结果。原餐单保留。")


class MealPlanService:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id, self.retriever = database, user_id, retriever

    def evidence(self):
        found = {hit.chunk_id: hit.model_dump(mode="json") for hit in
                 self.retriever.get_chunks(REQUIRED, topic="nutrition")}
        if any(key not in found for key in REQUIRED):
            raise ModelError("PLAN_EVIDENCE_MISSING", "单餐搭配所需的资料缺失、已撤回或待复核，暂不生成或采纳新建议。", 503)
        return [found[key] for key in REQUIRED]

    def snapshot(self, connection, day, meal_type, sources, original=None):
        profile = read_profile(connection, self.user_id)
        facts = {"profile": profile}
        binding = plan_binding(connection, self.user_id, original, day, "meal")
        if binding is not None:
            facts["coach"] = binding
        for table in ("meals", "workouts"):
            rows = connection.execute(f"SELECT id,payload FROM {table} WHERE user_id=? AND day=? ORDER BY id",
                                      (self.user_id, day)).fetchall()
            facts[table] = [{"id": row["id"], **json.loads(row["payload"])} for row in rows]
        if original and original.get("quick_recommendation"):
            facts["nutrient_context"] = nutrient_reference.context(connection, self.user_id, day, profile, facts["meals"])
            full_context = meal_context(connection, str(original["coach_id"])) if binding and binding["valid"] else {}
            facts["requested_meals"] = full_context.get("meal_types") or [meal_type]
        if original and original.get("intake_context_hash"):
            facts["intake_context"] = planning_context(connection, self.user_id, date.fromisoformat(day))
            context = meal_context(connection, str(original["coach_id"])) if binding and binding["valid"] else {}
            facts["intake_meals"] = context.get("meal_types") or [meal_type]
        version = connection.execute("SELECT COALESCE(MAX(version),0) FROM meal_plans WHERE user_id=? AND day=? AND meal_type=?",
                                     (self.user_id, day, meal_type)).fetchone()[0]
        fingerprint = digest({"facts": facts,
                              "sources": sources, "policy": CATALOG_VERSION, "foods": catalog(quick=bool(original and original.get("quick_recommendation")))})
        return facts, fingerprint, version

    def view(self, row, fingerprint, latest):
        payload = json.loads(row["payload"])
        original = json.loads(row["input_payload"])
        stale = row["context_hash"] != fingerprint
        if row["status"] == "draft" or (row["status"] == "generating" and payload.get("candidate_ready")):
            stale = stale or payload.get("base_version") != latest
        return {"id": row["id"], "day": row["day"], "meal_type": row["meal_type"],
                "coach_id": original.get("coach_id"), "coach_version": original.get("coach_version"),
                "status": row["status"], "version": row["version"], "stale": stale,
                "current": row["status"] == "accepted" and row["version"] == latest and not stale,
                "created_at": row["created_at"], "accepted_at": row["accepted_at"], **payload}

    def list(self, day):
        try:
            sources = self.evidence()
        except (KnowledgeUnavailable, ModelError):
            sources = None
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("WITH owned AS (SELECT rowid AS sequence,* FROM meal_plans WHERE user_id=? AND day=? AND status IN ('draft','accepted')) "
                                      "SELECT * FROM owned WHERE sequence IN (SELECT sequence FROM owned ORDER BY sequence DESC LIMIT 20) "
                                      "OR sequence IN (SELECT MAX(sequence) FROM owned GROUP BY json_extract(input_payload,'$.coach_id'),meal_type) ORDER BY sequence DESC",
                                      (self.user_id, day)).fetchall()
            result = []
            for row in rows:
                try:
                    _, fingerprint, version = self.snapshot(connection, day, row["meal_type"], sources, json.loads(row["input_payload"]))
                except ModelError:
                    fingerprint, version = None, row["version"]
                result.append(self.view(row, fingerprint if sources else None, version))
            return result

    def generate(self, body, model_factory, model_name, guard=None, *, defer_publish=False):
        if guard:
            guard()
        day, meal_type = body.day.isoformat(), body.meal_type
        original = body.model_dump(mode="json")
        if not body.quick_recommendation:
            original.pop("quick_recommendation", None)
        if original.get("intake_context_hash") is None:
            original.pop("intake_context_hash", None)
        input_payload = encode(original)
        sources = self.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts, fingerprint, version = self.snapshot(connection, day, meal_type, sources, body.model_dump(mode="json"))
            require_plan_binding(facts)
            existing = connection.execute("SELECT * FROM meal_plans WHERE user_id=? AND client_id=?",
                                          (self.user_id, str(body.client_id))).fetchone()
            if existing:
                if existing["input_payload"] != input_payload:
                    raise ModelError("PLAN_REQUEST_CONFLICT", "这次请求的内容已改变，请重新生成。", 409)
                if existing["status"] in ("draft", "accepted"):
                    return self.view(existing, fingerprint, version)
                raise ModelError("PLAN_REQUEST_PENDING", "这次请求正在处理或未完成。可刷新查看结果；明确重新生成会发起新的模型请求。", 409)
            if body.intake_context_hash:
                require_planning_context(facts["intake_context"], body.intake_context_hash)
            blocked, plant_only, context = meal_restrictions(facts, body.model_dump(mode="json"))
            kept, groups, portion_limits = [], None, []
            previous_budget = None
            adjustments = context.get("adjustments", [context["adjustment"]] if context.get("adjustment") else [])
            if (adjustments or context.get("batch_commands")) and not context.get("base_plan_id"):
                raise ModelError("PLAN_BASE_REQUIRED", "还没有可调整的餐单，请先安排下一餐，再指定替换或减少份量。", 409)
            if context.get("base_plan_id"):
                parent = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=? AND day=? AND meal_type=? AND status IN ('draft','accepted')",
                                            (context["base_plan_id"], self.user_id, day, meal_type)).fetchone()
                if not parent or json.loads(parent["input_payload"]).get("coach_id") != str(body.coach_id):
                    raise ModelError("PLAN_CONTEXT_CHANGED", "待调整餐单不属于当前对话或餐次，请核对后重新安排。", 409)
                parent_payload = json.loads(parent["payload"])
                previous_items = parent_payload["items"]
                previous_budget = parent_payload.get("quick_nutrition", {}).get("portion_reference")
                groups = dict(Counter(item["group"] for item in previous_items))
                changed = []
                for adjustment in adjustments:
                    targets = [item for item in previous_items if item["food_id"] in adjustment.get("source_foods", [])]
                    if not targets:
                        raise ModelError("PLAN_ITEM_NOT_FOUND", "当前餐单没有你指定的食材，请按餐单中的食材名称调整；本次修改未部分执行。", 409)
                    changed.extend(targets)
                    if adjustment.get("kind") == "reduce":
                        for item in targets:
                            if item["food_id"] in blocked:
                                raise ModelError("PLAN_ADJUSTMENT_CONFLICT", "要求减量的食材同时被限制排除，本次修改未执行，请先核对要求。", 409)
                            if item["upper"] <= FOODS[item["food_id"]][4]:
                                raise ModelError("PLAN_PORTION_LIMIT", "这项份量已到当前可核对范围的下限，不能继续自动减量；可以改选其他同类食材。", 409)
                            portion_limits.append({"food_id": item["food_id"], "max_lower": item["lower"], "max_upper": item["upper"] - 1})
                # Only current conversational changes may alter the original selection.
                replaceable = set(context.get("excluded_foods", []))
                replaceable.update(facts.get("coach", {}).get("constraints", {}).get("food_avoid", []))
                replaceable.update(item["food_id"] for item in changed)
                if context.get("plant_only"):
                    replaceable.update(ANIMAL_FOODS)
                if context.get("protein_choice"):
                    replaceable.update(key for key, food in FOODS.items() if food[1] == "protein" and key != context["protein_choice"])
                for group, choices in context.get("group_choices", {}).items():
                    replaceable.update(key for key, food in FOODS.items() if food[1] == group and key not in choices)
                    groups.setdefault(group, 1)
                if sum(groups.values()) > 7:
                    raise ModelError("PLAN_ITEM_LIMIT", "这餐已有七项食材，继续添加会超过当前餐单范围。可以替换已有食材，或重新安排这一餐；原建议保留。", 409)
                kept = [item for item in previous_items if item["food_id"] not in replaceable]
                if any(item["food_id"] in blocked for item in kept):
                    raise ModelError("PLAN_CONTEXT_CHANGED", "档案中的新限制也影响原餐单其他食材，请重新发送“下一餐吃什么”核对整餐。", 409)
                if any(sum(food[1] == group and key not in blocked for key, food in FOODS.items()) < count for group, count in groups.items()):
                    raise ModelError("PLAN_OPTIONS_INSUFFICIENT", "现有食材不足以保留原餐单结构并完成替换，暂不生成；不会用禁忌食材补足。", 409)
            if len(facts["meals"]) + len(facts["workouts"]) > 60:
                raise ModelError("PLAN_CONTEXT_TOO_LARGE", "当日记录过多，当前版本无法完整处理，未截断记录生成建议。", 422)
            plan_id = str(uuid4())
            other_meals = []
            if body.quick_recommendation and body.coach_id:
                seen = set()
                for prior in connection.execute("SELECT meal_type,payload FROM meal_plans WHERE user_id=? AND day=? "
                                                "AND json_extract(input_payload,'$.coach_id')=? AND (status IN ('draft','accepted') OR "
                                                "(status='generating' AND json_extract(payload,'$.candidate_ready')=1)) "
                                                "AND meal_type<>? ORDER BY rowid DESC", (self.user_id, day, str(body.coach_id), meal_type)):
                    if prior["meal_type"] not in seen:
                        seen.add(prior["meal_type"])
                        other_meals.append({"meal_type": prior["meal_type"], "items": json.loads(prior["payload"])["items"]})
            connection.execute("INSERT INTO meal_plans(id,user_id,client_id,day,meal_type,input_payload,context_hash,status) VALUES (?,?,?,?,?,?,?,'generating')",
                               (plan_id, self.user_id, str(body.client_id), day, meal_type, input_payload, fingerprint))
        try:
            # Only necessary private fields leave this authenticated backend.
            profile_keys = ("goal", "height_cm", "weight_kg", "preferences", "food_allergies")
            meal_keys = ("meal_type", "name", "grams", "amount_description")
            message = {"day": day, "meal_type": meal_type,
                       "profile": {key: facts["profile"][key] for key in profile_keys},
                       "eaten": [{key: meal.get(key) for key in meal_keys} for meal in facts["meals"]],
                       "completed_activity_minutes": sum(row["minutes"] for row in facts["workouts"] if row["status"] == "completed"),
                       "allowed_foods": [food for food in catalog(quick=body.quick_recommendation) if food["id"] not in blocked],
                       "food_style": "以中国日常饮食中易购买的食材搭配，优先米饭、面、薯类、常见肉蛋豆菜。只用allowed_foods，明确点名的食材优先。",
                       "evidence": [{key: hit[key] for key in ("chunk_id", "title", "excerpt", "scope")} for hit in sources]}
            preferences = facts.get("coach", {}).get("constraints", {}).get("preferences", [])
            if body.intake_context_hash:
                message["intake_reference"] = model_reference(facts["intake_context"], facts["intake_meals"])
            if preferences:
                message["session_preferences"] = preferences
            if kept:
                message["keep_items"] = [{key: item[key] for key in ("food_id", "lower", "upper")} for item in kept]
            if groups:
                message["required_groups"] = groups
            message["requested_groups"] = sorted(context.get("group_choices", {}))
            if portion_limits:
                message["portion_limits"] = portion_limits
            prompt = (ROOT / "prompts/next_meal.md").read_text(encoding="utf-8")
            reference, budget, allocation_note = None, None, ""
            if body.quick_recommendation:
                reference = nutrient_reference.reference_data()
                budget, allocation_note = nutrient_reference.meal_budget(facts["nutrient_context"], meal_type, facts["requested_meals"])
                message["portion_reference"] = budget
                nutrient_context = facts["nutrient_context"]
                message["daily_macro_reference"] = nutrient_context["macro"]
                message["recent_food_frequency"] = nutrient_context["food_history"]["counts"]
                message["nutrition_per_100g"] = {key: {name: value[name] for name in nutrient_reference.NAMES}
                                                for key, value in reference["foods"].items() if key not in blocked}
                message["other_suggested_meals"] = other_meals
                prompt += "\n" + (ROOT / "prompts/quick_meal.md").read_text(encoding="utf-8")
            prompt += "\nJSON Schema:\n" + json.dumps(MealPlanSelection.model_json_schema(), ensure_ascii=False)
            model = model_factory()
            raw = model.generate(system_prompt=prompt, message=encode(message))
            try:
                selection = MealPlanSelection.model_validate_json(raw)
            except ValidationError:
                raise ModelError("MODEL_INVALID_OUTPUT", "建议格式未通过核对，未显示或采纳。") from None
            items = self.validate(selection, blocked, quick=body.quick_recommendation)
            if not set(context.get("group_choices", {})) <= {item["group"] for item in items}:
                raise ModelError("MODEL_INVALID_OUTPUT", "建议遗漏了你指定的食材类别，原建议保留。")
            check_kept_items(items, kept, groups)
            check_portion_limits(items, portion_limits)
            if body.quick_recommendation:
                # Preserve identities on a swap, but stale calorie budgets must not freeze old quantities.
                rounded_budget = {key: round(value, 1) for key, value in budget.items()}
                fixed = kept if previous_budget == rounded_budget else []
                if kept and not fixed:
                    allocation_note += "目标或摄入配额已变化，保留未改食材但重新核算份量。"
                items = nutrient_reference.fit_portions(items, budget, fixed, portion_limits, reference)
                fitted = MealPlanSelection(items=[{key: item[key] for key in ("food_id", "lower", "upper")} for item in items], chunk_ids=list(REQUIRED))
                items = self.validate(fitted, blocked, quick=True)
                check_kept_items(items, fixed, groups)
                check_portion_limits(items, portion_limits)
            if guard:
                guard()
            current_sources = self.evidence()
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if guard:
                    guard()
                current, current_hash, latest = self.snapshot(connection, day, meal_type, current_sources, body.model_dump(mode="json"))
                if current_hash != fingerprint or latest != version:
                    raise ModelError("PLAN_CONTEXT_CHANGED", "生成期间档案、当天记录、已采纳版本或依据发生变化，请重新核对生成。", 409)
                meal_restrictions(current, body.model_dump(mode="json"))
                payload = {"items": items, "sources": sources, "base_version": version,
                           "requested_changes": context.get("batch_commands", []),
                           "excluded_foods": blocked, "plant_only": plant_only,
                           "kept_items": kept, "required_groups": groups, "portion_limits": portion_limits, "replaces_plan_id": context.get("base_plan_id"),
                           "meal_count": len(facts["meals"]), "completed_minutes": message["completed_activity_minutes"],
                           "model": model_name, "generated_at": datetime.now(timezone.utc).isoformat(),
                           "portion_basis": "模型估算范围；不是称重结果，也不是资料规定的个人用量。未计算全天热量缺口，不含额外用油和调味料。"}
                if body.intake_context_hash:
                    payload["intake_reference"] = message["intake_reference"]
                    payload["intake_confirmed_at"] = facts["intake_context"]["confirmed_at"]
                if body.quick_recommendation:
                    payload["quick_nutrition"] = nutrient_reference.nutrition_result(items, budget, allocation_note, facts["nutrient_context"], reference)
                    payload["fit_kept_items"] = fixed
                    payload["requested_groups"] = sorted(context.get("group_choices", {}))
                    payload["portion_basis"] = "食材成分表参考估算，程序按条件式营养参考调整份量；不是实测或达标认证。仅计列出的食材和用油。"
                if defer_publish:
                    payload["candidate_ready"] = True
                connection.execute("UPDATE meal_plans SET status=?,payload=? WHERE id=? AND user_id=?",
                                   ("generating" if defer_publish else "draft", encode(payload), plan_id, self.user_id))
                row = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)).fetchone()
                return self.view(row, current_hash, latest)
        except Exception:
            with self.database.connect() as connection:
                connection.execute("UPDATE meal_plans SET status='failed' WHERE id=? AND user_id=? AND status='generating'", (plan_id, self.user_id))
            raise

    @staticmethod
    def validate(selection, blocked, *, quick=False):
        keys = [item.food_id for item in selection.items]
        if len(set(keys)) != len(keys) or any(key not in FOODS or key in blocked for key in keys):
            raise ModelError("PLAN_UNSAFE_OUTPUT", "建议包含禁忌、重复或成分不明的食材，已停止展示。")
        if set(selection.chunk_ids) != set(REQUIRED):
            raise ModelError("MODEL_INVALID_OUTPUT", "建议的搭配依据不完整或引用不匹配，已停止展示。")
        groups = Counter(FOODS[key][1] for key in keys)
        if groups["starch"] not in ((1, 2) if quick else (1,)) or groups["protein"] != 1 or groups["vegetable"] not in (1, 2) or groups["fat"] > (2 if quick else 1) or any(groups[key] > 1 for key in ("fruit", "dairy")):
            raise ModelError("MODEL_INVALID_OUTPUT", "建议的食材组成不完整，已停止展示。")
        items = []
        for item in selection.items:
            name, group, unit, basis, minimum, maximum = FOODS[item.food_id]
            if quick:
                maximum = QUICK_MAX.get(item.food_id, maximum)
            if not minimum <= item.lower <= item.upper <= maximum:
                raise ModelError("MODEL_INVALID_OUTPUT", "份量超出当前可核对范围，已停止展示。")
            items.append({**item.model_dump(), "name": name, "group": group, "unit": unit, "basis": basis})
        if sum(item["upper"] for item in items if item["group"] == "vegetable") > 400:
            raise ModelError("MODEL_INVALID_OUTPUT", "蔬菜总份量超出当前可核对范围，已停止展示。")
        return items

    def accept(self, plan_id):
        sources = self.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)).fetchone()
            if not row:
                raise ModelError("PLAN_NOT_FOUND", "建议不存在或无权访问。", 404)
            facts, fingerprint, latest = self.snapshot(connection, row["day"], row["meal_type"], sources, json.loads(row["input_payload"]))
            if row["status"] == "accepted":
                return self.view(row, fingerprint, latest)
            view = self.view(row, fingerprint, latest)
            if row["status"] != "draft" or view["stale"]:
                raise ModelError("PLAN_CONTEXT_CHANGED", "这份草稿已失效，请根据最新档案、记录和依据重新生成。", 409)
            original = json.loads(row["input_payload"])
            if original.get("intake_context_hash"):
                require_planning_context(facts["intake_context"], original["intake_context_hash"])
            blocked, _, _ = meal_restrictions(facts, original)
            selected = MealPlanSelection(items=[{key: item[key] for key in ("food_id", "lower", "upper")} for item in view["items"]], chunk_ids=list(REQUIRED))
            items = self.validate(selected, blocked, quick=bool(original.get("quick_recommendation")))
            check_kept_items(items, view.get("kept_items", []), view.get("required_groups"))
            check_portion_limits(items, view.get("portion_limits", []))
            connection.execute("UPDATE meal_plans SET status='accepted',version=?,accepted_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                               (latest + 1, plan_id, self.user_id))
            row = connection.execute("SELECT * FROM meal_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)).fetchone()
            return self.view(row, fingerprint, latest + 1)
