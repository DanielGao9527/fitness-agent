import json
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import ValidationError

from config import ROOT
from database import Database
from model.factory import ModelError, TextModel
from schemas import DraftConfirm, DraftCreate, DraftUpdate, MealCreate, MealDraft, portion_issue
from services.records import RecordService
from services.nutrition import estimate_foods, food_identity, validate_estimate_items
from services.meal_input import require_single_meal


class MealDraftService:
    """Parses transient drafts only; has no database or record-writing tools."""

    def __init__(self, model: TextModel):
        self.model = model

    def parse_text(self, text: str) -> MealDraft:
        require_single_meal(text)
        prompt = (ROOT / "prompts" / "meal_text.md").read_text(encoding="utf-8")
        prompt += "\nJSON Schema:\n" + json.dumps(MealDraft.model_json_schema(), ensure_ascii=False)
        raw = self.model.generate(system_prompt=prompt, message=text)
        try:
            draft = MealDraft.model_validate_json(raw)
            if draft.input_type != "text":
                raise ValueError
        except (ValidationError, ValueError):
            raise ModelError("MODEL_INVALID_OUTPUT", "解析结果未通过检查，请修改描述后重试；未保存记录。") from None
        if not draft.items and not draft.questions:
            draft.questions = ["请描述已经吃过的食物和份量。"]
        else:
            issues = [portion_issue(item.name, item.grams, item.amount_description) for item in draft.items]
            required = list(dict.fromkeys(issue for issue in issues if issue))
            draft.questions = required[:10] if required else draft.questions[:10]
        return draft


