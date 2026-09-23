"""Persistent, bounded workflow routing. No free-form model advice in this stage."""
import json
import re
from datetime import date
from uuid import uuid4

from fastapi import HTTPException

from model.factory import ModelError
from schemas import MealIntakeReview
from services.meal_intake import MealIntakeService, planning_context
from services.plan_foods import ALIASES, FOODS
from services.training_context import training_context, training_command, merge_training, training_question
from services.meal_schedule import MEAL_NAMES, selection, targeted, question as meal_question, scoped

QUICK_QUESTIONS = ("今天剩下怎么吃？", "结合前几天的训练，今天练什么？", "怎么练？")
MEAL_REQUESTS = {"今天剩下怎么吃", "下一餐吃什么", "晚上吃什么", "晚饭吃什么", "今天晚上怎么吃", "今天晚上吃什么", "今晚怎么吃", "今天吃什么", "下一餐", "饮食建议"}
TRAINING_REQUESTS = {"结合前几天的训练，今天练什么", "结合前几天的训练,今天练什么", "今天练什么", "怎么练", "训练建议", "有氧建议"}


def meal_adjustment(message):
    value = message.strip().rstrip("？?。！! ")
    value = re.sub(r"^那(?:么)?[，, ]*", "", value)
    names = "|".join(sorted(ALIASES, key=len, reverse=True))
    match = re.fullmatch(r"(?:我)?(?:这顿|本餐|今天)?(?:不爱吃|不喜欢吃|不想吃|不吃|不喜欢)(" + names + r")(?:[，, ]*(?:请)?(?:换掉|换一个|换别的|换一下))?", value)
    if match:
        return {"excluded_foods": sorted(ALIASES[match[1]])}
    match = re.fullmatch(r"(?:我)?(?:想|希望)?(?:把)?(" + names + r")(?:给我|给)?(?:换成|换为)(" + names + r")", value)
    if match:
        source, target = ALIASES[match[1]], ALIASES[match[2]]
        groups = {FOODS[key][1] for key in source | target}
        if len(groups) == 1:
            group = next(iter(groups))
            return {"group_choices": {group: sorted(target)}, "adjustment": {"kind": "replace", "source_foods": sorted(source)}}
        return None
    match = re.fullmatch(r"(?:(?:我)?(?:想)?(?:吃|换成)|蛋白质换成|(?:请)?(?:你)?给我(?:配|换)(?:一)?点)(" + names + r")", value)
    if match:
        target = ALIASES[match[1]]
        groups = {FOODS[key][1] for key in target}
        if len(groups) == 1:
            return {"group_choices": {next(iter(groups)): sorted(target)}}
    match = re.fullmatch(r"(?:我)?(?:想)?(?:少吃|少配|少放)(?:一)?点(" + names + r")", value)
    if not match:
        match = re.fullmatch(r"(?:把)?(" + names + r")(?:给我)?(?:少|少配|少放|减少)(?:一)?点", value)
    if match:
        return {"adjustment": {"kind": "reduce", "source_foods": sorted(ALIASES[match[1]])}}
    if value in {"吃素", "我吃素", "这顿吃素", "本餐只吃植物性食材"}:
        return {"plant_only": True}
    return None


def meal_context(connection, conversation_id):
    rows = connection.execute("SELECT response FROM coach_turns WHERE conversation_id=? ORDER BY version DESC", (conversation_id,)).fetchall()
    for row in rows:
        response = json.loads(row[0])
        if "meal_context" in response:
            return response["meal_context"]
    return {}


def conversation_constraints(connection, conversation_id):
    rows = connection.execute("SELECT response FROM coach_turns WHERE conversation_id=? ORDER BY version DESC", (conversation_id,)).fetchall()
    for row in rows:
        response = json.loads(row[0])
        if "constraints" in response:
            return response["constraints"]
    return {}


