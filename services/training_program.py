"""Evidence-bound sessions; generated suggestions never advance the rotation."""
from services.training_catalog import read_systems

NAMES = {"chest": "胸部", "back": "背部", "legs": "腿臀", "core": "核心", "shoulders": "肩部", "arms": "手臂"}
SPLITS = {"ppl": "推 / 拉 / 腿三分化",
          "four": "胸 / 背 / 腿 / 肩臂四分化", "five": "胸 / 背 / 腿 / 肩 / 手臂五分化"}
SLOTS = {
    "press": ("胸部水平推", ("horizontal-press", "pushup")),
    "incline": ("胸部上斜推", ("incline-press", "incline-pushup")),
    "decline": ("胸部下斜推", ("decline-press",)),
    "fly": ("胸部内收", ("chest-fly",)),
    "row": ("背部水平拉", ("horizontal-pull",)),
    "pulldown": ("背部垂直拉", ("vertical-pull",)),
    "squat": ("腿臀蹲起", ("squat", "lunge")),
    "lunge": ("腿臀分腿蹲", ("lunge",)),
    "hinge": ("臀腿伸髋", ("hip-hinge", "hip-extension", "bridge")),
    "knee": ("腘绳肌屈膝", ("knee-flexion",)),
    "extension": ("股四头肌伸膝", ("knee-extension",)),
    "adduction": ("大腿内收", ("hip-adduction",)),
    "abduction": ("臀外侧外展", ("hip-abduction",)),
    "lateral": ("腿臀侧向支撑", ("lateral-lunge",)),
    "calf": ("小腿提踵", ("calf",)),
    "overhead": ("肩部上推", ("overhead-press",)),
    "side": ("肩部侧抬", ("side-shoulder",)),
    "rear": ("肩后侧", ("rear-shoulder", "face-pull")),
    "biceps": ("二头弯举", ("biceps", "hammer-curl")),
    "triceps": ("三头伸肘", ("triceps",)),
    "core": ("核心稳定", ("anti-extension", "crunch", "anti-lateral")),
    "rotation": ("核心抗旋转", ("anti-rotation",)),
}
SESSIONS = {
    "chest": ("胸部", ["press", "incline", "fly", "decline"]),
    "back": ("背部", ["row", "pulldown", "rear"]),
    "legs": ("腿臀", ["squat", "hinge", "knee", "adduction", "calf", "extension", "lateral"]),
    "shoulders": ("肩部", ["overhead", "side", "rear"]),
    "arms": ("手臂", ["biceps", "triceps"]),
    "arms_core": ("手臂与核心", ["biceps", "triceps", "core"]),
    "core": ("核心", ["core"]),
    "upper_chest": ("胸部上斜推", ["incline"]), "lower_chest": ("胸部下斜推", ["decline"]),
    "inner_thigh": ("大腿内收", ["adduction"]),
    "outer_hip": ("臀外侧", ["abduction"]),
    "split_squat": ("腿臀分腿蹲", ["lunge"]),
    "core_rotation": ("核心抗旋转", ["rotation"]),
    "lateral_leg": ("腿臀侧向支撑", ["lateral"]),
    "biceps": ("二头", ["biceps"]), "triceps": ("三头", ["triceps"]),
    "push": ("推：胸、肩、三头", ["press", "incline", "overhead", "side", "triceps", "fly"]),
    "pull": ("拉：背、肩后侧、二头", ["row", "pulldown", "rear", "biceps"]),
    "lower": ("下肢与核心", ["squat", "hinge", "core", "lunge", "knee", "calf", "adduction", "abduction"]),
    "upper": ("上肢", ["press", "row", "overhead", "pulldown", "biceps", "triceps"]),
    "shoulder_arms": ("肩与手臂", ["overhead", "side", "rear", "biceps", "triceps"]),
    "full": ("全身", ["squat", "press", "row", "hinge", "overhead", "core", "pulldown", "calf"]),
}
ROTATIONS = {"ppl": ["push", "pull", "lower"], "four": ["chest", "back", "lower", "shoulder_arms"],
             "five": ["chest", "back", "lower", "shoulders", "arms_core"]}


def compatible(item, resources, evidence, experience):
    return item["needs"] <= resources and item["source"] in evidence and (experience == "experienced" or item["level"] == "beginner")


def describe(item):
    return {"id": item["id"], "name": item["name"], "cue": item["cue"], "source_id": item["source"],
            "equipment": sorted(item["needs"]), "target": item["target"] or NAMES[item["focus"]]}


