"""Evidence-bound suggestions. This module never writes workouts or training plans."""
import json
import re
from datetime import date, datetime, timezone

from model.factory import ModelError
from rag.rag_service import KnowledgeUnavailable
from services.coach import CoachService
from services.meal_plans import digest, encode
from services.training_context import recent_training
from services.training_plans import HEALTH
from services.training_catalog import read_catalog, read_systems
from services.training_program import session, today
from services.training_progression import SOURCE as PROGRESSION_SOURCE, performance_history, attach_guidance

POLICY = "training-today-2026-09-23.3-image-sets"
GROUPS = {"chest": "胸部", "back": "背部", "legs": "腿臀", "core": "核心", "shoulders": "肩部", "arms": "手臂"}
PATTERNS = {
    "chest": r"胸|卧推|推胸|俯卧撑|chest|bench.?press|push.?up",
    "back": r"背|划船|引体|下拉|back|row|pull.?up",
    "legs": r"腿|臀|内收肌|深蹲|硬拉|弓步|leg|glute|squat|deadlift|lunge",
    "core": r"腹|核心|腰|卷腹|平板|core|crunch|plank",
    "shoulders": r"肩|推举|侧平举|shoulder|overhead",
    "arms": r"手臂|胳膊|二头|三头|弯举|臂屈伸|arms?|biceps?|triceps?|curl",
}
CARDIO = re.compile(r"游泳|步行|快走|散步|慢跑|跑步|骑行|单车|爬坡|爬楼|有氧|swim|walk|jog|cycl|cardio", re.I)
NEGATION = re.compile(r"没有|没练|未练|不练|没做|不做|计划|准备|打算|明天|未完成|not\b|didn't", re.I)
REQUIRED = ("mayo-strength:basics", "cdc-activities:sets", "cdc-adults:aerobic",
            "cdc-intensity:activities", "cdc-intensity:talk-test", "cdc-start:gradual", "acsm-2026:programming")


def equipment_tokens():
    tokens = {"游泳池": "pool", "泳池": "pool", "哑铃": "dumbbell", "可调节哑铃": "dumbbell", "可调哑铃": "dumbbell", "自行车": "cycle",
              "跑步机": "treadmill", "健身房": "floor", "家里面": "floor", "家里": "floor", "家中": "floor",
              "在家": "floor", "居家": "floor", "自重": "floor", "徒手": "floor",
              "无器械": "floor", "瑜伽垫": "floor", "户外": "outdoors", "室外": "outdoors",
              "公园": "outdoors", "跑道": "outdoors"}
    tokens.update({"弹力带": "band", "阻力带": "band", "训练凳": "seat", "稳定椅子": "seat", "椅子": "seat",
                   "推胸机": "chest-machine", "高位下拉机": "lat-machine", "坐姿划船机": "row-machine",
                   "腿举机": "leg-press-machine", "腿屈伸机": "knee-extension-machine",
                   "俯卧腿弯举机": "hamstring-machine", "绳索直杆下压": "cable-triceps"})
    tokens.update({"龙门架": "cable", "楼门架": "cable", "绳索机": "cable", "杠铃": "barbell",
        "平板训练凳": "bench", "平板凳": "bench", "平板卧推凳": "bench", "上斜卧推凳": "incline-bench", "上斜训练凳": "incline-bench", "上斜凳": "incline-bench",
        "卧推凳": "bench",
        "可调训练凳": "adjustable-bench", "靠背训练凳": "backrest", "靠背座椅": "backrest",
        "深蹲架": "rack", "卧推架": "rack", "保护杠": "safeties", "安全杠": "safeties",
        "双侧可调滑轮": "dual-adjustable", "双滑轮": "dual-pulley", "可调滑轮": "adjustable-pulley",
        "双把手": "handles", "两个D型把手": "handles", "直杆把手": "cable-bar", "绳索把手": "rope",
        "蝴蝶机": "pec-deck", "夹胸机": "pec-deck", "推肩机": "shoulder-machine",
        "二头弯举机": "curl-machine", "绳索坐姿划船机": "cable-row-machine",
        "单杠": "pullup-bar", "弹力带固定点": "band-anchor", "内收机": "adductor-machine",
        "内收肌机器": "adductor-machine", "史密斯机": "smith-machine",
        "坐姿腿弯举机": "seated-curl-machine", "壶铃": "kettlebell",
        "引体辅助弹力带": "pullup-band", "提踵适配踏板": "calf-platform",
        "单D型把手": "single-handle", "单把手": "single-handle", "单d型把手": "single-handle",
        "两个d型把手": "handles", "脚踝绑带": "ankle-cuff", "踝带": "ankle-cuff",
        "稳固扶手": "stable-support", "稳定扶手": "stable-support",
        "环形弹力带": "loop-band", "环形阻力带": "loop-band",
        "史密斯安全限位": "smith-stops", "髋内收机": "adductor-machine",
        "髋外展机": "abductor-machine", "外展机": "abductor-machine",
        "反向蝴蝶机": "rear-delt-machine", "肩后束机": "rear-delt-machine",
        "三头伸展机": "triceps-machine", "三头肌训练机": "triceps-machine",
        "坐姿卷腹机": "abdominal-machine", "卷腹机": "abdominal-machine"})
    return dict(sorted(tokens.items(), key=lambda item: len(item[0]), reverse=True))