def apply_single_meal_command(connection, user_id, row, context, command, target=None):
    adjustment = meal_adjustment(command)
    context = {key: value for key, value in context.items() if key not in ("base_plan_id", "meal_type", "adjustment", "adjustments", "batch_commands")}
    if adjustment is None:
        return context
    context["excluded_foods"] = sorted(set(context.get("excluded_foods", [])) | set(adjustment.get("excluded_foods", [])))
    choices = dict(context.get("group_choices", {}))
    legacy = context.pop("protein_choice", None)
    if legacy:
        choices.setdefault("protein", [legacy])
    choices.update(adjustment.get("group_choices", {}))
    if adjustment.get("excluded_foods"):
        choices = {group: keys for group, keys in choices.items() if set(keys) - set(context["excluded_foods"])}
    if choices:
        context["group_choices"] = choices
    else:
        context.pop("group_choices", None)
    context.update({key: value for key, value in adjustment.items() if key not in ("excluded_foods", "group_choices")})
    previous = connection.execute(
        "SELECT id,meal_type FROM meal_plans WHERE user_id=? AND day=? AND status IN ('draft','accepted') "
        "AND json_extract(input_payload,'$.coach_id')=? AND (? IS NULL OR meal_type=?) ORDER BY rowid DESC LIMIT 1",
        (user_id, row["day"], row["id"], target, target),
    ).fetchone()
    if previous:
        context.update(base_plan_id=previous["id"], meal_type=previous["meal_type"])
    return context


def apply_meal_command(connection, user_id, row, context, command, *, version=None, meal_types=None):
    version = row["version"] if version is None else version
    requested = meal_types or selection(command)
    target, plain = targeted(command)
    adjustment = meal_adjustment(plain)
    inherited = {key: value for key, value in context.items() if key in ("excluded_foods", "group_choices", "protein_choice", "plant_only")}
    if command.strip().rstrip("？?。！! ") in {"今天剩下怎么吃", "今天吃什么"} and not requested:
        return {**context, "selection_required": True}
    if requested and adjustment is None:
        contexts = {key: value for key, value in context.get("by_meal", {}).items() if key in requested}
        versions = {key: value for key, value in context.get("meal_versions", {}).items() if key in requested}
        for key in requested:
            contexts[key] = apply_single_meal_command(connection, user_id, row, contexts.get(key, inherited), "下一餐吃什么", key)
            versions[key] = version
        return {"meal_types": [key for key in MEAL_NAMES if key in contexts], "by_meal": contexts, "meal_versions": versions}
    if adjustment is not None and (target or requested or "meal_types" in context):
        choices = requested or ([target] if target else context.get("meal_types", []))
        if len(choices) != 1:
            return {**context, "target_required": True, "pending_command": command}
        target = choices[0]
        contexts = dict(context.get("by_meal", {}))
        contexts[target] = apply_single_meal_command(connection, user_id, row, contexts.get(target, inherited), plain, target)
        return {"meal_types": [key for key in MEAL_NAMES if key in contexts], "by_meal": contexts,
                "meal_versions": {**context.get("meal_versions", {}), target: version}}
    if "meal_types" in context:
        return {**context, "selection_required": True}
    return apply_single_meal_command(connection, user_id, row, {key: value for key, value in context.items() if key not in ("selection_required", "target_required", "pending_command")}, command)


def batch_meal_adjustments(commands):
    if not 2 <= len(commands) <= 3:
        raise ModelError("COACH_INVALID_OUTPUT", "同餐修改清单应包含2至3项明确操作，未应用。")
    changes, groups = [], set()
    for command in commands:
        change = meal_adjustment(command)
        if change is None:
            raise ModelError("COACH_INVALID_OUTPUT", "修改清单含未支持或不明确的操作，未应用。")
        affected = set(change.get("group_choices", {}))
        foods = set(change.get("excluded_foods", [])) | set(change.get("adjustment", {}).get("source_foods", []))
        affected.update(FOODS[key][1] for key in foods)
        if change.get("plant_only"):
            affected.add("protein")
        if not affected or affected & groups:
            raise ModelError("COACH_CHANGES_CONFLICT", "同一类别有多个修改，请先明确要保留的一个要求。", 409)
        groups.update(affected)
        changes.append(change)
    return changes


