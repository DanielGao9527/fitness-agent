import json
from datetime import datetime, timezone
from uuid import UUID, uuid5

from model.factory import ModelError
from schemas import TrainingExecutionItem, WorkoutCreate
from services.meal_plans import digest, encode
from services.records import RecordService
from services.workouts import WorkoutPreviewStore


class TrainingExecutionService:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id

    def load(self, connection, plan_id):
        row = connection.execute(
            "SELECT * FROM training_plans WHERE id=? AND user_id=?", (plan_id, self.user_id)
        ).fetchone()
        if not row or row["status"] not in ("draft", "accepted"):
            raise ModelError("PLAN_NOT_FOUND", "训练建议不存在或无权访问。", 404)
        return row, json.loads(row["payload"])

    def write(self, connection, plan_id, payload, review):
        payload["execution_review"] = review
        connection.execute("UPDATE training_plans SET payload=? WHERE id=? AND user_id=?",
                           (encode(payload), plan_id, self.user_id))
        return {**review, "plan_id": plan_id}

    @staticmethod
    def conflict():
        return ModelError("EXECUTION_CHANGED", "实际训练草稿已在其他页面更新或入账，请重新打开核对；当前输入未覆盖。", 409)

    def open(self, plan_id):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, payload = self.load(connection, plan_id)
            review = payload.get("execution_review")
            if review and review["status"] != "cancelled":
                return {**review, "plan_id": plan_id}
            names = [payload["activity_name"]]
            if payload["activity_id"] == "cycle":
                names = ["轻松步行热身", payload["activity_name"], "轻松步行收尾"]
            review = {"status": "draft", "version": review["version"] + 1 if review else 1, "day": row["day"], "weight_kg": None,
                      "notes": "", "items": [TrainingExecutionItem(name=name).model_dump(mode="json") for name in names]}
            return self.write(connection, plan_id, payload, review)

    def list(self):
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,payload FROM training_plans WHERE user_id=? AND status IN ('draft','accepted') "
                "AND json_extract(payload,'$.execution_review.status')='draft' ORDER BY rowid DESC LIMIT 30",
                (self.user_id,)
            ).fetchall()
            return [{**json.loads(row["payload"])["execution_review"], "plan_id": row["id"]} for row in rows]

    def discard(self, plan_id, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _, payload = self.load(connection, plan_id)
            review = payload.get("execution_review")
            if review and review["status"] == "cancelled" and review["version"] == body.version + 1:
                return {**review, "plan_id": plan_id}
            if not review or review["status"] != "draft" or review["version"] != body.version:
                raise self.conflict()
            review = {"status": "cancelled", "version": body.version + 1}
            return self.write(connection, plan_id, payload, review)

    def save(self, plan_id, body):
        values = body.model_dump(mode="json", exclude={"version"})
        request_hash = digest(values)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _, payload = self.load(connection, plan_id)
            review = payload.get("execution_review")
            if not review or review["status"] != "draft":
                raise self.conflict()
            if review["version"] == body.version + 1 and review.get("request_hash") == request_hash:
                return {**review, "plan_id": plan_id}
            if review["version"] != body.version:
                raise self.conflict()
            for item in values["items"]:
                if item["calorie_preview_id"]:
                    item["calorie_estimate"] = WorkoutPreviewStore.resolve(
                        connection, self.user_id, item["calorie_preview_id"], {**item, "weight_kg": body.weight_kg})
            review = {**values, "version": body.version + 1, "status": "draft", "request_hash": request_hash}
            return self.write(connection, plan_id, payload, review)

    def commit(self, plan_id, body):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _, payload = self.load(connection, plan_id)
            review = payload.get("execution_review")
            if not review:
                raise self.conflict()
            if review["status"] == "committed" and review.get("committed_version") == body.version:
                return {**review, "plan_id": plan_id}
            if review["status"] != "draft" or review["version"] != body.version:
                raise self.conflict()
            if any(not item["name"].strip() or item["minutes"] is None for item in review["items"]):
                raise ModelError("EXECUTION_INCOMPLETE", "请填写每项实际训练名称和实际分钟数；没有做的项目请移除。", 422)
            models = [WorkoutCreate(
                client_id=uuid5(UUID(plan_id), f"actual-training:{index}"), day=review["day"],
                name=item["name"], minutes=item["minutes"], status="completed", intensity=item["intensity"],
                details=item["details"], weight_kg=review["weight_kg"], notes=review["notes"],
                sync_weight=review.get("sync_weight", False),
                calorie_preview_id=item.get("calorie_preview_id")
            ) for index, item in enumerate(review["items"])]
            # The record writes and terminal marker share one transaction; replay never recreates deleted rows.
            records = RecordService(self.database, self.user_id).create_many_in_transaction(connection, "workouts", models)
            review = {**review, "version": body.version + 1, "status": "committed",
                      "committed_version": body.version, "record_ids": [record["id"] for record in records],
                      "committed_at": datetime.now(timezone.utc).isoformat()}
            return self.write(connection, plan_id, payload, review)