def denied_equipment(text):
    tokens = equipment_tokens()
    words = "|".join(map(re.escape, tokens))
    value = re.sub(r"(没有|没|不用|不能用|不能去|不去)(" + words + r")((?:和|及|与|、)(?:" + words + r"))+",
                   lambda match: "，".join(match[1] + word for word in re.findall(words, match[0])), text)
    return {tokens[match[1]] for match in re.finditer(r"(?:没有|没|无|不用|不能用|不能去|不去)\s*(" + words + r")", value)}


def equipment(text):
    """A small explicit vocabulary; negated or unknown facilities never become available."""
    value = text.lower().strip()
    found = set()
    tokens = equipment_tokens()
    words = "|".join(map(re.escape, tokens))
    value = re.sub(r"(没有|没|不用|不能用|不能去|不去)(" + words + r")((?:和|及|与|、)(?:" + words + r"))+",
                   lambda match: "，".join(match[1] + word for word in re.findall(words, match[0])), value)
    denied = set()
    for word, key in tokens.items():
        if re.search(r"(?:没有|没|无|不用|不能用|不能去|不去)\s*" + word, value):
            denied.add(key)
            value = re.sub(r"(?:没有|没|无|不用|不能用|不能去|不去)\s*" + word, "", value)
    for word, key in tokens.items():
        if word in value:
            found.add(key)
            value = value.replace(word, "")
    remainder = re.sub(r"我|今天|现在|器械|场地|只有|仅有|只用|可用|可以用|还有|只有一个|和|及|与|有|是|就|在|仅|只|用|一个|一对|一副|但是|但|、|[，,。；;：:/\s]", "", value)
    implications = {"dual-adjustable": {"dual-pulley", "adjustable-pulley"},
                    "adjustable-bench": {"bench", "incline-bench", "backrest", "seat"},
                    "bench": {"seat"}, "backrest": {"seat"}, "incline-bench": {"seat"},
                    "handles": {"single-handle"}}
    found -= denied
    for value in list(found):
        found.update(implications.get(value, set()))
    found -= denied
    # An explicit denial of a capability also removes equipment that requires it.
    for key, capabilities in implications.items():
        if capabilities & denied:
            found.discard(key)
    if {"cable", "adjustable-pulley", "cable-bar"} <= found and "cable-triceps" not in denied:
        found.add("cable-triceps")
    if found & {"dumbbell", "band", "seat", "barbell", "cable", "kettlebell", "loop-band", "smith-machine"} and "floor" not in denied:
        found.add("floor")
    return found, bool(remainder) or not found


def history_summary(history):
    day = date.fromisoformat(history["day"])
    monday = date.fromordinal(day.toordinal() - day.weekday()).isoformat()
    recent_loads, scores, unknown = set(), {key: 0 for key in GROUPS}, []
    cardio = []
    sessions = {}
    catalog, _ = read_catalog()
    for item in history["completed"]:
        name = item["name"].lower()
        age = (day - date.fromisoformat(item["day"])).days
        if not 0 <= age <= 6:
            continue
        exact = next((item for item in catalog if item["name"].lower() == name), None)
        muscle = {exact["focus"]} if exact else {key for key, pattern in PATTERNS.items() if re.search(pattern, name, re.I)}
        if re.search(r"腿弯举|hamstring.*curl|leg.*curl", name, re.I):
            muscle.discard("arms")
            muscle.add("legs")
        aerobic = bool(CARDIO.search(name))
        if NEGATION.search(name) or (not muscle and not aerobic) or (muscle and aerobic):
            if age <= 1:
                unknown.append(item)
            continue
        if aerobic:
            if item["day"] >= monday:
                cardio.append(item)
            # Swimming also loads the upper body; do not infer shoulder recovery.
            if age <= 1 and re.search(r"游泳|swim", name):
                recent_loads.update({"chest", "back", "shoulders", "arms"})
        if muscle:
            daily = sessions.setdefault(item["day"], {})
            for group in muscle:
                daily[group] = daily.get(group, 0) + item["minutes"]
            loads = set(exact["loads"]) if exact else set(muscle)
            if muscle & {"chest", "back"}:
                loads.update({"shoulders", "arms"})
            if age <= 1:
                recent_loads.update(loads)
            for key in scores:
                if key in muscle:
                    scores[key] += (7 - age) * item["minutes"]
    return {"recent_loads": sorted(recent_loads), "scores": scores, "unknown": unknown, "sessions": sessions,
            "cardio_days": len({row["day"] for row in cardio}), "cardio_minutes": sum(row["minutes"] for row in cardio)}


