"""Meal quality preferences; never restrictions or a source of nutrition values."""
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json

from services.plan_foods import ALIASES, FOODS

POLICY = "meal-quality-v1"
STAPLE_PORTIONS = {key: (60 if key in {"oats", "buckwheat", "quinoa", "wholemeal-bread"} else
                        250 if key in {"potato", "sweet-potato"} else 200)
                   for key, food in FOODS.items() if food[1] == "starch"}
RICE_FAMILY = {"rice", "brown-rice"}


def recent_foods(connection, user_id, day):
    day = date.fromisoformat(str(day))
    rows = connection.execute("SELECT id,day,payload FROM meals WHERE user_id=? AND day>=? AND day<=? ORDER BY day,id",
                              (user_id, (day - timedelta(days=6)).isoformat(), day.isoformat())).fetchall()
    daily = defaultdict(set)
    evidence = []
    for row in rows:
        meal = json.loads(row["payload"])
        name = meal.get("name", "")
        matches = {key for key, food in FOODS.items() if food[0] in name}
        for word, keys in ALIASES.items():
            if len(word) > 1 and len(keys) <= 2 and word in name:
                matches.update(keys)
        daily[row["day"]].update(matches)
        evidence.append((row["id"], row["day"], name))
    counts = defaultdict(float)
    for recorded_day, keys in daily.items():
        for key in keys:
            counts[key] += 1 / (1 + (day - date.fromisoformat(recorded_day)).days)
    return {"policy": POLICY, "counts": {key: round(value, 3) for key, value in sorted(counts.items())},
            "fingerprint": hashlib.sha256(json.dumps(evidence, ensure_ascii=False).encode()).hexdigest()}


def quality(plans, info, data):
    from services.meal_nutrient_reference import NAMES, amount_nutrients
    totals = {key: sum(info["recorded"][key].values()) / 2 for key in NAMES}
    cost, seen = 0., defaultdict(int)
    counts = info.get("food_history", {}).get("counts", {})
    for plan in plans:
        starch = 0.
        rice_count = 0
        for item in plan["items"]:
            key = item["food_id"]
            amount = (item["lower"] + item["upper"]) / 2
            for nutrient, value in amount_nutrients(key, amount, data).items():
                totals[nutrient] += value
            if key in STAPLE_PORTIONS:
                starch += amount / STAPLE_PORTIONS[key]
            rice_count += key in RICE_FAMILY
            if FOODS[key][1] in {"protein", "vegetable"}:
                cost += counts.get(key, 0) * .35 + seen[key] * 1.5
                seen[key] += 1
        cost += max(0, starch - 2) * 3 + max(0, rice_count - 1) * 2
    for key, target in info["macro"]["center"].items():
        cost += ({"kcal": 10, "protein": 15, "carbs": 8, "fat": 8}[key]
                 * abs(totals[key] - target) / max(target, 10))
    return cost
