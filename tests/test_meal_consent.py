import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from test_foundation import DAY, PASSWORD, application, client, register
from test_knowledge import library
from test_meal_plans import plans

URL = "/api/meal-plans/consent"


def confirm(client, **changes):
    status = client.get(URL).json()
    return client.put(URL, json={"context_hash": status["context_hash"], "adult_general_diet": True,
                                 "constraints_reviewed": True, **changes})


def generate(client):
    return client.post("/api/meal-plans", json={"client_id": str(uuid4()), "day": DAY, "meal_type": "dinner"})


def test_login_confirmation_survives_turns_and_restart(plans):
    client, app, model, _ = plans
    assert client.get(URL).json()["confirmed"] is False
    assert generate(client).status_code == 409 and not model.messages
    assert confirm(client).status_code == 200
    first = generate(client)
    assert first.status_code == 200, first.text
    assert generate(client).status_code == 200
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(URL).json()["confirmed"] is True
    assert client.get(f"/api/meals?day={DAY}").json() == []


@pytest.mark.parametrize("field,value", [("food_allergies", "牛肉过敏"), ("preferences", "在外饮食"),
                                         ("preferences", "口味偏甜")])
def test_changed_relevant_profile_requires_review(plans, field, value):
    client, _, model, _ = plans
    old = client.get(URL).json()
    assert confirm(client).status_code == 200
    client.put("/api/profile", json={field: value})
    new = client.get(URL).json()
    assert not new["confirmed"] and new["profile"][field] == value
    assert generate(client).status_code == 409 and not model.messages
    assert confirm(client, context_hash=old["context_hash"]).status_code == 409
    assert confirm(client).status_code == 200


def test_weight_change_does_not_repeat_eligibility(client):
    register(client)
    confirm(client)
    client.put("/api/profile", json={"weight_kg": 80})
    assert client.get(URL).json()["confirmed"]


@pytest.mark.parametrize("changes", [{"adult_general_diet": False}, {"adult_general_diet": 1},
    {"constraints_reviewed": "true"}, {"context_hash": "bad"}, {"user_id": 2}])
def test_explicit_checks_required(client, changes):
    register(client)
    assert confirm(client, **changes).status_code == 422
    assert not client.get(URL).json()["confirmed"]


def test_confirmation_is_login_scoped_and_revocable(client, application):
    assert client.get(URL).status_code == 401
    register(client)
    confirm(client)
    with TestClient(application) as other:
        register(other, "other")
        assert not other.get(URL).json()["confirmed"]
        assert other.delete(URL).status_code == 200
        assert client.get(URL).json()["confirmed"]
        other.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert not other.get(URL).json()["confirmed"]
    client.post("/api/auth/logout", json={})
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM meal_consents").fetchone()[0] == 0
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert not client.get(URL).json()["confirmed"]
    confirm(client)
    assert client.delete(URL).status_code == 200
    assert not client.get(URL).json()["confirmed"]


def test_revocation_during_model_call_discards_draft(plans):
    client, app, model, _ = plans
    confirm(client)
    model.on_call = lambda: client.delete(URL)
    result = generate(client)
    assert result.status_code == 409
    assert client.get(f"/api/meal-plans?day={DAY}").json() == []


def test_v8_upgrade_preserves_data_and_cascades_session(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE meal_consents")
        connection.execute("DROP TABLE coach_reviews")
        connection.execute("DROP TABLE intake_targets")
        connection.execute("DROP TABLE meal_intake_reviews")
        connection.execute("DROP TABLE body_measurements")
        connection.execute("PRAGMA user_version=8")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'test','synthetic')")
        connection.execute("INSERT INTO sessions VALUES('synthetic-session',1,9999999999)")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] for table in tables}
    database.initialize()
    with database.connect() as connection, sqlite3.connect(str(path) + ".pre-v9.bak") as backup:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 8
        for table in tables:
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == before[table]
            assert backup.execute(f"SELECT * FROM {table}").fetchall() == before[table]
        connection.execute("INSERT INTO meal_consents(session_hash,context_hash) VALUES('synthetic-session','synthetic')")
        connection.execute("DELETE FROM sessions")
        assert connection.execute("SELECT COUNT(*) FROM meal_consents").fetchone()[0] == 0
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
