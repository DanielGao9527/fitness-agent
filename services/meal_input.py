"""Single-meal recording checks, independent of recommendation meal schedules."""
import re

from fastapi import HTTPException

NAMES = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}
ALIASES = {"早餐": "breakfast", "早饭": "breakfast", "早上": "breakfast", "早晨": "breakfast",
           "午餐": "lunch", "午饭": "lunch", "中午": "lunch", "晚餐": "dinner", "晚饭": "dinner",
           "晚上": "dinner", "今晚": "dinner", "加餐": "snack", "夜宵": "snack"}
MARKER = re.compile("(" + "|".join(ALIASES) + r")(?!肉|奶|饼|麦片)")


def meal_mentions(text):
    matches = list(MARKER.finditer(text))
    found = set()
    for index, match in enumerate(matches):
        clause = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        if re.match(r"[\s:：,，]*(?:我)?(?:没吃|没喝|没有吃|没有喝|未吃|不吃|不喝|想|打算|准备|计划)", clause):
            continue
        found.add(ALIASES[match[1]])
    return [key for key in NAMES if key in found]


def require_single_meal(text, selected=None):
    mentioned = meal_mentions(text)
    if len(mentioned) > 1:
        raise HTTPException(422, "这段描述包含" + "、".join(NAMES[key] for key in mentioned)
                            + "。一次只录一餐，请拆开描述后分别录入；原文字已保留。")
    if mentioned and selected and mentioned[0] != selected:
        raise HTTPException(422, f"描述写的是{NAMES[mentioned[0]]}，当前选择的是{NAMES[selected]}。请先核对餐次或描述，未自动归类。")