def apply_meal_commands(connection, user_id, row, context, commands, meal_types=None, *, version=None):
    changes = batch_meal_adjustments(commands)
    choices = meal_types or context.get("meal_types", [])
    if len(choices) > 1:
        return {**context, "target_required": True}
    # Merge preferences once; collect transient edits instead of overwriting them.
    for command in commands:
        context = apply_meal_command(connection, user_id, row, context, command, meal_types=choices, version=version)
    selected = scoped(context, choices[0]) if choices else context
    selected.pop("adjustment", None)
    selected["adjustments"] = [change["adjustment"] for change in changes if "adjustment" in change]
    selected["batch_commands"] = list(commands)
    return context


def validate_meal_changes(changes):
    targets = [change["meal_type"] for change in changes]
    if not 1 <= len(targets) <= 3 or len(set(targets)) != len(targets) or any(key not in MEAL_NAMES for key in targets):
        raise ModelError("COACH_INVALID_OUTPUT", "逐餐修改需要1至3个不同餐次，未应用。")
    for change in changes:
        commands = change["commands"]
        if len(commands) == 1:
            if meal_adjustment(commands[0]) is None:
                raise ModelError("COACH_INVALID_OUTPUT", "餐次修改含未支持的操作，未应用。")
        else:
            batch_meal_adjustments(commands)


def explicit_meal_changes(message):
    clauses = [part.strip() for part in re.split(r"[；;，,。\n]+", message.strip().rstrip("。!！?？ ")) if part.strip()]
    if not 2 <= len(clauses) <= 3:
        return None
    changes = []
    for clause in clauses:
        meal, command = targeted(clause)
        if meal is None or meal_adjustment(command) is None:
            return None
        changes.append({"meal_type": meal, "source_text": clause, "commands": [command]})
    try:
        validate_meal_changes(changes)
    except ModelError:
        return None
    return changes


def apply_meal_changes(connection, user_id, row, context, changes, *, version=None):
    # Validate every meal before updating any context; no partially applied batch.
    validate_meal_changes(changes)
    for change in changes:
        commands, meal = change["commands"], change["meal_type"]
        if len(commands) == 1:
            context = apply_meal_command(connection, user_id, row, context, commands[0], meal_types=[meal], version=version)
        else:
            context = apply_meal_commands(connection, user_id, row, context, commands, [meal], version=version)
    return context


def route(message):
    value = message.strip().rstrip("？?。！! ")
    if value in MEAL_REQUESTS or selection(value) or explicit_meal_changes(value) or (targeted(value)[0] and meal_adjustment(targeted(value)[1])):
        return "meal"
    if value in TRAINING_REQUESTS:
        return "training"
    return "unknown"


def plan_binding(connection, user_id, original, day, intent):
    """Called in plan snapshots, including after inference and on acceptance."""
    if not original or not original.get("coach_id"):
        return None
    row = connection.execute(
        "SELECT day,version,intent,pending FROM coach_conversations WHERE id=? AND user_id=?",
        (str(original["coach_id"]), user_id),
    ).fetchone()
    valid = bool(row and row["day"] == day and row["version"] == original.get("coach_version")
                 and row["intent"] == intent and not row["pending"])
    binding = {"id": str(original["coach_id"]), "version": row["version"] if row else None,
               "valid": valid, "pending": bool(row and row["pending"])}
    context = meal_context(connection, str(original["coach_id"])) if row and intent == "meal" else {}
    if "meal_types" in context:
        meal_type = original.get("meal_type")
        meal_version = context.get("meal_versions", {}).get(meal_type)
        binding.update(version=meal_version, valid=bool(row and row["day"] == day and row["intent"] == intent
                       and not row["pending"] and not meal_question(context) and meal_version == original.get("coach_version")))
        context = scoped(context, meal_type)
    elif meal_question(context):
        binding["valid"] = False
    if context:
        binding["meal_context"] = context
    if row and intent == "training":
        training = training_context(connection, str(original["coach_id"]))
        if training:
            binding["training_context"] = training
    constraints = conversation_constraints(connection, str(original["coach_id"])) if row else {}
    if any(constraints.values()):
        binding["constraints"] = constraints
        binding["intent"] = intent
    return binding


