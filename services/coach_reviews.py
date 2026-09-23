"""Model intent drafts never execute a plan or silently discard pending notes."""
import json
import re
from uuid import uuid4

from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from schemas import CoachUnderstanding
from services.coach import CoachService, apply_meal_command, apply_meal_commands, apply_meal_changes, validate_meal_changes, batch_meal_adjustments, conversation_constraints, meal_adjustment, meal_context, route
from services.meal_plans import digest, encode
from services.plan_foods import ALIASES, CATALOG_VERSION, FOODS, MEDICAL, SOFT
from services.training_plans import HEALTH
from services.training_context import training_context, recent_training, merge_training, training_question, SPLIT_ALIASES
from services.meal_schedule import MEAL_ALIASES, selection, targeted, question as meal_question

POLICY = "coach-understanding-v10-profile-budget"
QUESTIONS = {
    "none": "",
    "which_food": "你想调整哪一种食材？请写出餐单里的名称。",
    "which_meal": "要安排或修改哪一餐？请明确早餐、午餐或晚餐，以及各餐分别要修改什么。",
    "which_request": "你想安排下一餐，还是调整训练？请补充具体想做的事。",
    "which_training": "请明确要采用的训练结构或要替换的动作；尚未应用本次修改，原有条件仍保留。",
    "multiple_requests": "这次包含多个操作，请先说明要优先调整哪一项。其他限制仍会保留。",
    "conflicting_changes": "同一食材类别出现多个修改，请说明最终要哪一个；其他原话和限制仍保留。",
    "unhandled_constraint": "仍有无法完整核对的限制，请补充具体食材或注意情况；不要删除真实限制来绕过。",
}
ALLERGY = re.compile(r"过敏|不耐受|忌口|禁忌|不能吃|allerg|intoleran", re.I)


def training_update(update, scope, facts):
    from services.training_catalog import read_catalog
    from services.training_recommendations import denied_equipment
    from services.workouts import duration_values

    changes = {key: value for key, value in update.model_dump().items() if key not in ("replace_from", "replace_with")}
    changes["kind"] = scope
    if update.minutes is not None:
        values = duration_values(update.source_text)
        if len(values) != 1 or values[0] != update.minutes:
            raise ModelError("COACH_INVALID_OUTPUT", "理解的可用时长与原话不一致或混合了多个时长，未应用；请明确本次还能练多少分钟。")
    if scope == "aerobic" and any((update.split, update.weekly, update.replace_from, update.replace_with)):
        raise ModelError("COACH_INVALID_OUTPUT", "有氧请求不能混入力量结构或动作替换，未应用。")
    if update.split and not any(alias in update.source_text and value == update.split for alias, value in SPLIT_ALIASES.items()):
        raise ModelError("COACH_INVALID_OUTPUT", "训练结构无法对应原话，未应用。")
    if update.weekly is False or update.weekly and not update.split and not re.search(r"本周|这周|一周|七天|7天", update.source_text):
        raise ModelError("COACH_INVALID_OUTPUT", "周安排无法对应原话，未应用。")
    if update.split or update.weekly:
        changes.update(weekly=True, clear_focus=not bool(update.focus), clear_replacements=True)
    originals = [turn["message"] for turn in facts["messages"] if update.source_text in turn["message"]]
    denied = set().union(*(denied_equipment(text) for text in originals))
    if denied and not denied <= denied_equipment(update.equipment or ""):
        raise ModelError("COACH_INVALID_OUTPUT", "理解遗漏了原话中不可用的器械，未应用；请保留完整器械条件重新核对。")
    if update.activity == "swim" and not re.search(r"游泳|泳池|游一会|游一会儿", update.source_text):
        raise ModelError("COACH_INVALID_OUTPUT", "游泳项目无法对应原话，未应用。")
    if update.replace_from or update.replace_with:
        if not all(value and value in update.source_text for value in (update.replace_from, update.replace_with)):
            raise ModelError("COACH_INVALID_OUTPUT", "动作替换必须同时对应原话中的原动作和目标动作，未应用。")
        items, _ = read_catalog()
        names = {item["name"]: item for item in items}
        old, new = names.get(update.replace_from), names.get(update.replace_with)
        current = {item["id"] for item in facts.get("current_training", [])}
        if not old or not new or old["id"] not in current or old["pattern"] != new["pattern"]:
            raise ModelError("COACH_INVALID_OUTPUT", "只能替换当前建议中的已知同模式动作，未应用。")
        if update.focus or update.split or update.weekly:
            raise ModelError("COACH_INVALID_OUTPUT", "切换训练结构或部位后，请先核对新动作再替换，未应用本次组合修改。")
        changes["replacements"] = {old["id"]: new["id"]}
    return changes