def session(key, catalog, resources, evidence, profile, blocked, minutes, replacements=None, only_device=False, planned_primary=(), working_sets=None):
    title, slots = SESSIONS[key]
    chosen, gaps = [], []
    target_count = (4 if minutes >= 35 else 3) if profile["experience"] == "beginner" else (8 if minutes >= 85 else 6)
    working_sets = working_sets or {}
    set_budget = max(0, (minutes - 10) // 3)
    sets_per_action = min(2 if profile["experience"] == "beginner" else 3, set_budget)
    if not sets_per_action:
        return {"key": key, "title": title, "exercises": [], "gaps": ["本次时间不足以安排正式组。"],
                "target_count": target_count, "estimated_minutes": 0, "volume_note": "", "status": "unavailable"}
    max_actions = min(target_count, max(0, (minutes - 10) // (3 * sets_per_action)))
    for slot in slots:
        label, patterns = SLOTS[slot]
        candidates = [item for item in catalog if item["pattern"] in patterns
                      and compatible(item, resources, evidence, profile["experience"])
                      and not item["loads"] & blocked and item["focus"] not in planned_primary
                      and not (only_device and item["needs"] == {"floor"})]
        candidates.sort(key=lambda item: (patterns.index(item["pattern"]), item["needs"] == {"floor"}))
        if not candidates:
            gaps.append(f"{label}：器械、经验、近期负荷或有效依据不足，未用其他目标的动作顶替。")
            continue
        item = candidates[0]
        replacement_id = (replacements or {}).get(item["id"])
        if replacement_id:
            alternative = next((other for other in candidates if other["id"] == replacement_id
                                and other["pattern"] == item["pattern"]), None)
            if not alternative:
                gaps.append(f"{item['name']}：指定替代未通过器械、同模式或负荷检查；原动作也未保留。")
                continue
            item = alternative
        if len(chosen) >= max_actions:
            if max_actions < target_count:
                gaps.append(f"{label}：本次时间不足，未加入。")
            continue
        if item["id"] in {action["id"] for action in chosen}:
            continue
        if 10 + 3 * sets_per_action * (len(chosen) + 1) > minutes:
            gaps.append(f"{label}：本次时间不足，未加入。")
            continue
        alternatives = [describe(other) for other in candidates if other["id"] != item["id"]
                        and other["pattern"] == item["pattern"]]
        target_sets = min(3 if profile["experience"] == "beginner" else 4, working_sets.get(item["pattern"], 3))
        chosen.append({**describe(item), "focus": item["focus"], "loads": sorted(item["loads"]),
                       "pattern": item["pattern"], "sets": sets_per_action, "repetitions": item["unit"],
                       "reference_sets": target_sets, "rest_seconds": 120, "alternatives": alternatives})
    cap = max((item["reference_sets"] for item in chosen), default=sets_per_action)
    budget = max(0, (minutes - 10) // 3 - sum(item["sets"] for item in chosen))
    for _ in range(cap - sets_per_action):
        for item in chosen:
            if budget and item["sets"] < item["reference_sets"]:
                item["sets"] += 1
                budget -= 1
    reduced = any(item["sets"] < item["reference_sets"] for item in chosen)
    volume_note = "以下组数均为正式组，不含热身。"
    if profile["experience"] == "beginner":
        volume_note += "按入门经验，每动作至多3组。"
    if reduced:
        volume_note += "受本次时间限制，部分动作少于结构参考组数；未缩短休息强行塞入。"
    return {"key": key, "title": title, "exercises": chosen, "gaps": gaps, "target_count": target_count,
            "volume_note": volume_note,
            "estimated_minutes": 10 + 3 * sum(item["sets"] for item in chosen) if chosen else 0,
            "status": "partial" if gaps and chosen else "ready" if chosen else "unavailable"}


def today(facts, catalog, resources, evidence, history, selected=None, minutes=None):
    profile, request = facts["profile"], facts["request"]
    split = request.get("split") or profile.get("training_split", "ppl")
    if split not in SPLITS:
        split = "ppl"
    framework = read_systems()[0].get(split)
    result = {"split": split, "title": SPLITS[split], "source_ids": [], "reason": ""}
    if not framework or any(key not in evidence for key in framework["source_ids"]):
        return {**result, "status": "unavailable", "message": "所选训练结构的参考资料缺失、撤回或待复核，暂不生成动作。"}, None
    result["source_ids"] = framework["source_ids"]
    rotation = ROTATIONS[split]
    start = 0
    # Infer the last direction from actual primary muscle groups, never generated plans.
    sessions = history.get("sessions", {})
    if sessions and len(rotation) > 1:
        last_day = max(sessions)
        actual = sessions[last_day]
        focus = {"chest": {"chest"}, "back": {"back"}, "lower": {"legs", "core"},
                 "shoulders": {"shoulders"}, "arms_core": {"arms", "core"},
                 "push": {"chest", "shoulders", "arms"}, "pull": {"back", "shoulders", "arms"},
                 "upper": {"chest", "back", "shoulders", "arms"}, "shoulder_arms": {"shoulders", "arms"}}
        matches = [(sum(actual.get(group, 0) for group in focus[key]) / len(focus[key]), index)
                   for index, key in enumerate(rotation)]
        score, previous = max(matches, key=lambda item: (item[0], -item[1]))
        if score:
            start = (previous + 1) % len(rotation)
            result["reason"] = f"近七天最近一次力量记录在{last_day[5:]}，本次从{SESSIONS[rotation[start]][0]}方向继续核对。"
    if not result["reason"]:
        result["reason"] = "近七天没有可用于轮转的力量记录，从所选结构起点安排。" if not sessions else "按近七天实际训练和本次条件安排。"
    if selected is None:
        only_device = "只用" in (request.get("equipment") or profile["equipment"]) and "自重" not in (request.get("equipment") or profile["equipment"])
        options = [session(rotation[(start + index) % len(rotation)], catalog, resources, evidence, profile,
                           set(history["recent_loads"]), minutes, request.get("replacements"), only_device,
                           working_sets=framework["working_sets"])
                   for index in range(len(rotation))]
        selected = next((option for option in options if option["exercises"]), options[0])
        if selected is not options[0]:
            result["reason"] += "原方向不满足近期负荷或器械条件，改选当前可执行方向。"
    else:
        result["reason"] = "按你本次指定的方向，结合近七天实际训练核对。"
    return {**result, "status": selected["status"]}, selected
