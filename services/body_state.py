"""Keep measured body values current while preserving a dated manual fallback."""
import json
from datetime import date, datetime, timezone
from services.business_time import business_today

from services.profile_context import read_profile
from services.nutrition_targets import profile_changed

BODY_FIELDS = ("weight_kg", "body_fat_percent")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def project_body(connection, user_id, previous_raw, profile, *, manual=False):
    baselines = dict(previous_raw.get("_body_baselines") or {})
    today, now = business_today().isoformat(), utc_now()
    sources, values = {}, {}
    for key in BODY_FIELDS:
        baseline = baselines.get(key) or {"value": previous_raw.get(key), "day": None, "saved_at": None}
        if manual and profile.get(key) != previous_raw.get(key):
            baseline = {"value": profile.get(key), "day": today, "saved_at": now}
        baselines[key] = baseline
        row = connection.execute(
            f"SELECT id,day,payload,updated_at FROM body_measurements WHERE user_id=? AND deleted=0 "
            f"AND day<=? AND json_extract(payload,'$.{key}') IS NOT NULL ORDER BY day DESC,id DESC LIMIT 1",
            (user_id, today)).fetchone()
        newer = row and (baseline["day"] is None or row["day"] > baseline["day"] or
                         (row["day"] == baseline["day"] and timestamp(row["updated_at"]) > timestamp(baseline["saved_at"])))
        if newer:
            values[key] = json.loads(row["payload"])[key]
            sources[key] = {"source": "measurement", "day": row["day"], "record_id": row["id"]}
        else:
            values[key] = baseline["value"]
            sources[key] = {"source": "profile" if baseline["value"] is not None else "unset", "day": baseline["day"]}
        if key == "weight_kg":
            workout = connection.execute(
                "SELECT id,day,payload,json_extract(payload,'$._body_weight_saved_at') AS saved_at FROM workouts "
                "WHERE user_id=? AND day<=? AND json_extract(payload,'$.status')='completed' "
                "AND json_extract(payload,'$.weight_kg') IS NOT NULL "
                "AND json_extract(payload,'$._body_weight_saved_at') IS NOT NULL "
                "ORDER BY day DESC,saved_at DESC,id DESC LIMIT 1", (user_id, today)).fetchone()
            source_day = row["day"] if newer else baseline["day"]
            source_time = row["updated_at"] if newer else baseline["saved_at"]
            if workout and (source_day is None or workout["day"] > source_day or
                            (workout["day"] == source_day and timestamp(workout["saved_at"]) > timestamp(source_time))):
                values[key] = json.loads(workout["payload"])[key]
                sources[key] = {"source": "workout", "day": workout["day"], "record_id": workout["id"]}
    return {**profile, **values, "_body_baselines": baselines, "_body_sources": sources}


def sync_profile(connection, user_id):
    row = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    raw = json.loads(row[0]) if row else {}
    previous = read_profile(connection, user_id)
    updated = project_body(connection, user_id, raw, previous)
    if updated != raw:
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
                           (user_id, json.dumps(updated, ensure_ascii=False)))
        profile_changed(connection, user_id, previous, updated)


def current_body(connection, user_id):
    row = connection.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    raw = json.loads(row[0]) if row else {}
    sources = raw.get("_body_sources") or {}
    return {key: {"value": raw.get(key), **sources.get(key, {"source": "profile" if raw.get(key) is not None else "unset", "day": None})}
            for key in BODY_FIELDS}
