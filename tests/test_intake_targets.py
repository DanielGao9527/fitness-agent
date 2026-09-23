import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from schemas import IntakeTargetChange
from services.intake_targets import IntakeTargetService
from test_foundation import DAY, PASSWORD, application, client, meal, register, workout
from test_nutrition import result


def change(client, **values):
    current = client.get(f"/api/intake-target?day={DAY}").json()
    return {"client_id": str(uuid4()), "version": current["version"], "context_hash": current["context_hash"],
            "effective_from": DAY, "kcal": 2100, "source": "Synthetic confirmed target", "confirmed": True,
            "general_adult": True, **values}


def save(client, **values):
    return client.post("/api/intake-target", json=change(client, **values))


def summary(client, day=DAY):
    return client.get(f"/api/summary?day={day}").json()


def test_empty_unset_and_zero_nutrition_are_not_conflated(client):
    register(client)
    assert summary(client)["intake_comparison"]["status"] == "unset"
    assert save(client).status_code == 200
    assert summary(client)["intake_comparison"]["status"] == "no_records"
    assert summary(client)["intake_comparison"]["recorded_kcal"] is None
    client.post("/api/meals", json=meal(kcal_per_100g=0, source="Synthetic label"))
    value = summary(client)["intake_comparison"]
    assert value["status"] == "ready" and value["recorded_kcal"] == {"lower": 0, "upper": 0}
    assert value["difference_kcal"] == {"lower": 2100, "upper": 2100}


def test_date_versions_pause_out_of_order_and_replay(client, application):
    register(client)
    original = change(client)
    first = client.post("/api/intake-target", json=original).json()
    assert client.post("/api/intake-target", json=original).json() == first
    assert summary(client, "2026-09-14")["intake_target"]["status"] == "unset"
    assert save(client, effective_from="2026-09-20", kcal=2200).status_code == 200
    assert save(client, effective_from="2026-09-18", kcal=2000).status_code == 200
    assert summary(client)["intake_target"]["target"]["kcal"] == 2100
    assert summary(client, "2026-09-19")["intake_target"]["target"]["kcal"] == 2000
    assert summary(client, "2026-09-21")["intake_target"]["target"]["kcal"] == 2200
    assert save(client, effective_from="2026-09-20", kcal=None, source="").status_code == 200
    assert summary(client, "2026-09-21")["intake_target"]["status"] == "paused"
    client.post("/api/intake-target", json=original)
    assert summary(client, "2026-09-21")["intake_target"]["status"] == "paused"
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 4


def test_conflicting_replay_stale_version_and_stale_profile(client):
    register(client)
    body = change(client)
    assert client.post("/api/intake-target", json=body).status_code == 200
    assert client.post("/api/intake-target", json={**body, "kcal": 2300}).status_code == 409
    assert client.post("/api/intake-target", json={**body, "client_id": str(uuid4())}).status_code == 409
    body = change(client)
    client.put("/api/profile", json={"weight_kg": 75})
    assert client.post("/api/intake-target", json=body).status_code == 409
    assert summary(client)["intake_comparison"]["status"] == "needs_review"
    assert summary(client)["intake_comparison"]["difference_kcal"] is None
    assert save(client).status_code == 200
    assert summary(client)["intake_target"]["status"] == "active"


@pytest.mark.parametrize("values", [
    {"kcal": 999}, {"kcal": 5001}, {"kcal": True}, {"kcal": "2000"}, {"kcal": 2000.5},
    {"source": " "}, {"source": "x" * 201}, {"confirmed": False}, {"confirmed": 1},
    {"general_adult": None}, {"general_adult": False}, {"general_adult": 1},
    {"version": True}, {"version": -1}, {"effective_from": "invalid"}, {"user_id": 2},
    {"context_hash": "wrong"}, {"kcal": None, "source": "not empty"},
])
def test_invalid_targets_do_not_write(client, application, values):
    register(client)
    assert save(client, **values).status_code == 422
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 0


@pytest.mark.parametrize("text", ["孕期", "糖尿病", "16岁", "厌食", "服药期间"])
def test_known_special_diet_context_stops_target_not_records(client, text):
    register(client)
    assert save(client).status_code == 200
    client.put("/api/profile", json={"preferences": text})
    assert save(client).status_code == 409
    assert summary(client)["intake_comparison"]["status"] == "out_of_scope"
    assert client.post("/api/meals", json=meal()).status_code == 201
    assert save(client, kcal=None, source="").status_code == 200


