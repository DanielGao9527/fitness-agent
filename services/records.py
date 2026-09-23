import json
from datetime import date

from fastapi import HTTPException

from database import Database
from schemas import Profile
from services.nutrition import NUTRIENTS, NutritionPreviewStore, food_identity
from services.workouts import WorkoutPreviewStore, workout_identity
from services.intake_targets import intake_comparison, target_state
from services.nutrition_totals import nutrition_totals
from services.workout_totals import workout_totals
from services.nutrition_targets import profile_changed
from services.meal_nutrient_reference import current_context
from model.factory import ModelError
from services.profile_context import read_profile, ENERGY_FIELDS
from services.body_state import project_body, sync_profile, utc_now
from services.business_time import business_today
from services.training_progression import validate_performance


class RecordService:
    def __init__(self, database: Database, user_id: int):
        self.database = database
        self.user_id = user_id

    def get_profile(self):
        with self.database.connect() as connection:
            return read_profile(connection, self.user_id)

    def save_profile(self, profile: Profile):
        payload = profile.model_dump(mode="json")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = read_profile(connection, self.user_id)
            previous_row = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (self.user_id,)).fetchone()
            raw = json.loads(previous_row[0]) if previous_row else {}
            for key in (*ENERGY_FIELDS, "training_split"):
                if key not in profile.model_fields_set:
                    payload[key] = previous[key]
            payload = project_body(connection, self.user_id, raw, payload, manual=True)
            connection.execute(
                "INSERT INTO profiles(user_id, payload) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload",
                (self.user_id, json.dumps(payload, ensure_ascii=False)),
            )
            profile_changed(connection, self.user_id, previous, payload)
        return {key: payload[key] for key in Profile.model_fields}

    @staticmethod
    def _table(table):
        if table not in ("meals", "workouts"):
            raise ValueError("Invalid record table")
        return table

    @staticmethod
    def _decode(row):
        return {"id": row["id"], **{key: value for key, value in json.loads(row["payload"]).items() if not key.startswith("_")}}

    def list_records(self, table: str, day: date):
        table = self._table(table)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT id, payload FROM {table} WHERE user_id = ? AND day = ? ORDER BY id",
                (self.user_id, day.isoformat()),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def meal_week(self, day: date):
        start = day.toordinal() - day.weekday()
        dates = [date.fromordinal(value).isoformat()
                 for value in range(start, min(date.max.toordinal(), start + 6) + 1)]
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT day,payload FROM meals WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day,id",
                (self.user_id, dates[0], dates[-1]),
            ).fetchall()
        by_day = {value: [] for value in dates}
        for row in rows:
            by_day[row["day"]].append(json.loads(row["payload"]))

        def summarize(items):
            known, estimated = nutrition_totals(items)
            return {"meal_count": len(items), "meal_types": sorted({item["meal_type"] for item in items}),
                    "nutrition": known, "estimated_nutrition": estimated}

        return {"start": dates[0], "end": dates[-1], "selected_day": day.isoformat(),
                "days": [{"day": value, **summarize(by_day[value])} for value in dates],
                "totals": {**summarize([item for items in by_day.values() for item in items]),
                           "recorded_days": sum(bool(items) for items in by_day.values())},
                "basis": "saved_records_only"}

    def workout_week(self, day: date):
        start = day.toordinal() - day.weekday()
        end = min(date.max.toordinal(), start + 6)
        dates = [date.fromordinal(value).isoformat() for value in range(start, end + 1)]
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT day,payload FROM workouts WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day,id",
                (self.user_id, dates[0], dates[-1]),
            ).fetchall()
        by_day = {value: [] for value in dates}
        for row in rows:
            by_day[row["day"]].append(json.loads(row["payload"]))
        days = [{"day": value, **workout_totals(by_day[value])} for value in dates]
        totals = workout_totals([item for items in by_day.values() for item in items])
        return {
            "start": dates[0], "end": dates[-1], "selected_day": day.isoformat(), "days": days,
            "totals": {**totals, "completed_days": sum(item["completed_count"] > 0 for item in days),
                       "planned_days": sum(item["planned_count"] > 0 for item in days)},
            "basis": "saved_records_only",
        }

    def create(self, table: str, model):
        return self.create_many(table, [model])[0]

    def create_many(self, table: str, models):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.create_many_in_transaction(connection, table, models)

    def create_many_in_transaction(self, connection, table: str, models, *, nutrition_estimates=None):
        table = self._table(table)
        records = []
        body_changed = False
        for index, model in enumerate(models):
            payload = model.model_dump(mode="json", exclude={"client_id"})
            if table == "workouts":
                validate_performance(payload)
            if nutrition_estimates and nutrition_estimates[index] is not None:
                payload["nutrition_estimate"] = nutrition_estimates[index]
            if table == "meals" and model.nutrition_preview_id:
                estimate = NutritionPreviewStore.resolve(connection, self.user_id, model.nutrition_preview_id, payload)
                if estimate:
                    payload["nutrition_estimate"] = estimate
            if table == "workouts" and model.calorie_preview_id:
                estimate = WorkoutPreviewStore.resolve(connection, self.user_id, model.calorie_preview_id, payload)
                if estimate:
                    payload["calorie_estimate"] = estimate
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            inserted = connection.execute(
                f"INSERT INTO {table}(user_id, client_id, day, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, client_id) DO NOTHING",
                (self.user_id, str(model.client_id), payload["day"], serialized),
            )
            row = connection.execute(
                f"SELECT id, payload FROM {table} WHERE user_id = ? AND client_id = ?",
                (self.user_id, str(model.client_id)),
            ).fetchone()
            previous = json.loads(row["payload"])
            previous.pop("_body_weight_saved_at", None)
            if table == "meals":
                previous.setdefault("amount_description", "")
            else:
                for key, default in (("intensity", "unknown"), ("details", ""), ("weight_kg", None), ("performance", None)):
                    previous.setdefault(key, default)
            if previous != payload:
                raise HTTPException(409, "这次提交标识已用于其他内容，请重新打开表单")
            if table == "workouts" and inserted.rowcount == 1 and model.sync_weight:
                self._weight_date(model)
                payload["_body_weight_saved_at"] = utc_now()
                connection.execute("UPDATE workouts SET payload=? WHERE id=? AND user_id=?",
                                   (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["id"], self.user_id))
                body_changed = True
            records.append(self._decode(row))
        if body_changed:
            sync_profile(connection, self.user_id)
        return records

    @staticmethod
    def _weight_date(model):
        if model.day > business_today():
            raise HTTPException(422, "未来日期不能作为本次体重同步档案，请核对训练日期。")

    def update(self, table: str, record_id: int, model):
        table = self._table(table)
        payload = model.model_dump(mode="json")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT payload FROM {table} WHERE id = ? AND user_id = ?", (record_id, self.user_id)
            ).fetchone()
            if existing is None:
                raise HTTPException(404, "记录不存在")
            previous = json.loads(existing["payload"])
            if table == "meals" and model.nutrition_preview_id:
                estimate = NutritionPreviewStore.resolve(connection, self.user_id, model.nutrition_preview_id, payload)
                if estimate:
                    payload["nutrition_estimate"] = estimate
            elif (table == "meals" and not model.clear_nutrition_estimate and previous.get("nutrition_estimate")
                    and food_identity(previous) == food_identity(payload)
                    and all(payload[f"{name}_per_100g"] is None for name in NUTRIENTS)):
                payload["nutrition_estimate"] = previous["nutrition_estimate"]
            if table == "workouts":
                unchanged_weight = all(previous.get(key) == payload.get(key) for key in ("weight_kg", "day", "status"))
                if unchanged_weight and previous.get("_body_weight_saved_at"):
                    payload["_body_weight_saved_at"] = previous["_body_weight_saved_at"]
                elif model.sync_weight:
                    self._weight_date(model)
                    payload["_body_weight_saved_at"] = utc_now()
                if "performance" not in model.model_fields_set and previous.get("name") == payload["name"]:
                    payload["performance"] = previous.get("performance")
                validate_performance(payload)
                if model.calorie_preview_id:
                    estimate = WorkoutPreviewStore.resolve(connection, self.user_id, model.calorie_preview_id, payload)
                    if estimate:
                        payload["calorie_estimate"] = estimate
                elif not model.clear_calorie_estimate and previous.get("calorie_estimate") and workout_identity(previous) == workout_identity(payload):
                    payload["calorie_estimate"] = previous["calorie_estimate"]
            result = connection.execute(
                f"UPDATE {table} SET day = ?, payload = ? WHERE id = ? AND user_id = ?",
                (payload["day"], json.dumps(payload, ensure_ascii=False, sort_keys=True), record_id, self.user_id),
            )
            if result.rowcount != 1:
                raise HTTPException(404, "记录不存在")
            if table == "workouts" and (previous.get("_body_weight_saved_at") or payload.get("_body_weight_saved_at")):
                sync_profile(connection, self.user_id)
        return {"id": record_id, **{key: value for key, value in payload.items() if not key.startswith("_")}}

    def delete(self, table: str, record_id: int):
        table = self._table(table)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(f"DELETE FROM {table} WHERE id = ? AND user_id = ?", (record_id, self.user_id))
            if result.rowcount != 1:
                raise HTTPException(404, "记录不存在")
            if table == "workouts":
                sync_profile(connection, self.user_id)

    def summary(self, day: date):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            meals = [self._decode(row) for row in connection.execute(
                "SELECT id,payload FROM meals WHERE user_id=? AND day=? ORDER BY id", (self.user_id, day.isoformat()))]
            workouts = [self._decode(row) for row in connection.execute(
                "SELECT id,payload FROM workouts WHERE user_id=? AND day=? ORDER BY id", (self.user_id, day.isoformat()))]
            target = target_state(connection, self.user_id, day)
            try:
                calculation = current_context(connection, self.user_id, day)
            except ModelError as error:
                calculation = {"ready": False, "reason": "reference_unavailable", "message": str(error)}
        totals, estimates = nutrition_totals(meals)
        return {
            "day": day.isoformat(), "meal_count": len(meals), "nutrition": totals,
            "estimated_nutrition": estimates,
            "intake_target": target,
            "meal_calculation": calculation,
            "intake_comparison": intake_comparison(totals, estimates, len(meals), target),
            **workout_totals(workouts),
        }