def build(facts, evidence):
    profile, request = facts["profile"], facts["request"]
    result = {"status": "needs_input", "title": "今天练什么", "message": "", "exercises": [], "aerobic_options": [],
              "history": facts["history"], "sources": [], "basis": "reviewed_rules", "warnings": []}

    def stop(message, status="needs_input"):
        return {**result, "status": status, "message": message}

    if request.get("unsupported_split"):
        return stop("当前只提供三分化、四分化和五分化，请从这三种训练结构中选择。")

    if HEALTH.search(profile["preferences"] + " " + profile["food_allergies"]) or any(
            facts["constraints"].get(key) for key in ("training_caution", "diet_caution")):
        return stop("本产品仅面向无伤病的一般成人健身，不提供医疗、伤病或康复训练方案。", "blocked")
    missing = [key for key in REQUIRED if key not in evidence]
    if missing:
        return stop("所需训练依据缺失、撤回或待复核，暂不生成动作。", "unavailable")
    exercises, _ = read_catalog()
    if not exercises:
        return stop("训练动作目录缺失或格式异常，暂不生成动作。", "unavailable")
    resources, uncertain = equipment(request.get("equipment") or profile["equipment"])
    if uncertain:
        return stop("请明确本次可用场地和器械，例如“在家，只有哑铃”或“健身房和泳池”；暂不猜测可用设备。")
    result["equipment"] = request.get("equipment") or profile["equipment"]
    if "健身房" in result["equipment"] and resources <= {"floor", "pool"}:
        result["equipment_note"] = "档案目前只写了健身房，尚未确认具体器械；力量部分先按徒手安排。可在个人档案补充哑铃、训练凳或实际可用的机器。"
    minutes = request.get("minutes", profile["minutes_per_session"])
    basis = request.get("time_basis", "session") if request.get("minutes") else "profile_session"
    if basis == "unspecified":
        return stop("这段时间是今天总共，还是接下来可用？可以说“今天总共30分钟”或“接下来还有30分钟”。")
    if basis == "daily":
        minutes -= sum(row["minutes"] for row in facts["history"]["completed"] if row["day"] == facts["day"])
    if minutes < 15:
        return stop("本次可用时间不足15分钟，当前基础组合不适用，未追加训练。")
    history = history_summary(facts["history"])
    result["weekly_cardio"] = {key: history[key] for key in ("cardio_days", "cardio_minutes")}
    result["time_basis"] = basis
    result["available_minutes"] = minutes
    result["warnings"] = ["只依据已保存的完成记录；未记录不等于没练，间隔不等于已经恢复。",
        "一般健康成人训练起点，不是个体处方。若疼痛、明显疲劳或动作失控应停止，不要求每组力竭。",
        "使用稳定地面和可控制的轻负重，不预设公斤数；动作/组次按当前能力下调，组间充分休息。"]
    if basis == "profile_session":
        result["warnings"].append(f"本次按档案每次{minutes}分钟估排，不代表你已确认今天有这些时间。")
    if "cable" in resources and not {"dual-pulley", "adjustable-pulley", "handles"} <= resources:
        result["warnings"].append("龙门架尚未确认双侧可调滑轮与双把手；夹胸需核对这些条件，坐姿绳索推胸还需稳固靠背座位。")
    if "barbell" in resources and not {"bench", "rack", "safeties"} <= resources:
        result["warnings"].append("杠铃卧推需平板训练凳、卧推架和保护杠；背蹲需深蹲架与保护杠。未确认的条件不会自动补齐。")
    if history["unknown"]:
        return stop("今天或昨天有未能确定部位的完成记录。请补充这些记录的训练部位即可，不要求动作组次；暂不据此安排新的力量负荷。")
    focus = request.get("focus")
    requested = {key for key, pattern in PATTERNS.items() if focus and re.search(pattern, focus, re.I)}
    if focus and (len(requested) != 1 or NEGATION.search(focus) or re.search(r"最大|力竭|大重量", focus)):
        return stop("请明确一个训练方向；不生成极限负重、力竭或未覆盖的复合专项。")
    if request.get("kind") != "aerobic":
        eligible = [item for item in exercises if item["needs"] <= resources
                    and (not requested or item["focus"] in requested)
                    and not item["loads"] & set(history["recent_loads"])]
        if focus and "二头" in focus and "三头" not in focus:
            eligible = [item for item in eligible if item["pattern"] in ("biceps", "hammer-curl")]
        elif focus and "三头" in focus and "二头" not in focus:
            eligible = [item for item in eligible if item["pattern"] == "triceps"]
        if eligible and not any(item["source"] in evidence for item in eligible):
            return stop("匹配当前方向与器械的动作依据已撤回或待复核，暂不推荐。", "unavailable")
        selected = None
        only_device = "只用" in result["equipment"] and "自重" not in result["equipment"]
        if requested:
            key = next(iter(requested))
            for word, special in (("上胸", "upper_chest"), ("下胸", "lower_chest"), ("内收", "inner_thigh"), ("侧弓步", "lateral_leg"), ("臀外侧", "outer_hip"), ("核心抗旋转", "core_rotation"), ("分腿蹲", "split_squat")):
                if word in focus:
                    key = special
            if key == "arms":
                if "二头" in focus and "三头" not in focus:
                    key = "biceps"
                elif "三头" in focus and "二头" not in focus:
                    key = "triceps"
            framework = read_systems()[0].get(request.get("split") or profile["training_split"], {})
            selected = session(key, exercises, resources, evidence, profile, set(history["recent_loads"]),
                               minutes, request.get("replacements"), only_device, working_sets=framework.get("working_sets"))
        structure, selected = today(facts, exercises, resources, evidence, history, selected, minutes)
        result["structure"] = structure
        if request.get("weekly"):
            result["warnings"].append("当前只安排本次训练，不预排未来七天；下次会重新读取实际完成记录。")
        if selected is None:
            return stop(structure["message"], structure["status"])
        result["exercises"] = attach_guidance(selected["exercises"], facts.get("performance_history", []), facts["day"], evidence)
        result["gaps"] = selected["gaps"]
        result["estimated_minutes"] = selected["estimated_minutes"]
        result["target_count"] = selected["target_count"]
        result["focus"] = next(iter(requested)) if requested else selected["key"]
        result["title"] = f"本次建议：{selected['title']}"
        time_source = "档案中每次可用" if basis == "profile_session" else "本次可用"
        result["message"] = (f"{time_source}{minutes}分钟；以下力量组合预计约{selected['estimated_minutes']}分钟，含热身、放松各约5分钟及组间休息。"
                             if selected["exercises"] else "近期实际负荷或当前条件不适合追加这组力量训练，未强行安排。")
        if selected["exercises"]:
            result["message"] += selected["volume_note"]
        if selected["exercises"] and minutes - selected["estimated_minutes"] >= 10:
            result["message"] += f"剩余约{minutes - selected['estimated_minutes']}分钟未安排；按当前方向、经验和可用动作选择，不为凑满时间增加组数。"
        result["warnings"].append("每组间先留约2分钟休息，按呼吸与动作控制情况延长；时间只是排程预算，不必赶进度。")
    else:
        result["title"] = "本次有氧备选"
        result["message"] = "以下选一项即可，不需要全部完成。"
    remaining = minutes
    result["aerobic_is_alternative"] = bool(result["exercises"])
    main = min(20 if profile["experience"] == "beginner" else 40, remaining - 10)
    if main >= 5:
        options = []
        if resources & {"outdoors", "treadmill"}:
            options.append({"id": "walk", "name": "平地步行", "condition": "安全平坦路线或可用跑步机，能舒适交谈的节奏。"})
        if "cycle" in resources:
            options.append({"id": "cycle", "name": "平地骑行", "condition": "有安全路线及骑行能力，佩戴适当防护。"})
        if "pool" in resources:
            options.append({"id": "swim", "name": "休闲游泳", "condition": "已会游泳且有开放、有人值守的安全泳池；不会游泳不要采用。"})
        activity = request.get("activity")
        if activity and activity != "either":
            options = [item for item in options if item["id"] == activity]
        result["aerobic_options"] = [{**item, "minutes": main, "total_minutes": main + 10,
                                       "preparation": "另留约5分钟逐渐开始、5分钟缓和结束"} for item in options]
    if request.get("kind") == "aerobic" and not result["aerobic_options"]:
        return stop("尚无匹配本次场地、项目和时长的有氧选项。步行需明确安全路线或跑步机；游泳需泳池。不用其他项目偷偷替代。")
    if not result["aerobic_options"] and result["exercises"]:
        result["warnings"].append("本次时间或明确场地不足以另加有氧，未强行追加。")
    listed = result["exercises"]
    keys = [*REQUIRED, *result.get("structure", {}).get("source_ids", []), *[item["source_id"] for item in listed],
            *[item["load_guidance"]["source_id"] for item in result["exercises"] if item.get("load_guidance", {}).get("source_id")],
            *[other["source_id"] for item in listed for other in item.get("alternatives", [])]]
    result["sources"] = [evidence[key] for key in dict.fromkeys(keys) if key in evidence]
    result["status"] = "ready" if result["exercises"] or result["aerobic_options"] else "needs_input"
    return result


