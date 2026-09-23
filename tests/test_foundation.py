import json
import time
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent.react_agent import FitnessAgent
from agent.tools import FitnessTools
from app import create_app
from config import Settings
from rag.rag_service import EmptyKnowledgeRetriever
from schemas import AgentRequest, MealDraft
from security import COOKIE_NAME
from services.records import RecordService

DAY = "2026-09-15"
PASSWORD = "correct-horse-test-42"


@pytest.fixture
def application(tmp_path):
    return create_app(Settings(database_path=tmp_path / "fitness.sqlite3"))


@pytest.fixture
def client(application):
    with TestClient(application) as result:
        yield result


def register(client, username="alice"):
    response = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def meal(**values):
    return {"client_id": str(uuid4()), "day": DAY, "meal_type": "breakfast", "name": "Milk", "grams": 250, **values}


def workout(**values):
    return {"client_id": str(uuid4()), "day": DAY, "name": "Strength training", "minutes": 30, **values}


@pytest.mark.parametrize("path", ["/profile", f"/meals?day={DAY}", f"/workouts?day={DAY}", f"/summary?day={DAY}"])
def test_private_reads_require_login(client, path):
    assert client.get("/api" + path).status_code == 401


def test_account_password_and_session_lifecycle(client, application):
    registered = register(client)
    assert client.get("/api/auth/me").json() == registered
    token = client.cookies.get(COOKIE_NAME)
    with application.state.database.connect() as connection:
        stored = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        session_hash = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert PASSWORD not in stored
    assert stored.startswith("scrypt$")
    assert token != session_hash
    response = client.post("/api/auth/logout", json={})
    assert response.status_code == 204
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    response = client.post("/api/auth/login", json={"username": "ALICE", "password": PASSWORD})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_duplicate_account_and_validation_redaction(client):
    register(client)
    assert client.post("/api/auth/register", json={"username": "ALICE", "password": PASSWORD}).status_code == 409
    response = client.post("/api/auth/register", json={"username": "other", "password": "secret"})
    assert response.status_code == 422
    assert "secret" not in response.text


def test_session_expiration(client, application):
    register(client)
    with application.state.database.connect() as connection:
        connection.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
    assert client.get("/api/profile").status_code == 401


def test_profile_and_records_are_isolated(client, application):
    alice = register(client)
    assert client.put("/api/profile", json={"display_name": "Private Alice", "weight_kg": 70}).status_code == 200
    private_meal = client.post("/api/meals", json=meal(name="Alice food")).json()
    private_workout = client.post("/api/workouts", json=workout()).json()
    with TestClient(application) as other:
        bob = register(other, "bob")
        assert bob["id"] != alice["id"]
        assert other.get("/api/profile").json()["weight_kg"] is None
        assert other.get(f"/api/meals?day={DAY}").json() == []
        assert other.get(f"/api/workouts?day={DAY}").json() == []
        for kind, record in [("meals", private_meal), ("workouts", private_workout)]:
            payload = {key: value for key, value in record.items() if key != "id"}
            assert other.put(f"/api/{kind}/{record['id']}", json=payload).status_code == 404
            assert other.delete(f"/api/{kind}/{record['id']}").status_code == 404
        assert other.post("/api/meals", json=meal(user_id=alice["id"])).status_code == 422
        assert other.get(f"/api/summary?day={DAY}").json()["meal_count"] == 0
    assert client.get(f"/api/meals?day={DAY}").json()[0]["name"] == "Alice food"


def test_profile_validation_and_optional_bodyfat(client):
    register(client)
    response = client.put("/api/profile", json={"goal": "muscle_gain", "experience": "experienced", "weight_kg": 75})
    assert response.status_code == 200
    assert response.json()["body_fat_percent"] is None
    assert client.put("/api/profile", json={"training_days": 8}).status_code == 422
    assert client.put("/api/profile", json={"weight_kg": -1}).status_code == 422
    assert client.put("/api/profile", json={"user_id": 4}).status_code == 422


