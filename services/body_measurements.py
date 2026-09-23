"""Private dated measurements and a descriptive review of saved facts only."""
import json
import sqlite3
from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from services.nutrition_totals import nutrition_totals
from services.workout_totals import workout_totals
from services.body_state import sync_profile, current_body, utc_now


def serialized(model, *, exclude=None):
    return json.dumps(model.model_dump(mode="json", exclude=exclude or set()),
                      ensure_ascii=False, sort_keys=True)


def metric_summary(records, key):
    points = [{"day": row["day"], "value": row[key]} for row in records if row[key] is not None]
    change = (float(Decimal(str(points[-1]["value"])) - Decimal(str(points[0]["value"])))
              if len(points) >= 2 else None)
    return {"count": len(points), "first": points[0] if points else None,
            "last": points[-1] if points else None, "change": change}


class BodyMeasurementService:
    def __init__(self, database, user_id):
        self.database = database
        self.user_id = user_id

    @staticmethod
    def decode(row):
        return {"id": row["id"], "version": row["version"], **json.loads(row["payload"])}

    def owned(self, connection, record_id):
        row = connection.execute("SELECT * FROM body_measurements WHERE id=? AND user_id=?",
                                 (record_id, self.user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "体测记录不存在")
        return row

    def create(self, body):
        payload = serialized(body, exclude={"client_id"})
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM body_measurements WHERE user_id=? AND client_id=?",
                                     (self.user_id, str(body.client_id))).fetchone()
            if old:
                if old["deleted"] or old["initial_payload"] != payload or old["payload"] != payload:
                    raise HTTPException(409, "该提交已使用或记录已变化，请刷新核对；不会恢复旧内容")
                return self.decode(old)
            try:
                cursor = connection.execute(
                    "INSERT INTO body_measurements(user_id,client_id,day,initial_payload,payload,updated_at) VALUES(?,?,?,?,?,?)",
                    (self.user_id, str(body.client_id), body.day.isoformat(), payload, payload, utc_now()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "这一天已有体测记录，请编辑已有记录") from None
            sync_profile(connection, self.user_id)
            return self.decode(self.owned(connection, cursor.lastrowid))

    def update(self, record_id, body):
        payload = serialized(body, exclude={"version"})
        request = serialized(body)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self.owned(connection, record_id)
            if old["deleted"]:
                raise HTTPException(404, "体测记录已删除")
            if old["last_request"] == request:
                return self.decode(old)
            if old["version"] != body.version:
                raise HTTPException(409, "体测记录已在别处修改，请刷新核对后再编辑")
            try:
                connection.execute(
                    "UPDATE body_measurements SET day=?,payload=?,version=version+1,last_request=?,"
                    "updated_at=? WHERE id=? AND user_id=?",
                    (body.day.isoformat(), payload, request, utc_now(), record_id, self.user_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "目标日期已有体测记录，请核对日期") from None
            sync_profile(connection, self.user_id)
            return self.decode(self.owned(connection, record_id))

    def delete(self, record_id, version):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self.owned(connection, record_id)
            if old["deleted"] and old["version"] == version + 1:
                return
            if old["deleted"] or old["version"] != version:
                raise HTTPException(409, "体测记录已变化，请刷新后再删除")
            # Keep only a request tombstone so a late create/update cannot restore a deletion.
            connection.execute("UPDATE body_measurements SET deleted=1,version=version+1,payload='{}',"
                               "initial_payload='{}',last_request=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                               (record_id, self.user_id))
            sync_profile(connection, self.user_id)

    def review(self, day, days):
        if days not in (30, 90):
            raise HTTPException(422, "回顾范围应为30天或90天")
        start = date.fromordinal(max(date.min.toordinal(), day.toordinal() - days + 1)).isoformat()
        end = day.isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            current = current_body(connection, self.user_id)
            records = [self.decode(row) for row in connection.execute(
                "SELECT * FROM body_measurements WHERE user_id=? AND deleted=0 AND day BETWEEN ? AND ? ORDER BY day,id",
                (self.user_id, start, end))]
            meals = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM meals WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day,id",
                (self.user_id, start, end))]
            workouts = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM workouts WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day,id",
                (self.user_id, start, end))]
        known, estimated = nutrition_totals(meals)
        return {"start": start, "end": end, "days": day.toordinal() - date.fromisoformat(start).toordinal() + 1,
                "records": records,
                "metrics": {key: metric_summary(records, key) for key in ("weight_kg", "body_fat_percent")},
                "review": {"measurement_days": len(records), "meal_days": len({row["day"] for row in meals}),
                           "meal_count": len(meals), "nutrition": known, "estimated_nutrition": estimated,
                           "completed_workout_days": len({row["day"] for row in workouts if row["status"] == "completed"}),
                           **workout_totals(workouts)},
                "basis": "saved_records_only", "profile_sync": True, "current_body": current}
