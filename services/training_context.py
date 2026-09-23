"""Private training requests, never actual workouts or exercise prescriptions."""
import json
import re
from datetime import date

SPLIT_ALIASES = {"三分化": "ppl", "四分化": "four", "五分化": "five"}


def training_context(connection, conversation_id):
    rows = connection.execute("SELECT response FROM coach_turns WHERE conversation_id=? ORDER BY version DESC", (conversation_id,)).fetchall()
    for row in rows:
        response = json.loads(row[0])
        if "training_context" in response:
            from services.profile_context import read_profile
            owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (conversation_id,)).fetchone()
            profile = read_profile(connection, owner[0])
            return current_conditions(response["training_context"], profile)
    return {}


def current_conditions(context, profile):
    result = dict(context)
    anchors = dict(result.get("profile_basis", {}))
    for key, field in (("minutes", "minutes_per_session"), ("split", "training_split")):
        if field in anchors and anchors[field] != profile[field]:
            result.pop(key, None)
            anchors.pop(field)
            if key == "minutes":
                result.pop("time_basis", None)
            else:
                result.pop("focus", None)
                result.pop("replacements", None)
    if result.get("split") not in (None, "ppl", "four", "five"):
        result.pop("split")
    if anchors:
        result["profile_basis"] = anchors
    else:
        result.pop("profile_basis", None)
    return result


def recent_training(connection, user_id, day):
    start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 6)).isoformat()
    rows = connection.execute("SELECT day,payload FROM workouts WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day DESC,id DESC", (user_id, start, day)).fetchall()
    completed = []
    for row in rows:
        item = json.loads(row["payload"])
        if item.get("status") == "completed":
            completed.append({"day": row["day"], **{key: item.get(key) for key in ("name", "minutes", "details")}})
    return {"window_start": start, "day": day, "completed": completed}


def training_command(message, continuing=False):
    value = message.strip().rstrip("？?。！! ")
    if value in ("今天练什么", "结合前几天的训练，今天练什么", "结合前几天的训练,今天练什么"):
        return {"kind": "strength", "clear_focus": True, "clear_replacements": True, "reset_legacy_defaults": True}
    commands = {
        "有氧建议": {"kind": "aerobic"}, "安排基础有氧": {"kind": "aerobic"},
        "安排力量训练": {"kind": "strength"}, "力量训练": {"kind": "strength"},
        "不骑车，改成走路": {"kind": "aerobic", "activity": "walk"},
        "不骑车,改成走路": {"kind": "aerobic", "activity": "walk"},
        "改成走路": {"kind": "aerobic", "activity": "walk"},
        "改成步行": {"kind": "aerobic", "activity": "walk"},
        "改成骑车": {"kind": "aerobic", "activity": "cycle"},
        "改成骑行": {"kind": "aerobic", "activity": "cycle"},
    }
    if value in commands:
        return commands[value]
    if value in ("安排本周训练", "安排一周训练", "未来七天怎么练", "本周怎么练"):
        return {"kind": "strength", "weekly": True, "clear_focus": True, "clear_replacements": True}
    if re.fullmatch(r"(?:改成|改为|采用|安排)?(全身训练|上下肢交替|自动安排)", value):
        return {"kind": "strength", "unsupported_split": True}
    split = re.fullmatch(r"(?:改成|改为|采用|安排)?(三分化|四分化|五分化)", value)
    if split:
        return {"kind": "strength", "split": SPLIT_ALIASES[split[1]], "weekly": True, "clear_focus": True}
    focus = re.fullmatch(r"(?:今天|本次|改成|改为|我想|想)?(?:练|训练)(胸|上胸|下胸|背|腿|臀|臀外侧|内收肌|侧弓步|分腿蹲|核心|核心抗旋转|腹|肩|肩部|手臂|二头|三头|二头和三头)", value)
    if focus:
        return {"kind": "strength", "focus": focus[1]}
    if continuing:
        from services.training_catalog import read_catalog
        from services.training_recommendations import equipment
        replacement = re.fullmatch(r"把(.{1,100})换成(.{1,100})", value)
        if replacement:
            items, _ = read_catalog()
            names = {item["name"]: item["id"] for item in items}
            if replacement[1] in names and replacement[2] in names:
                return {"replacements": {names[replacement[1]]: names[replacement[2]]}}
        if value.startswith(("器械改为", "只用", "可用器械")) and len(value) <= 300:
            description = value.removeprefix("器械改为").removeprefix("可用器械").lstrip("：: ")
            if not equipment(description)[1]:
                return {"equipment": description, "clear_replacements": True}
        if value in ("在家，只有哑铃", "在家只有哑铃", "健身房和泳池", "在家，徒手", "户外", "泳池"):
            return {"equipment": value}
        if value == "改成游泳":
            return {"kind": "aerobic", "activity": "swim"}
    if continuing:
        match = re.fullmatch(r"(今天只有|今天总共|全天总共|接下来还有|接下来只有|本次只有|本次)([1-9]\d{0,2})分钟", value)
        if match and int(match[2]) <= 300:
            basis = "unspecified" if match[1] == "今天只有" else "daily" if match[1] in ("今天总共", "全天总共") else "session"
            return {"minutes": int(match[2]), "time_basis": basis}
    return None


def merge_training(context, changes, profile=None):
    result = {**context, **{key: value for key, value in changes.items() if value is not None and key not in ("source_text", "clear_focus", "clear_replacements", "replacements", "reset_legacy_defaults")}}
    anchors = dict(result.get("profile_basis", {}))
    if changes.get("reset_legacy_defaults"):
        if "minutes_per_session" not in anchors:
            result.pop("minutes", None)
            result.pop("time_basis", None)
        if "training_split" not in anchors:
            result.pop("split", None)
    if not changes.get("unsupported_split"):
        result.pop("unsupported_split", None)
    if profile:
        for key, field in (("minutes", "minutes_per_session"), ("split", "training_split")):
            if changes.get(key) is not None:
                anchors[field] = profile[field]
    if changes.get("clear_focus"):
        result.pop("focus", None)
    if changes.get("clear_replacements") or changes.get("focus") or changes.get("split") or changes.get("equipment"):
        result.pop("replacements", None)
    if changes.get("replacements"):
        replacements = dict(result.get("replacements", {}))
        for original, target in changes["replacements"].items():
            root = next((key for key, value in replacements.items() if value == original), original)
            replacements[root] = target
        result["replacements"] = replacements
    if changes.get("minutes") is not None and not changes.get("time_basis"):
        result["time_basis"] = "unspecified"
    if changes.get("kind") == "aerobic":
        for key in ("focus", "split", "weekly", "replacements"):
            result.pop(key, None)
    elif changes.get("kind") == "strength":
        result.pop("activity", None)
    anchors = {field: value for field, value in anchors.items()
               if (field == "minutes_per_session" and "minutes" in result)
               or (field == "training_split" and "split" in result)}
    if anchors:
        result["profile_basis"] = anchors
    else:
        result.pop("profile_basis", None)
    return result


def training_question(context):
    if context.get("minutes") and context.get("time_basis") not in ("daily", "session"):
        return "这段时间是今天总共可用，还是接下来还能练？请在下方核对时间口径，避免重复扣除已经练过的时间。"
    return "根据近期实际完成记录、器械和可用时间整理本次建议；不会把建议当作练过。"