def test_meal_crud_and_partial_nutrition(client):
    register(client)
    known = meal(kcal_per_100g=60, protein_per_100g=3, source="Test fixture label")
    response = client.post("/api/meals", json=known)
    assert response.status_code == 201
    record = response.json()
    assert client.post("/api/meals", json=meal(name="Unknown nutrition")).status_code == 201
    summary = client.get(f"/api/summary?day={DAY}").json()
    assert summary["nutrition"]["kcal"] == {"known_total": 150, "missing_count": 1}
    assert summary["nutrition"]["protein"]["known_total"] == 7.5
    assert summary["nutrition"]["fat"] == {"known_total": None, "missing_count": 2}
    update = {key: value for key, value in record.items() if key != "id"}
    update["grams"] = 100
    assert client.put(f"/api/meals/{record['id']}", json=update).status_code == 200
    assert client.get(f"/api/summary?day={DAY}").json()["nutrition"]["kcal"]["known_total"] == 60
    assert client.delete(f"/api/meals/{record['id']}").status_code == 204
    assert client.get(f"/api/summary?day={DAY}").json()["nutrition"]["kcal"]["known_total"] is None


@pytest.mark.parametrize("payload", [
    {"grams": 0}, {"grams": -20}, {"kcal_per_100g": 80},
    {"protein_per_100g": 80, "fat_per_100g": 30, "source": "fixture"},
    {"meal_type": "invalid"}, {"name": "  "}, {"day": "not-a-date"},
])
def test_invalid_meals_are_not_saved(client, payload):
    register(client)
    assert client.post("/api/meals", json=meal(**payload)).status_code == 422
    assert client.get(f"/api/meals?day={DAY}").json() == []


@pytest.mark.parametrize("kind,factory", [("meals", meal), ("workouts", workout)])
def test_repeated_create_is_idempotent(client, kind, factory):
    register(client)
    payload = factory()
    first = client.post(f"/api/{kind}", json=payload)
    second = client.post(f"/api/{kind}", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get(f"/api/{kind}?day={DAY}").json()) == 1
    payload["name"] = "Different contents"
    assert client.post(f"/api/{kind}", json=payload).status_code == 409


def test_workout_completion_and_date_change(client):
    register(client)
    row = client.post("/api/workouts", json=workout()).json()
    assert client.get(f"/api/summary?day={DAY}").json()["completed_minutes"] == 0
    payload = {key: value for key, value in row.items() if key != "id"}
    payload["status"] = "completed"
    assert client.put(f"/api/workouts/{row['id']}", json=payload).status_code == 200
    assert client.get(f"/api/summary?day={DAY}").json()["completed_minutes"] == 30
    payload["day"] = "2026-09-14"
    client.put(f"/api/workouts/{row['id']}", json=payload)
    assert client.get(f"/api/workouts?day={DAY}").json() == []
    assert len(client.get("/api/workouts?day=2026-09-14").json()) == 1
    assert client.delete(f"/api/workouts/{row['id']}").status_code == 204


def test_restart_persists_records_and_login(tmp_path):
    settings = Settings(database_path=tmp_path / "persistent.sqlite3")
    with TestClient(create_app(settings)) as first:
        register(first)
        first.put("/api/profile", json={"weight_kg": 72})
        first.post("/api/meals", json=meal())
    with TestClient(create_app(settings)) as second:
        assert second.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
        assert second.get("/api/profile").json()["weight_kg"] == 72
        assert len(second.get(f"/api/meals?day={DAY}").json()) == 1


