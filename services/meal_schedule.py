"""Bounded meal selection; no inference from clock time or missing records."""
import re

MEAL_NAMES = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
MEAL_ALIASES = {"早餐": "breakfast", "早饭": "breakfast", "早上": "breakfast", "早晨": "breakfast",
                "午餐": "lunch", "午饭": "lunch", "中午": "lunch",
                "晚餐": "dinner", "晚饭": "dinner", "晚上": "dinner", "今晚": "dinner"}
NAMES = "|".join(MEAL_ALIASES)


def selection(message):
    value = message.strip().rstrip("？?。！! ")
    match = re.fullmatch(r"(?:请)?(?:帮我|给我)?(?:安排|规划)?(?:今天的?)?((?:" + NAMES + r")(?:[和、与及](?:" + NAMES + r")){0,2})(?:一起)?(?:安排|怎么吃|吃什么)?", value)
    if not match:
        return None
    found = {MEAL_ALIASES[name] for name in re.findall(NAMES, match[1])}
    return [key for key in MEAL_NAMES if key in found]


def targeted(message):
    value = message.strip().rstrip("？?。！! ")
    match = re.fullmatch(r"(?:请)?(?:把)?(" + NAMES + r")(?:的|[：:，, ])*(.+)", value)
    return (MEAL_ALIASES[match[1]], match[2]) if match else (None, value)


def question(context):
    if context.get("selection_required"):
        return "要安排哪几餐？请确认早餐、午餐或晚餐；未记录不代表还没吃。"
    if context.get("target_required"):
        return "这次要修改哪一餐？请在消息中带上餐次；其他餐不会一起改。"
    return ""


def scoped(context, meal_type):
    return context.get("by_meal", {}).get(meal_type, {}) if "meal_types" in context else context