def require_plan_binding(facts):
    if facts.get("coach") is not None and not facts["coach"]["valid"]:
        raise ModelError("COACH_CONTEXT_CHANGED", "对话已变化、已删除或有尚未处理的补充，当前建议不能生成或采纳。请回到助手核对。", 409)
    binding = facts.get("coach") or {}
    if binding.get("training_context", {}).get("kind") == "strength":
        raise ModelError("TRAINING_NOT_READY", "力量需求已保留，审核依据不足，不能改成有氧生成。", 409)
    constraints = binding.get("constraints", {})
    if binding.get("intent") == "training" and constraints.get("training_caution"):
        raise ModelError("COACH_TRAINING_CAUTION", "本次请求超出一般健身范围，不提供医疗、伤病或康复训练方案。", 409)
    if constraints.get("diet_caution"):
        raise ModelError("COACH_HEALTH_CAUTION", "本对话含特殊健康或饮食管理情况，当前一般饮食/训练建议暂停。", 409)


def capture_meal_readiness(connection, user_id, day, response):
    if response["intent"] != "meal" or response["status"] == "needs_review" or meal_question(response.get("meal_context", {})):
        return
    from services.meal_nutrient_reference import current_context
    if response.get("constraints", {}).get("diet_caution"):
        state = {"ready": False, "reason": "health_caution", "message": "本对话含特殊健康或饮食管理情况，当前一般饮食建议暂停。"}
    else:
        try:
            info = current_context(connection, user_id, day)
            state = {key: info[key] for key in ("ready", "reason", "message")}
        except ModelError as error:
            state = {"ready": False, "reason": "reference_unavailable", "message": str(error)}
    response["meal_readiness"] = state
    if not state["ready"]:
        response["text"] = "这次还没有生成餐单。" + state["message"]