class CoachReviewService:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id
        self.coach = CoachService(database, user_id)

    def snapshot(self, connection, conversation_id):
        row = self.coach.owned(connection, conversation_id)
        resolved = connection.execute("SELECT COALESCE(MAX(base_version),0) FROM coach_reviews WHERE conversation_id=? AND status='confirmed'", (row["id"],)).fetchone()[0]
        turns = connection.execute(
            "SELECT version,message FROM coach_turns WHERE conversation_id=? AND version>? "
            "AND json_extract(response,'$.status')='needs_review' ORDER BY version", (row["id"], resolved),
        ).fetchall()
        profile = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (self.user_id,)).fetchone()
        profile = json.loads(profile[0]) if profile else {}
        previous = connection.execute(
            "SELECT id,payload FROM meal_plans WHERE user_id=? AND day=? AND status IN ('draft','accepted') "
            "AND json_extract(input_payload,'$.coach_id')=? ORDER BY rowid DESC LIMIT 1", (self.user_id, row["day"], row["id"]),
        ).fetchone()
        facts = {"version": row["version"], "day": row["day"], "pending": bool(row["pending"]),
                 "messages": [dict(turn) for turn in turns],
                 "profile": {key: profile.get(key, "") for key in ("preferences", "food_allergies")},
                 "constraints": conversation_constraints(connection, row["id"]),
                 "meal_context": meal_context(connection, row["id"]),
                 "current_meal": json.loads(previous["payload"])["items"] if previous else [],
                 "plan_id": previous["id"] if previous else None}
        facts["training_context"] = training_context(connection, row["id"])
        if "meal_types" in facts["meal_context"]:
            rows = connection.execute("SELECT id,meal_type,payload FROM meal_plans WHERE user_id=? AND day=? AND status IN ('draft','accepted') AND json_extract(input_payload,'$.coach_id')=? ORDER BY rowid DESC", (self.user_id, row["day"], row["id"])).fetchall()
            facts["current_meals"] = {}
            for previous in rows:
                facts["current_meals"].setdefault(previous["meal_type"], {"id": previous["id"], "items": json.loads(previous["payload"])["items"]})
        # Only training-related interpretation receives exercise details, never meal-only requests.
        if row["intent"] == "training" or facts["training_context"] and row["intent"] == "unknown" or any(re.search(r"训练|练|分化|卧推|游泳|泳池|哑铃|杠铃|龙门架|有氧|骑|步行|走路|器械|分钟|胸|肩|二头|三头", turn["message"]) for turn in turns):
            context = self.coach.context(connection, row["day"])
            facts["training_facts"] = {"profile": {key: context["profile"][key] for key in ("goal", "experience", "equipment", "minutes_per_session", "training_split")},
                                       "history": recent_training(connection, self.user_id, row["day"])}
            previous = connection.execute("SELECT json_extract(response,'$.training_recommendation') FROM coach_turns "
                "WHERE conversation_id=? AND json_extract(response,'$.training_recommendation') IS NOT NULL ORDER BY version DESC LIMIT 1", (row["id"],)).fetchone()
            recommendation = json.loads(previous[0]) if previous else {}
            facts["current_training"] = [{"id": item["id"], "name": item["name"],
                "alternatives": [{"id": other["id"], "name": other["name"]} for other in item.get("alternatives", [])]}
                for item in recommendation.get("exercises", [])]
        return row, facts, digest({"policy": POLICY, "foods": CATALOG_VERSION, "facts": facts})

    @staticmethod
    def view(review, fingerprint, version):
        return {"id": review["id"], "base_version": review["base_version"], "status": review["status"],
                "stale": review["base_version"] != version or (review["status"] != "confirmed" and review["context_hash"] != fingerprint),
                **json.loads(review["payload"])}

    def latest(self, conversation_id):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            row, _, fingerprint = self.snapshot(connection, conversation_id)
            review = connection.execute("SELECT * FROM coach_reviews WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
            return self.view(review, fingerprint, row["version"]) if review else None

    @staticmethod
    def validate(result, facts):
        originals = {turn["version"]: turn["message"] for turn in facts["messages"]}
        versions = [note.version for note in result.notes]
        if len(set(versions)) != len(versions) or set(versions) != set(originals):
            raise ModelError("COACH_INVALID_OUTPUT", "理解结果遗漏或重复了待核对原话，未应用。")
        notes = []
        for note in result.notes:
            original = originals[note.version]
            if any(alias not in ALIASES or alias not in original for alias in note.avoid) or any(value not in SOFT for value in note.preferences):
                raise ModelError("COACH_INVALID_OUTPUT", "理解结果含有无法对应原话的食材或未支持的偏好，未应用。")
            blocked = set().union(*(ALIASES[alias] for alias in note.avoid)) if note.avoid else set()
            unresolved = note.unresolved
            # Conservative keyword floors supplement, not replace, semantic review.
            for clause in re.split(r"[，,。；;！!？?\n]", original):
                if ALLERGY.search(clause):
                    aliases = re.findall("|".join(sorted(ALIASES, key=len, reverse=True)), clause)
                    if aliases:
                        blocked.update(set().union(*(ALIASES[alias] for alias in aliases)))
                    else:
                        unresolved = True
            notes.append({"version": note.version, "message": original, "food_avoid": sorted(blocked),
                          "avoid_names": [FOODS[key][0] for key in sorted(blocked)],
                          "preferences": sorted(set(note.preferences)),
                          "training_caution": note.training_caution or bool(HEALTH.search(original)),
                          "diet_caution": note.diet_caution or bool(MEDICAL.search(original)), "unresolved": unresolved})
        clarification = result.clarification
        meal_changes = [change.model_dump() for change in result.meal_changes]
        if meal_changes:
            if result.scope != "meal" or result.command or result.commands or result.training is not None:
                raise ModelError("COACH_INVALID_OUTPUT", "跨餐修改不能混入其他操作，未应用。")
            for change in meal_changes:
                source = change["source_text"]
                named = {key for alias, key in MEAL_ALIASES.items() if alias in source}
                if named != {change["meal_type"]} or not any(source in message for message in originals.values()):
                    raise ModelError("COACH_INVALID_OUTPUT", "各餐修改无法对应带餐次的原话，未应用。")
            try:
                validate_meal_changes(meal_changes)
            except ModelError as error:
                if error.code != "COACH_CHANGES_CONFLICT":
                    raise
                clarification = "conflicting_changes"
        named_target, _ = targeted(result.command)
        declared = selection(result.command) or ([named_target] if named_target else [])
        meal_types = result.meal_types or ([change["meal_type"] for change in meal_changes] if meal_changes else declared)
        if meal_changes and set(meal_types) != {change["meal_type"] for change in meal_changes}:
            raise ModelError("COACH_INVALID_OUTPUT", "餐次与跨餐修改清单不一致，未应用。")
        if result.meal_types and declared and set(declared) != set(result.meal_types):
            raise ModelError("COACH_INVALID_OUTPUT", "命令与餐次清单不一致，未应用。")
        if len(set(meal_types)) != len(meal_types) or meal_types and result.scope != "meal":
            raise ModelError("COACH_INVALID_OUTPUT", "餐次重复或与请求类型不符，未应用。")
        for meal in meal_types:
            if not any(alias in message for message in originals.values() for alias, key in MEAL_ALIASES.items() if key == meal):
                raise ModelError("COACH_INVALID_OUTPUT", "餐次无法对应原话，未应用。")
        modifying = bool(result.commands or meal_adjustment(targeted(result.command)[1]))
        if result.scope == "meal" and not meal_changes:
            mentioned = {key for message in originals.values() for alias, key in MEAL_ALIASES.items() if alias in message}
            if not modifying and len(mentioned) > 1 and not meal_types and clarification == "none":
                clarification = "which_meal"
            if modifying and (len(meal_types) > 1 or not meal_types and len(facts.get("meal_context", {}).get("meal_types", [])) > 1):
                clarification = "which_meal" if clarification == "none" else clarification
            elif not modifying and not meal_types and (facts.get("meal_context", {}).get("selection_required") or any("剩下" in message for message in originals.values())):
                clarification = "which_meal" if clarification == "none" else clarification
        training = None
        if result.training is not None:
            update = result.training
            if result.scope not in ("aerobic", "strength") or not any(update.source_text in message for message in originals.values()):
                raise ModelError("COACH_INVALID_OUTPUT", "训练条件无法对应待核对原话，未应用。")
            if any(value and value not in update.source_text for value in (update.focus, update.equipment)):
                raise ModelError("COACH_INVALID_OUTPUT", "训练部位或器械不是原话内容，未应用。")
            if result.scope == "strength" and update.activity or result.scope == "aerobic" and update.focus:
                raise ModelError("COACH_INVALID_OUTPUT", "训练类型与项目不一致，未应用。")
            changes = training_update(update, result.scope, facts)
            training = merge_training(facts.get("training_context", {}), changes, facts.get("training_facts", {}).get("profile"))
        elif result.scope in ("aerobic", "strength"):
            training = merge_training(facts.get("training_context", {}), {"kind": result.scope})
            if any(alias in message for alias in SPLIT_ALIASES for message in originals.values()):
                clarification = "which_training"
        if any(note["unresolved"] for note in notes):
            clarification = "unhandled_constraint"
        if result.scope == "unclear" and clarification == "none":
            clarification = "which_request"
        if result.commands:
            if result.scope != "meal" or result.command:
                raise ModelError("COACH_INVALID_OUTPUT", "同餐修改清单不能混入其他领域或额外操作，未应用。")
            try:
                batch_meal_adjustments(result.commands)
            except ModelError as error:
                if error.code != "COACH_CHANGES_CONFLICT":
                    raise
                if clarification == "none":
                    clarification = "conflicting_changes"
        elif clarification == "none" and not meal_changes:
            matched = route(result.command)
            valid = (result.scope == "meal" and (matched == "meal" or meal_adjustment(result.command) is not None)
                     or result.scope == "aerobic" and matched == "training"
                     or result.scope in ("strength", "other") and result.command == "")
            if not valid:
                raise ModelError("COACH_INVALID_OUTPUT", "理解结果没有对应到已支持的操作，未应用。")
        question = QUESTIONS[clarification]
        if clarification == "unhandled_constraint" and result.scope in ("aerobic", "strength"):
            question = "仍有无法完整核对的训练条件或限制，请补充具体项目、器械或注意情况；不要删除真实限制来绕过。"
        replacement_names = {}
        if training and training.get("replacements"):
            from services.training_catalog import read_catalog
            keys = set(training["replacements"]) | set(training["replacements"].values())
            replacement_names = {item["id"]: item["name"] for item in read_catalog()[0] if item["id"] in keys}
        return {"scope": result.scope, "command": result.command, "commands": result.commands, "meal_changes": meal_changes, "notes": notes, "training": training, "meal_types": meal_types,
                "question": question, "clarification": clarification, "replacement_names": replacement_names}

    def understand(self, conversation_id, body, factory, model_name):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, facts, fingerprint = self.snapshot(connection, conversation_id)
            existing = connection.execute("SELECT * FROM coach_reviews WHERE conversation_id=? AND client_id=?", (row["id"], str(body.client_id))).fetchone()
            if existing:
                if existing["base_version"] != body.version:
                    raise ModelError("COACH_REQUEST_CONFLICT", "这次理解请求已用于其他版本，请刷新。", 409)
                return self.view(existing, fingerprint, row["version"])
            if body.version != row["version"] or not row["pending"] or not facts["messages"]:
                raise ModelError("COACH_CONTEXT_CHANGED", "对话已变化或没有待理解的补充，请刷新。", 409)
            if sum(len(item["message"]) for item in facts["messages"]) > 12000:
                raise ModelError("COACH_REVIEW_TOO_LARGE", "待核对原话超过当前处理上限，未截断或忽略内容。请先整理真实限制至档案，再开启新对话。", 422)
            count = connection.execute("SELECT COUNT(*) FROM coach_reviews WHERE conversation_id=? AND base_version=?", (row["id"], body.version)).fetchone()[0]
            if count >= 3:
                raise ModelError("COACH_REVIEW_LIMIT", "当前版本已尝试理解3次，请补充或纠正原话后再试。", 429)
            review_id = str(uuid4())
            connection.execute("INSERT INTO coach_reviews(id,conversation_id,client_id,base_version,context_hash,status) VALUES (?,?,?,?,?,'generating')",
                               (review_id, row["id"], str(body.client_id), body.version, fingerprint))
        try:
            prompt = (ROOT / "prompts/coach_understanding.md").read_text(encoding="utf-8")
            prompt += "\nJSON Schema:\n" + json.dumps(CoachUnderstanding.model_json_schema(), ensure_ascii=False)
            message = {key: facts[key] for key in ("messages", "profile", "constraints", "current_meal")}
            # A training-only request does not need dietary preferences or allergies for intent extraction.
            # The complete profile still participates in the fingerprint and recommendation safety checks.
            if "training_facts" in facts and not any(re.search(r"吃|餐|饭|食|过敏|忌口|营养", item["message"]) for item in facts["messages"]):
                message["profile"] = {}
            message["meal_context"] = {key: facts["meal_context"][key] for key in ("meal_types", "selection_required", "target_required") if key in facts["meal_context"]}
            if "current_meals" in facts:
                message["current_meals"] = {key: value["items"] for key, value in facts["current_meals"].items()}
            if "training_facts" in facts:
                history = facts["training_facts"]["history"]
                message["training_facts"] = {"profile": facts["training_facts"]["profile"], "window_start": history["window_start"],
                                             "day": history["day"], "recent_workouts": history["completed"][:20],
                                             "recent_count": len(history["completed"]), "truncated": len(history["completed"]) > 20}
                message["training_context"] = facts["training_context"]
                message["current_training"] = facts["current_training"]
                message["training_splits"] = SPLIT_ALIASES
            message["food_aliases"] = {key: sorted(value) for key, value in ALIASES.items()}
            message["soft_preferences"] = sorted(SOFT)
            raw = factory().generate(system_prompt=prompt, message=encode(message))
            try:
                result = CoachUnderstanding.model_validate_json(raw)
            except ValidationError:
                raise ModelError("COACH_INVALID_OUTPUT", "理解结果格式不完整，原话已保留，未应用。") from None
            payload = {**self.validate(result, facts), "model": model_name}
            status = "needs_input" if payload["question"] else "ready"
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current, _, current_hash = self.snapshot(connection, conversation_id)
                if current_hash != fingerprint:
                    raise ModelError("COACH_CONTEXT_CHANGED", "理解期间对话、档案或餐单已变化，未应用旧结果。", 409)
                connection.execute("UPDATE coach_reviews SET payload=?,status=? WHERE id=?", (encode(payload), status, review_id))
                review = connection.execute("SELECT * FROM coach_reviews WHERE id=?", (review_id,)).fetchone()
                return self.view(review, current_hash, current["version"])
        except Exception as error:
            payload = {"error": str(error) if isinstance(error, ModelError) else "理解未完成，原话已保留，未应用任何更改。",
                       "code": error.code if isinstance(error, ModelError) else "COACH_UNAVAILABLE"}
            with self.database.connect() as connection:
                connection.execute("UPDATE coach_reviews SET payload=?,status='failed' WHERE id=? AND status='generating'", (encode(payload), review_id))
            if not isinstance(error, ModelError):
                raise ModelError("COACH_UNAVAILABLE", payload["error"]) from None
            raise

    def confirm(self, conversation_id, body):
        return self.apply(conversation_id, body.version, body.review_id)

    def apply_ready(self, conversation_id, version):
        review = self.latest(conversation_id)
        if not review or review["stale"] or review["status"] != "ready" or review.get("question"):
            return self.coach.get(conversation_id)
        if review["scope"] not in ("meal", "aerobic", "strength"):
            return self.coach.get(conversation_id)
        return self.apply(conversation_id, version, review["id"], automatic=True)

    def apply(self, conversation_id, version, review_id, automatic=False):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, facts, fingerprint = self.snapshot(connection, conversation_id)
            review = connection.execute("SELECT * FROM coach_reviews WHERE id=? AND conversation_id=?", (str(review_id), row["id"])).fetchone()
            if not review:
                raise ModelError("COACH_REVIEW_NOT_FOUND", "理解草稿不存在或不属于本对话。", 404)
            if version != review["base_version"]:
                raise ModelError("COACH_CONTEXT_CHANGED", "核对版本不匹配，请刷新。", 409)
            if review["status"] == "confirmed":
                return self.coach.view(connection, row)
            latest = connection.execute("SELECT id FROM coach_reviews WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()[0]
            if review["status"] != "ready" or latest != review["id"] or fingerprint != review["context_hash"] or row["version"] != version:
                raise ModelError("COACH_REVIEW_STALE", "理解尚未就绪、仍有缺项或上下文已变化，请重新核对；不会直接解除阻断。", 409)
            payload = json.loads(review["payload"])
            constraints = dict(facts["constraints"])
            for note in payload["notes"]:
                for key in ("food_avoid", "preferences"):
                    constraints[key] = sorted(set(constraints.get(key, [])) | set(note[key]))
                for key in ("training_caution", "diet_caution"):
                    constraints[key] = bool(constraints.get(key) or note[key])
            intent = "meal" if payload["scope"] == "meal" else "training" if payload["scope"] in ("aerobic", "strength") else "unknown"
            context = facts["meal_context"]
            if intent == "meal":
                if payload.get("meal_changes"):
                    context = apply_meal_changes(connection, self.user_id, row, context, payload["meal_changes"])
                elif payload.get("commands"):
                    context = apply_meal_commands(connection, self.user_id, row, context, payload["commands"], payload.get("meal_types"))
                else:
                    context = apply_meal_command(connection, self.user_id, row, context, payload["command"], meal_types=payload.get("meal_types"))
            text = "已按本次请求更新建议条件，临时限制继续保留。" if automatic else "已按你的核对处理本次补充，临时限制继续保留；未修改长期档案或实际记录。"
            if intent == "meal" and meal_question(context):
                text += meal_question(context)
            if payload.get("commands"):
                text += "本次合并调整：" + "；".join(payload["commands"]) + "。全部核对通过才返回新餐单。"
            if payload["scope"] == "other":
                text += "这个请求目前不在已开放能力内。"
            elif constraints.get("diet_caution") or intent == "training" and constraints.get("training_caution"):
                text += "本次请求超出一般健身范围，不提供医疗、伤病或康复方案。"
            response = {"source": "applied_understanding" if automatic else "reviewed_understanding", "status": "applied" if automatic else "reviewed", "intent": intent, "text": text,
                        "meal_context": context, "constraints": constraints, "review_id": review["id"],
                        "reviewed_versions": [note["version"] for note in payload["notes"]]}
            from services.coach import capture_meal_readiness
            capture_meal_readiness(connection, self.user_id, row["day"], response)
            training = payload.get("training") or facts["training_context"]
            if training:
                response["training_context"] = training
                if intent == "training" and not constraints.get("training_caution") and not constraints.get("diet_caution"):
                    response["text"] += training_question(training)
            connection.execute("UPDATE coach_turns SET response=? WHERE conversation_id=? AND version=?", (encode(response), row["id"], row["version"]))
            connection.execute("UPDATE coach_reviews SET status='confirmed' WHERE id=?", (review["id"],))
            connection.execute("UPDATE coach_conversations SET pending=0,intent=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (intent, row["id"]))
            return self.coach.view(connection, self.coach.owned(connection, row["id"]))
