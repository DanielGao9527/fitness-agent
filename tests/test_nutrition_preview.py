import json
import sqlite3
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from services.nutrition import PREVIEW_SECONDS
from test_foundation import DAY, application, client, meal, register
from test_nutrition import model


def request(**kwargs):
    return {"name": "Eggs", "grams": None, "amount_description": "three eggs", "client_id": str(uuid4()), **kwargs}


def preview(client, **kwargs):
    body = request(**kwargs)
    result = client.post("/api/nutrition/preview", json=body)
    assert result.status_code == 200, result.text
    return result.json()


def food(preview, **kwargs):
    return meal(**preview["food"], nutrition_preview_id=preview["id"]) | kwargs


def edit_body(row, **kwargs):
    return {key: value for key, value in row.items() if key not in ("id", "nutrition_estimate")} | kwargs


def test_manual_preview_does_not_write_records_and_batch_is_idempotent(client, model, application):
    register(client)
    body = request()
    first = client.post("/api/nutrition/preview", json=body).json()
    assert client.post("/api/nutrition/preview", json=body).json() == first
    assert model.calls == 1
    assert client.get("/api/meal-drafts").json() == []
    assert client.get(f"/api/meals?day={DAY}").json() == []
    batch = {"items": [food(first), meal(name="Unknown")]}
    saved = client.post("/api/meals/batch", json=batch)
    assert saved.status_code == 201
    assert saved.json()[0]["grams"] is None and saved.json()[0]["nutrition_estimate"]["source_type"] == "model_estimate"
    assert "nutrition_preview_id" not in saved.json()[0]
    assert "nutrition_estimate" not in saved.json()[1]
    assert client.post("/api/meals/batch", json=batch).json() == saved.json()
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.post("/api/meals/batch", json=batch).json() == saved.json()


def test_edit_three_to_five_estimates_before_save(client, model):
    register(client)
    first = preview(client)
    row = client.post("/api/meals", json=food(first)).json()
    second = preview(client, amount_description="5 eggs")
    assert client.get(f"/api/meals?day={DAY}").json()[0] == row
    saved = client.put(f"/api/meals/{row['id']}", json=edit_body(row, amount_description="5 eggs", nutrition_preview_id=second["id"]))
    assert saved.status_code == 200
    assert saved.json()["amount_description"] == "5 eggs" and "nutrition_estimate" in saved.json()
    assert client.put(f"/api/meals/{row['id']}", json=edit_body(saved.json(), clear_nutrition_estimate=True)).status_code == 200
    assert "nutrition_estimate" not in client.get(f"/api/meals?day={DAY}").json()[0]


@pytest.mark.parametrize("changes", [{"name": "Rice"}, {"grams": 200}, {"amount_description": "4 eggs"}])
def test_preview_cannot_be_applied_to_changed_food(client, model, changes):
    register(client)
    p = preview(client)
    assert client.post("/api/meals", json=food(p, **changes)).status_code == 409
    assert client.get(f"/api/meals?day={DAY}").json() == []


def test_cross_user_forgery_and_batch_rollback(client, model, application):
    register(client)
    p = preview(client)
    with TestClient(application) as other:
        assert other.post("/api/nutrition/preview", json=request()).status_code == 401
        register(other, "bob")
        assert other.post("/api/meals", json=food(p)).status_code == 404
    batch = {"items": [meal(), food(p, name="Wrong food")]}
    assert client.post("/api/meals/batch", json=batch).status_code == 409
    assert client.get(f"/api/meals?day={DAY}").json() == []
    assert client.post("/api/meals", json=meal(nutrition_estimate={"source_type": "model_estimate"})).status_code == 422
    assert client.post("/api/nutrition/preview", json=request(user_id=999)).status_code == 422


@pytest.mark.parametrize("extra", [{"kcal_per_100g": 100, "source": "Test label"}, {"clear_nutrition_estimate": True}])
def test_estimate_and_manual_values_cannot_be_mixed(client, model, extra):
    register(client)
    p = preview(client)
    assert client.post("/api/meals", json=food(p, **extra)).status_code == 422


def test_expiry_and_cleanup_do_not_change_saved_snapshots(client, model, application):
    register(client)
    p = preview(client)
    saved = client.post("/api/meals", json=food(p)).json()
    with application.state.database.connect() as connection:
        connection.execute("UPDATE nutrition_previews SET created_at=?", (int(time.time()) - PREVIEW_SECONDS - 1,))
    assert client.post("/api/meals", json=food(p)).status_code == 409
    preview(client)
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM nutrition_previews WHERE id=?", (p["id"],)).fetchone()[0] == 0
    assert client.get(f"/api/meals?day={DAY}").json() == [saved]


def test_missing_configuration_input_conflict_and_limits(client, application, model):
    register(client)
    for bad in [request(name="炒菜"), request(amount_description="unknown"), request(grams=0)]:
        assert client.post("/api/nutrition/preview", json=bad).status_code == 422
    assert model.calls == 0
    body = request()
    assert client.post("/api/nutrition/preview", json=body).status_code == 200
    assert client.post("/api/nutrition/preview", json=body | {"name": "Rice"}).status_code == 409
    assert model.calls == 1
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=1)
    assert client.post("/api/nutrition/preview", json=request()).status_code == 429


