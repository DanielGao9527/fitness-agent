import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from model.factory import ModelError, nutrition_status
from schemas import DraftUpdate
from services.meal_drafts import MealDraftStore
from services.nutrition import NutritionService
from test_draft_flow import changes, confirm_body, create, ready
from test_foundation import DAY, application, client, meal, register


def result(index=0, **changes):
    return {"index": index, "status": "estimated", "kcal": {"lower": 100, "upper": 200},
            "protein": {"lower": 10, "upper": 15}, "carbs": {"lower": 1, "upper": 3},
            "fat": {"lower": 8, "upper": 12}, "assumptions": ["Synthetic test values, not nutrition reference data"], **changes}


class Model:
    def __init__(self, response=None):
        self.calls = 0
        self.response = response

    def generate(self, **kwargs):
        self.calls += 1
        items = json.loads(kwargs["message"])["items"]
        assert all(set(item) == {"index", "name", "grams", "amount_description"} for item in items)
        assert "JSON Schema" in kwargs["system_prompt"]
        return self.response if self.response is not None else json.dumps({"items": [result(item["index"]) for item in reversed(items)]})


def estimate(client, draft):
    return client.post(f"/api/meal-drafts/{draft['id']}/nutrition", json={"version": draft["version"]})


@pytest.fixture
def model(monkeypatch):
    instance = Model()
    monkeypatch.setattr("api.routes.create_nutrition_model", lambda settings: instance)
    return instance


def test_estimate_confirm_recovery_and_separate_summary(client, application, model):
    register(client)
    draft = ready(client)
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, items=[
        {"name": "Eggs", "grams": None, "amount_description": "two eggs"},
        {"name": "Milk", "grams": 250}])).json()
    draft = estimate(client, draft).json()
    assert [item["index"] for item in draft["nutrition"]["items"]] == [0, 1]
    assert draft["nutrition"]["source_type"] == "model_estimate"
    assert draft["nutrition"]["model"] == "qwen3.7-plus"
    assert client.get(f"/api/meals?day={DAY}").json() == []
    assert estimate(client, draft).json() == draft
    assert model.calls == 1
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/meal-drafts/{draft['id']}").json() == draft
    body = confirm_body(draft)
    saved = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=body).json()
    assert saved["records"][0]["grams"] is None
    assert all(row["kcal_per_100g"] is None and row["source"] == "" for row in saved["records"])
    assert client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=body).json() == saved
    client.post("/api/meals", json=meal(kcal_per_100g=60, source="Test label"))
    client.post("/api/meals", json=meal())
    summary = client.get(f"/api/summary?day={DAY}").json()
    assert summary["nutrition"]["kcal"] == {"known_total": 150, "missing_count": 3}
    assert summary["estimated_nutrition"]["kcal"] == {"lower_total": 200, "upper_total": 400, "count": 2, "unknown_count": 1}
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/summary?day={DAY}").json() == summary


@pytest.mark.parametrize("patch", [
    {"kcal": {"lower": 200, "upper": 100}}, {"kcal": {"lower": -1, "upper": 100}},
    {"kcal": {"lower": "100", "upper": 200}}, {"kcal": {"lower": True, "upper": 200}},
    {"kcal": {"lower": 1, "upper": float("inf")}}, {"kcal": None}, {"assumptions": []},
    {"status": "known"}, {"question": "Cannot know but here are numbers"},
    {"source_type": "database"}, {"source_url": "https://example.com"}, {"user_id": 99},
    {"status": "unknown", "question": "Which portion?"}, {"index": 1},
    {"fat": {"lower": 200, "upper": 210}},
])
def test_invalid_outputs_rejected(patch):
    service = NutritionService(Model(json.dumps({"items": [result(**patch)]})), "test-model")
    with pytest.raises(ModelError) as error:
        service.estimate([{"name": "Eggs", "grams": 100}])
    assert error.value.code == "MODEL_INVALID_OUTPUT"


@pytest.mark.parametrize("output", ['not json', '{"items":[]}', json.dumps({"items": [result(), result()]})])
def test_incomplete_or_duplicate_outputs_rejected(output):
    with pytest.raises(ModelError):
        NutritionService(Model(output), "test").estimate([{"name": "Eggs", "grams": 100}])


@pytest.mark.parametrize("patch", [{"items": [{"name": "Different", "grams": 100}]},
                                    {"items": [{"name": "Eggs", "grams": 150}]},
                                    {"text": "New description"}])
def test_changed_inputs_clear_old_estimates(client, model, patch):
    register(client)
    draft = estimate(client, ready(client)).json()
    updated = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, items=draft["items"]) | patch).json()
    assert "nutrition" not in updated