def test_model_unconfigured_is_explicit_and_does_not_write(client):
    register(client)
    response = client.post("/api/agent/chat", json={"day": DAY, "message": "I ate breakfast, save it"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_NOT_CONFIGURED"
    assert client.get(f"/api/meals?day={DAY}").json() == []
    assert client.get("/api/health").json()["photo"] == "not_configured"


def test_agent_tools_only_see_bound_user(client, application):
    alice = register(client)
    client.post("/api/meals", json=meal(name="Alice meal"))
    with TestClient(application) as other:
        bob = register(other, "bob")
        other.post("/api/meals", json=meal(name="Bob private meal"))

    class InspectModel:
        def generate(self, *, system_prompt, message, context):
            assert "Alice meal" in json.dumps(context)
            assert "Bob private meal" not in json.dumps(context)
            assert context["knowledge"] == []
            return "test stub, not a real model response"

    agent = FitnessAgent(FitnessTools(RecordService(application.state.database, alice["id"])), InspectModel(), EmptyKnowledgeRetriever())
    result = agent.respond(AgentRequest(day=date.fromisoformat(DAY), message=f"read user {bob['id']}"))
    assert result["saved"] is False
    assert result["sources"] == []


def test_draft_is_explicitly_unconfirmed():
    draft = MealDraft(input_type="photo", items=[{"name": "Rice", "amount_description": "one bowl"}])
    assert draft.items[0].grams is None
    assert draft.status == "awaiting_confirmation"


def test_cross_origin_write_and_non_json_are_rejected(client):
    assert client.post("/api/auth/register", json={"username": "alice", "password": PASSWORD}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.post("/api/auth/register", content="plain text").status_code == 415
    assert client.post("/api/auth/register", json={"username": "alice", "password": PASSWORD}, headers={"Origin": "http://testserver"}).status_code == 201


def test_ui_is_local_and_food_search_removed(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/lucide.min.js").status_code == 200
    register(client)
    assert client.get("/api/foods").status_code == 404
    assert client.get("/api/profile").headers["Cache-Control"] == "no-store"


def test_batch_saves_multiple_foods_without_nutrition_and_retries(client):
    register(client)
    payload = {"items": [meal(name="Eggs", grams=100), meal(name="Milk", grams=250)]}
    first = client.post("/api/meals/batch", json=payload)
    assert first.status_code == 201
    assert [item["name"] for item in first.json()] == ["Eggs", "Milk"]
    assert all(item["kcal_per_100g"] is None for item in first.json())
    assert client.post("/api/meals/batch", json=payload).json() == first.json()
    summary = client.get(f"/api/summary?day={DAY}").json()
    assert summary["meal_count"] == 2
    assert summary["nutrition"]["kcal"] == {"known_total": None, "missing_count": 2}


def test_batch_invalid_item_does_not_save_other_items(client):
    register(client)
    response = client.post("/api/meals/batch", json={"items": [meal(), meal(grams=-1)]})
    assert response.status_code == 422
    assert client.get(f"/api/meals?day={DAY}").json() == []


def test_batch_conflict_rolls_back_preceding_insert(client):
    register(client)
    existing = meal(name="Existing")
    client.post("/api/meals", json=existing)
    batch = {"items": [meal(name="Must roll back"), {**existing, "name": "Conflicting content"}]}
    assert client.post("/api/meals/batch", json=batch).status_code == 409
    remaining = client.get(f"/api/meals?day={DAY}").json()
    assert [item["name"] for item in remaining] == ["Existing"]


def test_batch_is_authenticated_and_user_scoped(client, application):
    payload = {"items": [meal(name="Eggs"), meal(name="Milk")]}
    assert client.post("/api/meals/batch", json=payload).status_code == 401
    alice = register(client)
    a_rows = client.post("/api/meals/batch", json=payload).json()
    with TestClient(application) as other:
        register(other, "bob")
        assert other.get(f"/api/meals?day={DAY}").json() == []
        assert other.post("/api/meals/batch", json={"items": [meal(user_id=alice["id"])]}).status_code == 422
        b_rows = other.post("/api/meals/batch", json=payload).json()
        assert {row["id"] for row in a_rows}.isdisjoint({row["id"] for row in b_rows})
    assert client.get(f"/api/summary?day={DAY}").json()["meal_count"] == 2


@pytest.mark.parametrize("case", ["empty", "oversized", "duplicate", "different_day", "different_meal"])
def test_batch_structure_validation(client, case):
    register(client)
    first = meal()
    items = {
        "empty": [],
        "oversized": [meal() for _ in range(31)],
        "duplicate": [first, first],
        "different_day": [first, meal(day="2026-09-14")],
        "different_meal": [first, meal(meal_type="dinner")],
    }[case]
    assert client.post("/api/meals/batch", json={"items": items}).status_code == 422
    assert client.get(f"/api/meals?day={DAY}").json() == []