def test_unknown_preview_clears_existing_estimate(client, model):
    register(client)
    p = preview(client)
    row = client.post("/api/meals", json=food(p)).json()
    model.response = json.dumps({"items": [{"index": 0, "status": "unknown", "question": "How much?"}]})
    unknown = preview(client)
    saved = client.put(f"/api/meals/{row['id']}", json=edit_body(row, nutrition_preview_id=unknown["id"]))
    assert saved.status_code == 200 and "nutrition_estimate" not in saved.json()


def test_v3_upgrade_preserves_rows_and_backup(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE coach_turns")
        connection.execute("DROP TABLE meal_consents")
        connection.execute("DROP TABLE coach_reviews")
        connection.execute("DROP TABLE intake_targets")
        connection.execute("DROP TABLE meal_intake_reviews")
        connection.execute("DROP TABLE body_measurements")
        connection.execute("DROP TABLE coach_conversations")
        connection.execute("DROP TABLE nutrition_previews")
        connection.execute("DROP TABLE workout_previews")
        connection.execute("DROP TABLE meal_plans")
        connection.execute("DROP TABLE training_plans")
        connection.execute("PRAGMA user_version=3")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'fixture','synthetic')")
        connection.execute("INSERT INTO meals(user_id,client_id,day,payload) VALUES (1,'fixture',?,?)", (DAY, '{"legacy":"unchanged"}'))
    database.initialize()
    with database.connect() as connection, sqlite3.connect(path.with_name(path.name + ".pre-v4.bak")) as backup:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert tuple(connection.execute("SELECT * FROM meals").fetchone()) == backup.execute("SELECT * FROM meals").fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_eight_foods_one_batch_two_model_calls_and_changed_item_reuse(client, model, application):
    register(client)
    items = [request(name=f"Eggs {i}") for i in range(8)]
    first = client.post("/api/nutrition/preview-batch", json={"items": items})
    assert first.status_code == 200 and len(first.json()["items"]) == 8
    assert model.calls == 2
    assert client.post("/api/nutrition/preview-batch", json={"items": items}).json() == first.json()
    assert model.calls == 2
    items[0] = request(name="Eggs 0", amount_description="5 eggs")
    changed = client.post("/api/nutrition/preview-batch", json={"items": items}).json()
    assert model.calls == 3
    assert changed["items"][1:] == first.json()["items"][1:]
    saved = client.post("/api/meals/batch", json={"items": [food(p) for p in changed["items"]]})
    assert saved.status_code == 201 and len(saved.json()) == 8
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 3


def test_batch_budget_preflight_and_second_chunk_failure(client, model, application):
    register(client)
    items = [request(name=f"Eggs {i}") for i in range(8)]
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=1)
    assert client.post("/api/nutrition/preview-batch", json={"items": items}).status_code == 429
    assert model.calls == 0
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=10)
    original = model.generate
    def generate(**kwargs):
        if model.calls == 1:
            from model.factory import ModelError
            raise ModelError("MODEL_TIMEOUT", "Synthetic timeout", 504)
        return original(**kwargs)
    model.generate = generate
    assert client.post("/api/nutrition/preview-batch", json={"items": items}).status_code == 504
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM nutrition_previews").fetchone()[0] == 0
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 2


@pytest.mark.parametrize("count", [0, 31])
def test_batch_size_boundaries(client, model, count):
    register(client)
    assert client.post("/api/nutrition/preview-batch", json={"items": [request() for _ in range(count)]}).status_code == 422
    assert model.calls == 0


def test_thirty_foods_reserves_six_calls_and_unique_ids(client, model, application):
    register(client)
    items = [request(name=f"Eggs {i}") for i in range(30)]
    duplicate = [items[0], items[0]]
    assert client.post("/api/nutrition/preview-batch", json={"items": duplicate}).status_code == 422
    result = client.post("/api/nutrition/preview-batch", json={"items": items})
    assert result.status_code == 200 and len(result.json()["items"]) == 30
    assert model.calls == 6
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 6


def test_partial_draft_nutrition_keeps_correct_food_alignment(client, model):
    from test_draft_flow import ready, changes, confirm_body
    from test_nutrition import estimate
    register(client)
    draft = estimate(client, ready(client)).json()
    original = draft["nutrition"]["items"][1]
    items = [dict(item) for item in draft["items"]]
    items[0]["grams"] = 200
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, items=items)).json()
    assert draft["nutrition"]["items"] == [original]
    reestimated = estimate(client, draft).json()
    assert reestimated["nutrition"]["items"][1] == original
    assert model.calls == 2
    items[0]["grams"] = 300
    partial = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(reestimated, items=items)).json()
    saved = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(partial)).json()["records"]
    assert "nutrition_estimate" not in saved[0]
    assert saved[1]["nutrition_estimate"]["generated_at"] == original["generated_at"]
