import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from schemas import WorkoutCalorieResponse, WorkoutExtraction, WorkoutParsed

PREVIEW_SECONDS = 7 * 24 * 60 * 60
_NUMBER = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百半]+)"
_DURATION = re.compile(
    rf"(?<![\d.点:：])(?:(?P<hours>{_NUMBER})\s*(?:个)?(?:小时|hours?\b)"
    rf"(?:\s*(?P<extra>{_NUMBER})\s*(?:分钟|分|minutes?\b|min\b))?"
    rf"|(?P<minutes>{_NUMBER})\s*(?:分钟|分(?!化|组)|minutes?\b|min\b))", re.I
)


def _simple_number(text):
    if text is None:
        return 0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    digits = dict(zip("零一二两三四五六七八九", (0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if text == "半":
        return 0.5
    if text in digits:
        return digits[text]
    if text.count("十") == 1:
        tens, ones = text.split("十")
        if tens in digits or not tens:
            if ones in digits or not ones:
                return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    return None


def duration_values(text):
    values = []
    for match in _DURATION.finditer(text):
        hours, extra, minutes = (_simple_number(match.group(key)) for key in ("hours", "extra", "minutes"))
        values.append(hours * 60 + extra + minutes if all(value is not None for value in (hours, extra, minutes)) else None)
    return values


def validate_time_sources(text, result):
    cursor = 0
    for item in result.items:
        start = text.find(item.source_text, cursor)
        if start < 0:
            raise ModelError("MODEL_INVALID_OUTPUT", "训练条目无法对应原话，原描述已保留；请重试解析或手动核对。")
        cursor = start + len(item.source_text)
        durations = list(_DURATION.finditer(item.source_text))
        # This is a bounded evidence check, not a second natural-language parser.
        if len(durations) > 1:
            raise ModelError("MODEL_INVALID_OUTPUT", "解析合并了多个时长，未接收该结果。原描述已保留，请重试或分项手动填写。")
        if item.minutes is not None and durations:
            match = durations[0]
            hours, extra, minutes = (_simple_number(match.group(key)) for key in ("hours", "extra", "minutes"))
            if all(value is not None for value in (hours, extra, minutes)) and item.minutes != hours * 60 + extra + minutes:
                raise ModelError("MODEL_INVALID_OUTPUT", "解析时长与原话不一致，未接收该结果。原描述已保留，请重试或手动核对。")


def workout_identity(item):
    return {"name": item["name"], "minutes": item["minutes"], "weight_kg": item.get("weight_kg"),
            "intensity": item.get("intensity", "unknown"), "details": item.get("details", "")}


def model_json(model, prompt_name, schema, message):
    prompt = (ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
    prompt += "\nJSON Schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    raw = model.generate(system_prompt=prompt, message=message)
    try:
        return schema.model_validate_json(raw)
    except ValidationError as error:
        if issubclass(schema, WorkoutParsed):
            labels = {"minutes": "时长", "status": "完成状态", "intensity": "强度", "name": "项目名称"}
            fields = list(dict.fromkeys(labels[part] for issue in error.errors(include_input=False)
                                        for part in issue["loc"] if part in labels))
            detail = f"（{'、'.join(fields)}字段）" if fields else ""
            message = f"模型返回的训练格式异常{detail}，原描述已保留。可重试解析，或切换手动填写。"
        else:
            labels = {"items": "项目列表", "index": "项目编号", "status": "估算状态", "kcal": "热量范围",
                      "lower": "下界", "upper": "上界", "assumptions": "估算假设", "question": "补问"}
            fields = list(dict.fromkeys(labels[part] for issue in error.errors(include_input=False)
                                        for part in issue["loc"] if part in labels))
            detail = "、".join(fields) if fields else "结果结构或状态与数值关系"
            message = f"消耗估算未通过检查（{detail}），没有接受这次估算；训练内容已保留，可重试估算或按消耗未知保存。"
        raise ModelError("MODEL_INVALID_OUTPUT", message) from None


def parse_workout_text(text, model):
    extracted = model_json(model, "workout_text.md", WorkoutExtraction, text)
    validate_time_sources(text, extracted)
    result = WorkoutParsed.model_validate(extracted.model_dump(exclude={"items": {"__all__": {"source_text"}}}))
    missing = []
    for item in result.items:
        if item.minutes is None:
            status = "，并确认是否已完成" if item.status == "unknown" else ""
            missing.append(f"请补充{item.name}的总时长（分钟）{status}。")
        elif item.status == "unknown":
            missing.append(f"请确认{item.name}是否已完成。")
    # Required fields are authoritative; optional model follow-ups must not block recording.
    if result.items:
        result.questions = missing[:10]
    if not result.items and not result.questions:
        result.questions = ["请描述训练项目、时长及是否已经完成。"]
    return result


class WorkoutPreviewStore:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id

    @staticmethod
    def _view(row):
        return {"id": row["id"], "workout": json.loads(row["input_payload"]), "estimate": json.loads(row["payload"])}

    def create_many(self, bodies, model_factory, model_name):
        inputs = [workout_identity(body.model_dump(mode="json")) for body in bodies]
        serialized = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in inputs]
        cached, missing = {}, []
        with self.database.connect() as connection:
            for index, body in enumerate(bodies):
                row = connection.execute("SELECT * FROM workout_previews WHERE user_id=? AND client_id=?",
                                         (self.user_id, str(body.client_id))).fetchone()
                if row:
                    if row["input_payload"] != serialized[index] or row["created_at"] < time.time() - PREVIEW_SECONDS:
                        raise HTTPException(409, "消耗预览已变化或过期，请重新估算")
                    cached[index] = self._view(row)
                else:
                    missing.append(index)
        if not missing:
            return [cached[index] for index in range(len(inputs))]
        model = model_factory((len(missing) + 4) // 5)
        estimates = {}
        for start in range(0, len(missing), 5):
            indices = missing[start:start + 5]
            message = json.dumps({"items": [{"index": i, **inputs[index]} for i, index in enumerate(indices)]}, ensure_ascii=False)
            result = model_json(model, "workout_calories.md", WorkoutCalorieResponse, message)
            if sorted(item.index for item in result.items) != list(range(len(indices))):
                raise ModelError("MODEL_INVALID_OUTPUT", "训练消耗结果不完整，输入已保留。")
            for item in result.items:
                index = indices[item.index]
                # Broad corruption guard, not a claim that every accepted value is physiologically accurate.
                if item.kcal and item.kcal.upper > inputs[index]["minutes"] * 100:
                    raise ModelError("MODEL_INVALID_OUTPUT", "训练消耗超出校验范围，未保存估算。")
                estimates[index] = {**item.model_dump(mode="json", exclude={"index"}),
                                    "source_type": "model_estimate", "basis": "gross_activity",
                                    "model": model_name, "generated_at": datetime.now(timezone.utc).isoformat()}
                if item.status == "estimated" and inputs[index]["intensity"] == "normal_assumed":
                    estimates[index]["assumptions"].insert(0, "未说明强度，按该项目正常强度估算；不是用户报告或实测强度。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM workout_previews WHERE created_at < ?", (int(time.time()) - PREVIEW_SECONDS,))
            for index in missing:
                connection.execute(
                    "INSERT INTO workout_previews(id,user_id,client_id,input_payload,payload,created_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,client_id) DO NOTHING",
                    (str(uuid4()), self.user_id, str(bodies[index].client_id), serialized[index], json.dumps(estimates[index], ensure_ascii=False), int(time.time())),
                )
                row = connection.execute("SELECT * FROM workout_previews WHERE user_id=? AND client_id=?",
                                         (self.user_id, str(bodies[index].client_id))).fetchone()
                if row["input_payload"] != serialized[index]:
                    raise HTTPException(409, "消耗预览已变化，请重新估算")
                cached[index] = self._view(row)
            return [cached[index] for index in range(len(inputs))]

    @staticmethod
    def resolve(connection, user_id, preview_id, payload):
        row = connection.execute("SELECT * FROM workout_previews WHERE id=? AND user_id=?", (str(preview_id), user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "消耗预览不存在，请重新估算")
        if row["created_at"] < time.time() - PREVIEW_SECONDS or json.loads(row["input_payload"]) != workout_identity(payload):
            raise HTTPException(409, "训练参数已变化或预览过期，请重新估算")
        estimate = json.loads(row["payload"])
        return estimate if estimate["status"] == "estimated" else None