class MealDraftStore:
    def __init__(self, database: Database, user_id: int):
        self.database = database
        self.user_id = user_id

    def _read(self, connection, draft_id):
        row = connection.execute(
            "SELECT * FROM meal_drafts WHERE id = ? AND user_id = ?", (str(draft_id), self.user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "草稿不存在")
        return row

    @staticmethod
    def _view(row):
        return {
            "id": row["id"], "day": row["day"], "meal_type": row["meal_type"],
            "text": row["text"], "version": row["version"], "status": row["status"],
            **json.loads(row["payload"]), "records": json.loads(row["result"]),
        }

    @staticmethod
    def _editable(row, version):
        if row["version"] != version:
            raise HTTPException(409, "草稿已在其他页面更新，请重新打开后核对")
        if row["status"] in ("committed", "cancelled"):
            raise HTTPException(409, "草稿已经结束，不能再次修改或解析")

    @staticmethod
    def _status(items):
        return "ready" if items and all(not portion_issue(item["name"], item.get("grams"), item.get("amount_description", "")) for item in items) else "needs_input"

    def create(self, body: DraftCreate):
        initial = body.model_dump_json(exclude={"client_id"})
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM meal_drafts WHERE user_id = ? AND client_id = ?",
                (self.user_id, str(body.client_id)),
            ).fetchone()
            if existing:
                if existing["initial_payload"] != initial:
                    raise HTTPException(409, "这次提交标识已用于另一份草稿")
                return self._view(existing)
            draft_id = str(uuid4())
            connection.execute(
                "INSERT INTO meal_drafts(id,user_id,client_id,initial_payload,day,meal_type,text,payload,status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (draft_id, self.user_id, str(body.client_id), initial, body.day.isoformat(), body.meal_type,
                 body.text, json.dumps({"items": [], "questions": [], "notes": ""}), "needs_input"),
            )
            return self._view(self._read(connection, draft_id))

    def list_active(self):
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM meal_drafts WHERE user_id = ? AND status IN ('needs_input','ready') "
                "ORDER BY updated_at DESC, rowid DESC", (self.user_id,),
            ).fetchall()
            return [self._view(row) for row in rows]

    def get(self, draft_id):
        with self.database.connect() as connection:
            return self._view(self._read(connection, draft_id))

    def parse(self, draft_id, version: int, model_factory):
        with self.database.connect() as connection:
            row = self._read(connection, draft_id)
            self._editable(row, version)
            text = row["text"]
            require_single_meal(text, row["meal_type"])
        # Never hold a database lock during the external request. Stale results cannot overwrite edits.
        parsed = MealDraftService(model_factory()).parse_text(text)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            self._editable(row, version)
            payload = json.loads(row["payload"])
            payload.pop("nutrition", None)
            payload.update(parsed.model_dump(mode="json", include={"input_type", "items", "questions"}))
            self._write(connection, row, payload, row["day"], row["meal_type"])
            return self._view(self._read(connection, draft_id))

    def estimate_nutrition(self, draft_id, version, model_factory, model_name):
        with self.database.connect() as connection:
            row = self._read(connection, draft_id)
            self._editable(row, version)
            require_single_meal(row["text"], row["meal_type"])
            payload = json.loads(row["payload"])
            validate_estimate_items(payload["items"], limit=30)
            previous = payload.get("nutrition", {})
            cached = {item["index"]: item for item in previous.get("items", []) if item["status"] == "estimated"}
            missing = [index for index in range(len(payload["items"])) if index not in cached]
            if not missing:
                return self._view(row)
        nutrition = estimate_foods([payload["items"][index] for index in missing], model_factory, model_name)
        for item in nutrition["items"]:
            index = missing[item["index"]]
            cached[index] = {**item, "index": index}
        nutrition["items"] = [cached[index] for index in sorted(cached)]
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            self._editable(row, version)
            payload = json.loads(row["payload"])
            payload["nutrition"] = nutrition
            self._write(connection, row, payload, row["day"], row["meal_type"])
            return self._view(self._read(connection, draft_id))

    def clear_nutrition(self, draft_id, version):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            self._editable(row, version)
            payload = json.loads(row["payload"])
            if payload.pop("nutrition", None) is not None:
                self._write(connection, row, payload, row["day"], row["meal_type"])
            return self._view(self._read(connection, draft_id))

    def _write(self, connection, row, payload, day, meal_type):
        connection.execute(
            "UPDATE meal_drafts SET payload=?, day=?, meal_type=?, status=?, version=version+1, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
            (json.dumps(payload, ensure_ascii=False), day, meal_type,
             self._status(payload["items"]), row["id"], self.user_id),
        )

    def update(self, draft_id, body: DraftUpdate):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            self._editable(row, body.version)
            payload = json.loads(row["payload"])
            updated_items = [item.model_dump(mode="json") for item in body.items]
            if body.text is not None and body.text != row["text"]:
                payload.pop("nutrition", None)
            elif payload.get("nutrition"):
                nutrition = payload["nutrition"]
                nutrition["items"] = [{**item, "model": item.get("model", nutrition["model"]),
                                       "generated_at": item.get("generated_at", nutrition["generated_at"])}
                    for item in nutrition["items"] if item["index"] < len(updated_items)
                    and food_identity(payload["items"][item["index"]]) == food_identity(updated_items[item["index"]])]
                if not nutrition["items"]:
                    payload.pop("nutrition", None)
            payload.update(body.model_dump(mode="json", include={"items", "notes"}))
            if body.text is not None and body.text != row["text"]:
                payload["questions"] = []
                connection.execute(
                    "UPDATE meal_drafts SET text=? WHERE id=? AND user_id=?",
                    (body.text, str(draft_id), self.user_id),
                )
            self._write(connection, row, payload, body.day.isoformat(), body.meal_type)
            return self._view(self._read(connection, draft_id))

    def cancel(self, draft_id, version: int):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            if row["status"] == "cancelled":
                return self._view(row)
            self._editable(row, version)
            connection.execute(
                "UPDATE meal_drafts SET status='cancelled', version=version+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND user_id=?", (str(draft_id), self.user_id),
            )
            return self._view(self._read(connection, draft_id))

    def confirm(self, draft_id, body: DraftConfirm):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            if (row["status"] == "committed" and row["confirmation_id"] == str(body.confirmation_id)
                    and row["committed_version"] == body.version):
                return self._view(row)
            self._editable(row, body.version)
            payload = json.loads(row["payload"])
            if self._status(payload["items"]) != "ready":
                raise HTTPException(422, "请补充具体食物和基本份量，再确认保存")
            require_single_meal(row["text"], row["meal_type"])
            models = [MealCreate(
                client_id=uuid5(UUID(row["id"]), str(index)), day=row["day"], meal_type=row["meal_type"],
                name=item["name"], grams=item["grams"], amount_description=item.get("amount_description", ""), notes=payload["notes"],
            ) for index, item in enumerate(payload["items"])]
            nutrition = payload.get("nutrition")
            estimates = None
            if nutrition:
                metadata = {key: nutrition[key] for key in ("source_type", "model", "generated_at")}
                by_index = {item["index"]: item for item in nutrition["items"]}
                estimates = [{**metadata, **{key: value for key, value in by_index[index].items() if key != "index"}}
                             if index in by_index and by_index[index]["status"] == "estimated" else None
                             for index in range(len(models))]
            records = RecordService(self.database, self.user_id).create_many_in_transaction(
                connection, "meals", models, nutrition_estimates=estimates)
            connection.execute(
                "UPDATE meal_drafts SET status='committed', version=version+1, confirmation_id=?, "
                "committed_version=?, result=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                (str(body.confirmation_id), body.version, json.dumps(records, ensure_ascii=False), str(draft_id), self.user_id),
            )
            return self._view(self._read(connection, draft_id))
