import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from schemas import NutritionEstimateResponse, portion_issue

NUTRIENTS = ("kcal", "protein", "carbs", "fat")
PREVIEW_SECONDS = 7 * 24 * 60 * 60


def food_identity(item):
    return {"name": item["name"], "grams": item.get("grams"),
            "amount_description": item.get("amount_description", "")}


def validate_estimate_items(items, limit=5):
    if not 1 <= len(items) <= limit:
        raise HTTPException(422, f"每次营养估算支持1至{limit}种食物")
    if any(portion_issue(**food_identity(item)) for item in items):
        raise HTTPException(422, "请先补充具体食物和基本份量，再估算营养")


class NutritionService:
    """Estimates only the submitted portions; no profile, history, or writing tools."""

    def __init__(self, model, model_name):
        self.model = model
        self.model_name = model_name

    def estimate(self, items, *, planned=False):
        validate_estimate_items(items)
        prompt = (ROOT / "prompts" / ("meal_plan_nutrition.md" if planned else "nutrition.md")).read_text(encoding="utf-8")
        prompt += "\nJSON Schema:\n" + json.dumps(NutritionEstimateResponse.model_json_schema(), ensure_ascii=False)
        message = json.dumps({"items": [{"index": index, **food_identity(item)}
                                      for index, item in enumerate(items)]}, ensure_ascii=False)
        raw = self.model.generate(system_prompt=prompt, message=message)
        try:
            result = NutritionEstimateResponse.model_validate_json(raw)
            if sorted(item.index for item in result.items) != list(range(len(items))):
                raise ValueError
            for item in result.items:
                grams = items[item.index].get("grams")
                if item.status == "estimated" and grams is not None:
                    if item.kcal.upper > grams * 10 or sum(getattr(item, name).lower for name in NUTRIENTS[1:]) > grams:
                        raise ValueError
        except (ValidationError, ValueError):
            raise ModelError("MODEL_INVALID_OUTPUT", "营养估算未通过检查，草稿已保留；可修改份量后重试。") from None
        return {
            "source_type": "model_estimate", "model": self.model_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": [item.model_dump(mode="json") for item in sorted(result.items, key=lambda item: item.index)],
        }


def estimate_foods(items, model_factory, model_name, *, planned=False):
    validate_estimate_items(items, limit=30)
    model = model_factory((len(items) + 4) // 5)
    combined = None
    results = []
    for start in range(0, len(items), 5):
        batch = NutritionService(model, model_name).estimate(items[start:start + 5], planned=planned)
        combined = batch
        results.extend({**item, "index": start + item["index"], "model": batch["model"],
                        "generated_at": batch["generated_at"]} for item in batch["items"])
    return {**combined, "items": results}


class NutritionPreviewStore:
    def __init__(self, database, user_id):
        self.database = database
        self.user_id = user_id

    @staticmethod
    def _view(row):
        return {"id": row["id"], "food": json.loads(row["input_payload"]),
                "nutrition": json.loads(row["payload"])}

    def create(self, body, model_factory, model_name):
        return self.create_many([body], model_factory, model_name)[0]

    def create_many(self, bodies, model_factory, model_name):
        foods = [food_identity(body.model_dump(mode="json")) for body in bodies]
        validate_estimate_items(foods, limit=30)
        serialized = [json.dumps(food, sort_keys=True, ensure_ascii=False) for food in foods]
        cached, missing = {}, []
        with self.database.connect() as connection:
            for index, body in enumerate(bodies):
                previous = connection.execute("SELECT * FROM nutrition_previews WHERE user_id=? AND client_id=?",
                                              (self.user_id, str(body.client_id))).fetchone()
                if previous:
                    if previous["input_payload"] != serialized[index] or previous["created_at"] < int(time.time()) - PREVIEW_SECONDS:
                        raise HTTPException(409, "估算预览已变化或过期，请重新估算")
                    cached[index] = self._view(previous)
                else:
                    missing.append(index)
        if not missing:
            return [cached[index] for index in range(len(bodies))]
        nutrition = estimate_foods([foods[index] for index in missing], model_factory, model_name)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM nutrition_previews WHERE created_at < ?", (int(time.time()) - PREVIEW_SECONDS,))
            for offset, index in enumerate(missing):
                item = nutrition["items"][offset]
                snapshot = {**nutrition, "model": item["model"], "generated_at": item["generated_at"], "items": [{**item, "index": 0}]}
                connection.execute(
                    "INSERT INTO nutrition_previews(id,user_id,client_id,input_payload,payload,created_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,client_id) DO NOTHING",
                    (str(uuid4()), self.user_id, str(bodies[index].client_id), serialized[index], json.dumps(snapshot, ensure_ascii=False), int(time.time())),
                )
                row = connection.execute("SELECT * FROM nutrition_previews WHERE user_id=? AND client_id=?",
                                         (self.user_id, str(bodies[index].client_id))).fetchone()
                if row["input_payload"] != serialized[index]:
                    raise HTTPException(409, "估算内容已变化，请重新估算")
                cached[index] = self._view(row)
            return [cached[index] for index in range(len(bodies))]

    @staticmethod
    def resolve(connection, user_id, preview_id, payload):
        row = connection.execute("SELECT * FROM nutrition_previews WHERE id=? AND user_id=?", (str(preview_id), user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "估算预览不存在，请重新估算")
        if row["created_at"] < int(time.time()) - PREVIEW_SECONDS or json.loads(row["input_payload"]) != food_identity(payload):
            raise HTTPException(409, "食物或份量已变化，或预览已过期，请重新估算")
        nutrition = json.loads(row["payload"])
        item = nutrition["items"][0]
        if item["status"] == "unknown":
            return None
        return {**{key: nutrition[key] for key in ("source_type", "model", "generated_at")},
                **{key: value for key, value in item.items() if key != "index"}}