class TrainingRecommendationService:
    def __init__(self, database, user_id, retriever):
        self.database, self.user_id, self.retriever = database, user_id, retriever
        self.coach = CoachService(database, user_id)

    def evidence(self):
        try:
            exercises, _ = read_catalog()
            systems, _ = read_systems()
            keys = list(dict.fromkeys([*REQUIRED, PROGRESSION_SOURCE, *[item["source"] for item in exercises],
                *[key for system in systems.values() for key in system["source_ids"]]]))
            found = {}
            for start in range(0, len(keys), 20):
                found.update({hit.chunk_id: hit.model_dump(mode="json") for hit in
                              self.retriever.get_chunks(keys[start:start + 20], topic="training")})
            return found
        except KnowledgeUnavailable:
            return {}

    def snapshot(self, connection, conversation_id, evidence):
        row = self.coach.owned(connection, conversation_id)
        data = self.coach.view(connection, row)
        facts = {"day": data["day"], "version": data["version"], "pending": data["pending"], "intent": data["intent"],
                 "profile": data["context"]["profile"], "constraints": data["constraints"],
                 "request": data["training_context"], "history": recent_training(connection, self.user_id, data["day"]),
                 "performance_history": performance_history(connection, self.user_id, data["day"])}
        _, catalog_hash = read_catalog()
        _, systems_hash = read_systems()
        return facts, digest({"facts": facts, "evidence": evidence, "policy": POLICY, "catalog": catalog_hash, "systems": systems_hash})

    def generate(self, conversation_id, body):
        evidence = self.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts, fingerprint = self.snapshot(connection, conversation_id, evidence)
            if facts["pending"] or facts["intent"] != "training" or facts["version"] != body.version:
                raise ModelError("TRAINING_CONTEXT_CHANGED", "对话已变化或补充尚待核对，请先刷新对话。", 409)
            row = connection.execute("SELECT response FROM coach_turns WHERE conversation_id=? AND version=?", (str(conversation_id), body.version)).fetchone()
            response = json.loads(row[0])
            previous = response.get("training_recommendation")
            if previous and previous["client_id"] == str(body.client_id):
                if previous["context_hash"] != fingerprint:
                    raise ModelError("TRAINING_CONTEXT_CHANGED", "档案、实际记录或依据已变化，请按最新情况重新建议。", 409)
                return
            response["training_recommendation"] = {**build(facts, evidence), "client_id": str(body.client_id),
                "context_hash": fingerprint, "generated_at": datetime.now(timezone.utc).isoformat(), "policy": POLICY}
            connection.execute("UPDATE coach_turns SET response=? WHERE conversation_id=? AND version=?", (encode(response), str(conversation_id), body.version))

    def decorate(self, data):
        if not any("training_recommendation" in turn["response"] for turn in data["turns"]):
            return data
        evidence = self.evidence()
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            _, fingerprint = self.snapshot(connection, data["id"], evidence)
        for turn in data["turns"]:
            recommendation = turn["response"].get("training_recommendation")
            if recommendation:
                recommendation["stale"] = recommendation["context_hash"] != fingerprint
        return data
