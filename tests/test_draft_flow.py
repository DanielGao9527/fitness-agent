import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from database import Database, SCHEMA
from model.factory import ModelError
from schemas import DraftConfirm, DraftUpdate
from services.meal_drafts import MealDraftStore
from test_foundation import DAY, application, client, meal, register


def create(client, **kwargs):
    response = client.post("/api/meal-drafts", json={
        "client_id": str(uuid4()), "text": "I ate eggs and milk", "day": DAY, "meal_type": "breakfast", **kwargs,
    })
    assert response.status_code == 201, response.text
    return response.json()


def changes(draft, **kwargs):
    return {"version": draft["version"], "day": draft["day"], "meal_type": draft["meal_type"],
            "items": [{"name": "Eggs", "grams": 100}, {"name": "Milk", "grams": 250}], **kwargs}


def ready(client):
    draft = create(client)
    response = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft))
    assert response.status_code == 200
    return response.json()


def confirm_body(draft):
    return {"version": draft["version"], "confirmation_id": str(uuid4())}


def test_draft_requires_login_and_valid_input(client):
    body = {"client_id": str(uuid4()), "day": DAY, "meal_type": "breakfast", "text": "Eggs"}
    assert client.post("/api/meal-drafts", json=body).status_code == 401
    assert client.get("/api/meal-drafts").status_code == 401
    register(client)
    for extra in ({"user_id": 99}, {"text": " "}, {"meal_type": "bad"}, {"text": "x" * 2001}):
        assert client.post("/api/meal-drafts", json={**body, **extra}).status_code == 422


def test_create_idempotency_and_account_isolation(client, application, monkeypatch):
    register(client)
    client_id = str(uuid4())
    draft = create(client, client_id=client_id)
    assert create(client, client_id=client_id) == draft
    assert client.post("/api/meal-drafts", json={"client_id":client_id, "day":DAY,
        "meal_type":"breakfast", "text":"different"}).status_code == 409
    with TestClient(application) as other:
        register(other, "bob")
        assert other.get("/api/meal-drafts").json() == []
        root = f"/api/meal-drafts/{draft['id']}"
        monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: pytest.fail("Foreign draft must not call model"))
        assert other.get(root).status_code == 404
        assert other.put(root, json=changes(draft)).status_code == 404
        for action, body in [("parse", {"version":1}), ("cancel", {"version":1}), ("confirm", confirm_body(draft))]:
            assert other.post(root + "/" + action, json=body).status_code == 404
        assert create(other, client_id=client_id)["id"] != draft["id"]


def test_edit_unknown_grams_conflicts_and_confirm(client):
    register(client)
    draft = create(client)
    root = f"/api/meal-drafts/{draft['id']}"
    assert client.post(root + "/confirm", json=confirm_body(draft)).status_code == 422
    draft = client.put(root, json=changes(draft, items=[{"name":"Milk", "grams":None}])).json()
    assert draft["status"] == "needs_input"
    assert client.post(root + "/confirm", json=confirm_body(draft)).status_code == 422
    stale = changes(draft)
    draft = client.put(root, json=changes(draft, notes="checked")).json()
    assert draft["status"] == "ready"
    assert client.put(root, json=stale).status_code == 409
    assert client.post(root + "/cancel", json={"version":1}).status_code == 409
    body = confirm_body(draft)
    first = client.post(root + "/confirm", json=body)
    assert first.status_code == 200
    assert first.json()["status"] == "committed"
    assert len(first.json()["records"]) == 2
    assert all(item["kcal_per_100g"] is None for item in first.json()["records"])
    assert client.post(root + "/confirm", json=body).json() == first.json()
    assert client.post(root + "/confirm", json=confirm_body(draft)).status_code == 409
    assert client.put(root, json=changes(first.json())).status_code == 409
    assert client.post(root + "/parse", json={"version":first.json()["version"]}).status_code == 409
    assert client.get("/api/meal-drafts").json() == []
    assert client.get(f"/api/summary?day={DAY}").json()["meal_count"] == 2
    # Replay is the original result, not a second insert after later record deletion.
    client.delete(f"/api/meals/{first.json()['records'][0]['id']}")
    assert client.post(root + "/confirm", json=body).json() == first.json()
    assert client.get(f"/api/summary?day={DAY}").json()["meal_count"] == 1


def test_draft_validation(client):
    register(client)
    draft = create(client)
    for patch in ({"version":0}, {"items":[{"name":"Milk","grams":-1}]},
                  {"items":[{"name":"Milk","kcal_per_100g":60}]}, {"items":[{"name":""}]},
                  {"items":[{"name":"Milk"}] * 31}, {"status":"committed"}):
        assert client.put(f"/api/meal-drafts/{draft['id']}", json={**changes(draft), **patch}).status_code == 422
    assert client.get(f"/api/meal-drafts/{draft['id']}").json()["version"] == 1