def test_allergy_does_not_change_numeric_target_or_drop_constraint(client):
    register(client)
    client.put("/api/profile", json={"food_allergies": "西兰花过敏"})
    assert save(client).status_code == 200
    assert client.get("/api/profile").json()["food_allergies"] == "西兰花过敏"


def test_private_target_isolation_auth_origin_and_restart(client, application):
    assert client.get(f"/api/intake-target?day={DAY}").status_code == 401
    assert client.post("/api/intake-target", json={}).status_code == 401
    register(client)
    body = change(client, source="Private target source")
    assert client.post("/api/intake-target", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/intake-target", json=body).status_code == 200
    with TestClient(application) as other:
        register(other, "bob")
        assert "Private target source" not in other.get(f"/api/summary?day={DAY}").text
        assert other.get(f"/api/intake-target?day={DAY}").json()["target"] is None
        assert other.post("/api/intake-target", json={**body, "user_id": 1}).status_code == 422
        assert save(other, kcal=2500, client_id=body["client_id"]).status_code == 200
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert summary(restarted)["intake_target"]["target"]["kcal"] == 2100


def test_numeric_summary_uses_range_and_never_adds_exercise(client, application):
    user = register(client)
    save(client)
    known = client.post("/api/meals", json=meal(kcal_per_100g=60, source="Synthetic label")).json()
    estimated = client.post("/api/meals", json=meal(name="Synthetic food")).json()
    with application.state.database.connect() as connection:
        for record in (known, estimated):
            payload = {key: value for key, value in record.items() if key != "id"}
            payload["nutrition_estimate"] = result()
            connection.execute("UPDATE meals SET payload=? WHERE id=? AND user_id=?", (json.dumps(payload), record["id"], user["id"]))
    original = summary(client)["intake_comparison"]
    assert original["recorded_kcal"] == {"lower": 250, "upper": 350}
    assert original["difference_kcal"] == {"lower": 1750, "upper": 1850}
    assert original["estimated_count"] == 1 and not original["exercise_added"]
    row = client.post("/api/workouts", json=workout(status="completed")).json()
    with application.state.database.connect() as connection:
        payload = {key: value for key, value in row.items() if key != "id"}
        payload["calorie_estimate"] = {"kcal": {"lower": 500, "upper": 700}}
        connection.execute("UPDATE workouts SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    assert summary(client)["intake_comparison"] == original
    unknown = client.post("/api/meals", json=meal(name="Unknown food")).json()
    value = summary(client)["intake_comparison"]
    assert value["recorded_kcal"] == original["recorded_kcal"]
    assert value["status"] == "unknown_intake" and value["difference_kcal"] is None
    client.delete(f"/api/meals/{unknown['id']}")
    assert summary(client)["intake_comparison"] == original


@pytest.mark.parametrize("grams,expected", [(2500, {"lower": -400, "upper": -400}),
    (2100, {"lower": 0, "upper": 0}), (2000.25, {"lower": 99.75, "upper": 99.75})])
def test_difference_sign_and_precision(client, grams, expected):
    register(client)
    save(client)
    client.post("/api/meals", json=meal(grams=grams, kcal_per_100g=100, source="Synthetic label"))
    assert summary(client)["intake_comparison"]["difference_kcal"] == expected


def test_concurrent_changes_only_one_wins(client, application):
    user = register(client)
    first, second = change(client), change(client, kcal=2200)
    service = IntakeTargetService(application.state.database, user["id"])
    def submit(body):
        try:
            service.change(IntakeTargetChange(**body))
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, [first, second])) == [200, 409]


def test_v10_upgrade_preserves_all_original_tables_and_backup(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE intake_targets")
        connection.execute("DROP TABLE meal_intake_reviews")
        connection.execute("DROP TABLE body_measurements")
        connection.execute("PRAGMA user_version=10")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'fixture','synthetic')")
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES (1,?)", ('{"weight_kg":72}',))
    database.initialize()
    database.initialize()
    with database.connect() as connection, sqlite3.connect(str(path) + ".pre-v11.bak") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        tables = backup.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables) == 15
        for (table,) in tables:
            assert [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] == backup.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM intake_targets").fetchone()[0] == 0