class CoachService:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id

    def owned(self, connection, conversation_id):
        row = connection.execute("SELECT * FROM coach_conversations WHERE id=? AND user_id=?",
                                 (str(conversation_id), self.user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "对话不存在")
        return row

    def context(self, connection, day):
        start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 6)).isoformat()
        from services.profile_context import read_profile
        profile = read_profile(connection, self.user_id)
        meals = connection.execute("SELECT payload FROM meals WHERE user_id=? AND day=? ORDER BY id", (self.user_id, day)).fetchall()
        workouts = connection.execute("SELECT payload FROM workouts WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day DESC,id DESC",
                                      (self.user_id, start, day)).fetchall()
        completed = [json.loads(row[0]) for row in workouts if json.loads(row[0]).get("status") == "completed"]
        return {
            "day": day, "window_start": start, "window_days": 7,
            "profile": {key: profile[key] for key in ("goal", "experience", "equipment", "minutes_per_session", "training_days", "training_split", "preferences", "food_allergies")},
            "meal_count": len(meals), "recent_completed_count": len(completed),
            "completed_minutes_today": sum(item["minutes"] for item in completed if item["day"] == day),
            "meals": [{key: item.get(key) for key in ("meal_type", "name", "grams", "amount_description")} for item in [json.loads(row[0]) for row in meals[:100]]],
            "recent_workouts": [{key: item.get(key) for key in ("day", "name", "minutes", "details")} for item in completed[:100]],
            "truncated": len(meals) > 100 or len(completed) > 100,
        }

    def view(self, connection, row):
        turns = connection.execute("SELECT version,message,response,created_at FROM coach_turns WHERE conversation_id=? ORDER BY version", (row["id"],)).fetchall()
        constraints = conversation_constraints(connection, row["id"])
        training = training_context(connection, row["id"])
        blocked = constraints.get("diet_caution") or row["intent"] == "training" and constraints.get("training_caution")
        return {"id": row["id"], "title": row["title"], "day": row["day"], "version": row["version"],
                "pending": bool(row["pending"]), "intent": row["intent"],
                "action": row["intent"] if row["version"] and not row["pending"] and not blocked and row["intent"] in ("meal", "training") else None,
                "turns": [{**dict(turn), "response": json.loads(turn["response"])} for turn in turns],
                "meal_context": meal_context(connection, row["id"]),
                "meal_question": meal_question(meal_context(connection, row["id"])),
                "training_context": training,
                "training_question": training_question(training) if row["intent"] == "training" else "",
                "constraints": constraints,
                "context": self.context(connection, row["day"])}

    def list(self):
        with self.database.connect() as connection:
            rows = connection.execute("SELECT id,title,day,version,pending FROM coach_conversations WHERE user_id=? ORDER BY updated_at DESC,rowid DESC", (self.user_id,)).fetchall()
            return [dict(row) for row in rows]

    def get(self, conversation_id):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return self.view(connection, self.owned(connection, conversation_id))

    def create(self, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM coach_conversations WHERE user_id=? AND client_id=?", (self.user_id, str(body.client_id))).fetchone()
            if row:
                if row["day"] != body.day.isoformat():
                    raise HTTPException(409, "这次创建标识已用于其他日期")
                return self.view(connection, row)
            count = connection.execute("SELECT COUNT(*) FROM coach_conversations WHERE user_id=?", (self.user_id,)).fetchone()[0]
            if count >= 50:
                raise HTTPException(409, "最多保留50个对话，请先删除不需要的对话")
            conversation_id = str(uuid4())
            connection.execute("INSERT INTO coach_conversations(id,user_id,client_id,day) VALUES (?,?,?,?)", (conversation_id, self.user_id, str(body.client_id), body.day.isoformat()))
            return self.view(connection, self.owned(connection, conversation_id))

    def send(self, conversation_id, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.owned(connection, conversation_id)
            duplicate = connection.execute("SELECT version,message FROM coach_turns WHERE conversation_id=? AND client_id=?", (row["id"], str(body.client_id))).fetchone()
            if duplicate:
                if duplicate["version"] != body.version + 1 or duplicate["message"] != body.message:
                    raise HTTPException(409, "这次发送标识已用于其他内容")
                return self.view(connection, row)
            if row["version"] != body.version:
                raise HTTPException(409, "对话已在其他页面更新，请刷新对话后再发送")
            if row["version"] >= 40:
                raise HTTPException(409, "本次对话已满40轮，请先核对未处理补充，再开启新对话")
            message = body.message
            intake_statement = re.fullmatch(r"(?:我)?(今天已吃的都记录好了|今天已经吃的都记录好了|今天还没吃东西)[，,；;。\s]*(.*)", message)
            if intake_statement:
                current_intake = planning_context(connection, self.user_id, date.fromisoformat(row["day"]))
                MealIntakeService(self.database, self.user_id).confirm_in_transaction(connection, MealIntakeReview(
                    day=row["day"], version=current_intake["version"], context_hash=current_intake["context_hash"],
                    record_state="none_yet" if intake_statement[1] == "今天还没吃东西" else "complete", confirmed=True))
                previous_meals = meal_context(connection, row["id"]).get("meal_types", [])
                message = intake_statement[2] or ("安排" + "和".join(MEAL_NAMES[key] for key in previous_meals) if previous_meals else "今天剩下怎么吃")
            intent = route(message)
            training = training_context(connection, row["id"])
            previous = connection.execute("SELECT input_payload FROM training_plans WHERE user_id=? AND day=? AND status IN ('draft','accepted') AND json_extract(input_payload,'$.coach_id')=? AND json_extract(input_payload,'$.coach_version')=? ORDER BY rowid DESC LIMIT 1", (self.user_id, row["day"], row["id"], row["version"])).fetchone()
            if previous:
                original = json.loads(previous[0])
                training = merge_training(training, {"kind": "aerobic", "minutes": original["daily_minutes"], "time_basis": original.get("time_basis", "daily"), "activity": original["activity"]})
            training_change = training_command(body.message, continuing=row["intent"] == "training")
            if training_change is not None:
                intent = "training"
                if not row["pending"]:
                    training = merge_training(training, training_change, self.context(connection, row["day"])["profile"])
            adjustment = meal_adjustment(targeted(message)[1])
            meal_changes = explicit_meal_changes(message)
            context = meal_context(connection, row["id"])
            if adjustment is not None:
                intent = "meal"
            if intent == "meal":
                if meal_changes:
                    context = apply_meal_changes(connection, self.user_id, row, context, meal_changes, version=row["version"] + 1)
                else:
                    context = apply_meal_command(connection, self.user_id, row, context, message, version=row["version"] + 1)
            pending = bool(row["pending"] or intent == "unknown")
            if pending:
                answer = "补充已保存在本次对话，核对完整意思和限制之前暂停生成。长期食物过敏请保存在个人档案。"
            elif intent == "meal":
                if meal_question(context):
                    answer = meal_question(context)
                elif "meal_types" in context:
                    changed = [key for key, value in context["meal_versions"].items() if value == row["version"] + 1]
                    answer = "已收到" + "、".join(MEAL_NAMES[key] for key in changed) + "的请求，餐单尚未生成。"
                elif adjustment is not None:
                    omitted = "、".join(FOODS[key][0] for key in context.get("excluded_foods", []))
                    preference = f"这段对话的后续餐单避开：{omitted}。" if omitted else "已记下这顿的食材偏好。"
                    if adjustment.get("adjustment", {}).get("kind") == "reduce":
                        preference = "本次只减少你指定食材的份量。" + preference
                    elif adjustment.get("group_choices"):
                        preferred = "、".join(FOODS[key][0] for keys in adjustment["group_choices"].values() for key in keys)
                        preference = f"本次从{preferred}中选择符合禁忌的食材替换同类项。" + preference
                    answer = preference + "会保留其他食材，只调整指定食材或份量；不能覆盖档案过敏，也不会永久修改档案。生成后可核对替代草稿。"
                else:
                    answer = "核对餐次和饮食限制后，我会给出食材与估算份量；生成后可以继续说哪种食材不想吃。也可以明确要一起安排哪几餐。"
            else:
                answer = training_question(training)
            response = {"text": answer, "source": "workflow", "status": "needs_review" if pending else "guided", "intent": intent}
            if context:
                response["meal_context"] = context
            constraints = conversation_constraints(connection, row["id"])
            if constraints:
                response["constraints"] = constraints
            capture_meal_readiness(connection, self.user_id, row["day"], response)
            if training:
                response["training_context"] = training
            version = row["version"] + 1
            connection.execute("INSERT INTO coach_turns(id,conversation_id,client_id,version,message,response) VALUES (?,?,?,?,?,?)", (str(uuid4()), row["id"], str(body.client_id), version, body.message, json.dumps(response, ensure_ascii=False)))
            connection.execute("UPDATE coach_conversations SET title=?,version=?,intent=?,pending=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (body.message[:40] if row["version"] == 0 else row["title"], version, intent, int(pending), row["id"], self.user_id))
            return self.view(connection, self.owned(connection, row["id"]))

    def delete(self, conversation_id, version):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.owned(connection, conversation_id)
            if row["version"] != version:
                raise HTTPException(409, "对话已变化，请刷新后再确认删除")
            connection.execute("DELETE FROM coach_conversations WHERE id=? AND user_id=?", (row["id"], self.user_id))