def test_cancel_and_restart_recovery(client, application):
    register(client)
    abandoned = create(client)
    draft = ready(client)
    root = f"/api/meal-drafts/{abandoned['id']}"
    assert client.post(root + "/cancel", json={"version":1}).json()["status"] == "cancelled"
    assert client.post(root + "/cancel", json={"version":1}).status_code == 200
    assert client.post(root + "/confirm", json=confirm_body(abandoned)).status_code == 409
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/api/meal-drafts").json() == [draft]
        assert restarted.get(f"/api/meals?day={DAY}").json() == []


def test_parse_is_persisted_but_never_writes_meals(client, monkeypatch):
    register(client)
    draft = create(client)

    class Model:
        def generate(self, **kwargs):
            assert kwargs["message"] == draft["text"]
            return json.dumps({"input_type":"text", "items":[{"name":"Eggs","grams":100}, {"name":"Milk"}]})

    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: Model())
    response = client.post(f"/api/meal-drafts/{draft['id']}/parse", json={"version":1})
    assert response.status_code == 200
    parsed = response.json()
    assert parsed["version"] == 2 and parsed["status"] == "needs_input"
    assert parsed["questions"] and parsed["items"][1]["grams"] is None
    assert client.get(f"/api/meal-drafts/{draft['id']}").json() == parsed
    assert client.get(f"/api/meals?day={DAY}").json() == []


@pytest.mark.parametrize("code,status", [("MODEL_TIMEOUT",504), ("MODEL_NOT_CONFIGURED",503), ("MODEL_INVALID_OUTPUT",502)])
def test_parse_errors_preserve_draft(client, monkeypatch, code, status):
    register(client)
    draft = ready(client)

    def failure(settings):
        raise ModelError(code, "Test provider failure", status)

    monkeypatch.setattr("api.routes.create_meal_text_model", failure)
    response = client.post(f"/api/meal-drafts/{draft['id']}/parse", json={"version":draft["version"]})
    assert response.status_code == status
    assert client.get(f"/api/meal-drafts/{draft['id']}").json() == draft


def test_parse_cannot_overwrite_concurrent_edit(client, application, monkeypatch):
    user = register(client)
    draft = create(client)
    store = MealDraftStore(application.state.database, user["id"])

    class Model:
        def generate(self, **kwargs):
            store.update(draft["id"], DraftUpdate(**changes(draft, notes="newer edit")))
            return '{"input_type":"text","items":[{"name":"Stale result"}]}'

    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: Model())
    assert client.post(f"/api/meal-drafts/{draft['id']}/parse", json={"version":1}).status_code == 409
    current = store.get(draft["id"])
    assert current["notes"] == "newer edit" and current["items"][0]["name"] == "Eggs"


def test_description_can_be_corrected_before_reparsing(client, monkeypatch):
    register(client)
    draft = create(client)
    root = f"/api/meal-drafts/{draft['id']}"
    draft = client.put(root, json=changes(draft, text="Corrected description", items=[])).json()
    assert draft["text"] == "Corrected description"
    assert draft["status"] == "needs_input"

    class Model:
        def generate(self, **kwargs):
            assert kwargs["message"] == "Corrected description"
            return '{"input_type":"text","items":[{"name":"Corrected food","grams":100}]}'

    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: Model())
    assert client.post(root + "/parse", json={"version":draft["version"]}).json()["items"][0]["name"] == "Corrected food"


def test_atomic_confirmation_rollback(client, application):
    user = register(client)
    draft = ready(client)
    conflict_id = str(uuid5(UUID(draft["id"]), "1"))
    client.post("/api/meals", json=meal(client_id=conflict_id, name="existing unrelated food"))
    response = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft))
    assert response.status_code == 409
    assert client.get(f"/api/meals?day={DAY}").json()[0]["name"] == "existing unrelated food"
    assert len(client.get(f"/api/meals?day={DAY}").json()) == 1
    assert MealDraftStore(application.state.database, user["id"]).get(draft["id"])["status"] == "ready"


def test_concurrent_confirmations_are_idempotent(client, application):
    user = register(client)
    draft = ready(client)
    store = MealDraftStore(application.state.database, user["id"])
    body = DraftConfirm(**confirm_body(draft))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.confirm(draft["id"], body), range(2)))
    assert results[0] == results[1]
    assert len(results[0]["records"]) == 2
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 2


def test_v1_migration_preserves_data_and_backup(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA + "\nPRAGMA user_version=1;")
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'legacy','test-hash')")
        connection.execute("INSERT INTO meals(user_id,client_id,day,payload) VALUES (1,'old',?,?)", (DAY, '{"name":"legacy meal"}'))
    database = Database(path)
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert connection.execute("SELECT payload FROM meals").fetchone()[0] == '{"name":"legacy meal"}'
        assert connection.execute("SELECT COUNT(*) FROM meal_drafts").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(path.with_name(path.name + ".pre-v2.bak")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 1


def test_migration_failure_rolls_back_schema(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA + "\nPRAGMA user_version=1;")
    monkeypatch.setattr("database.DRAFT_SCHEMA", "CREATE TABLE temporary_change(id INTEGER); INVALID SQL;")
    with pytest.raises(sqlite3.Error):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='temporary_change'").fetchone() is None