def test_notes_preserve_clear_removes_and_reparse_invalidates(client, model, monkeypatch):
    register(client)
    draft = estimate(client, ready(client)).json()
    root = f"/api/meal-drafts/{draft['id']}"
    draft = client.put(root, json=changes(draft, items=draft["items"], notes="New note")).json()
    assert "nutrition" in draft
    draft = client.post(root + "/nutrition/clear", json={"version": draft["version"]}).json()
    assert "nutrition" not in draft
    draft = estimate(client, draft).json()
    # The parser receives plain text, unlike the nutrition model.
    class Parser:
        def generate(self, **kwargs):
            return '{"input_type":"text","items":[{"name":"Eggs","grams":100}]}'
    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: Parser())
    assert "nutrition" not in client.post(root + "/parse", json={"version": draft["version"]}).json()


def test_unknown_can_be_saved_without_numbers(client, model):
    register(client)
    draft = ready(client)
    model.response = json.dumps({"items": [{"index": index, "status": "unknown", "question": "How much did you personally eat?"} for index in range(2)]})
    draft = estimate(client, draft).json()
    assert all(item["kcal"] is None for item in draft["nutrition"]["items"])
    saved = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft)).json()
    assert all("nutrition_estimate" not in row for row in saved["records"])


def test_ownership_versions_errors_and_budget(client, application, model):
    register(client)
    draft = ready(client)
    with TestClient(application) as other:
        assert estimate(other, draft).status_code == 401
        register(other, "bob")
        assert estimate(other, draft).status_code == 404
        assert other.post(f"/api/meal-drafts/{draft['id']}/nutrition/clear", json={"version": draft["version"]}).status_code == 404
    assert estimate(client, {**draft, "version": 1}).status_code == 409
    assert client.post(f"/api/meal-drafts/{draft['id']}/nutrition", json={"version": draft["version"]}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert model.calls == 0
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=0)
    assert estimate(client, draft).status_code == 429
    assert model.calls == 0
    assert client.get(f"/api/meal-drafts/{draft['id']}").json() == draft


@pytest.mark.parametrize("items", [[], [{"name": "Eggs"}], [{"name": "炒菜", "grams": 100}]])
def test_invalid_inputs_do_not_call_model(client, model, items):
    register(client)
    draft = create(client)
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, items=items)).json()
    assert estimate(client, draft).status_code == 422
    assert model.calls == 0


def test_concurrent_edit_not_overwritten(client, application, model):
    user = register(client)
    draft = ready(client)
    store = MealDraftStore(application.state.database, user["id"])
    original = model.generate
    def generate(**kwargs):
        store.update(draft["id"], DraftUpdate(**changes(draft, notes="Concurrent edit")))
        return original(**kwargs)
    model.generate = generate
    assert estimate(client, draft).status_code == 409
    current = store.get(draft["id"])
    assert current["notes"] == "Concurrent edit" and "nutrition" not in current


@pytest.mark.parametrize("patch,kept", [({"notes": "Changed note"}, True), ({"day": "2026-09-14"}, True),
    ({"grams": 200}, False), ({"amount_description": "2 bowls"}, False), ({"name": "Rice"}, False),
    ({"kcal_per_100g": 80, "source": "New label"}, False)])
def test_record_edit_preserves_or_invalidates_snapshot(client, model, patch, kept):
    register(client)
    draft = estimate(client, ready(client)).json()
    row = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft)).json()["records"][0]
    body = {key: value for key, value in row.items() if key not in ("id", "nutrition_estimate")}
    response = client.put(f"/api/meals/{row['id']}", json=body | patch)
    assert response.status_code == 200
    assert ("nutrition_estimate" in response.json()) == kept
    assert client.put(f"/api/meals/{row['id']}", json=body | {"nutrition_estimate": row["nutrition_estimate"]}).status_code == 422


@pytest.mark.parametrize("code,status", [("MODEL_TIMEOUT", 504), ("MODEL_INVALID_OUTPUT", 502)])
def test_failure_preserves_draft_and_counts_attempt(client, application, model, code, status):
    register(client)
    draft = ready(client)
    def fail(**kwargs):
        raise ModelError(code, "Safe test error", status)
    model.generate = fail
    assert estimate(client, draft).status_code == status
    assert client.get(f"/api/meal-drafts/{draft['id']}").json() == draft
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 1


def test_configuration_and_no_key(client, application):
    settings = application.state.settings
    assert nutrition_status(settings) == "not_configured"
    assert nutrition_status(replace(settings, nutrition_enabled=True)) == "not_configured"
    assert nutrition_status(replace(settings, nutrition_enabled=True, model_provider="qwen", qwen_api_key="synthetic")) == "configured_unverified"
    register(client)
    assert estimate(client, ready(client)).status_code == 503
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0


def test_env_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("FITNESS_NUTRITION_ENABLED", "true")
    monkeypatch.setenv("FITNESS_DB_PATH", str(tmp_path / "test.sqlite3"))
    assert Settings.from_env().nutrition_enabled
