from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import DAY, application, client, register
from test_knowledge import library
from test_meal_plans import plans, generate, accept
from test_draft_flow import changes, confirm_body


def consent(client):
    state = client.get("/api/meal-plans/consent").json()
    assert client.put("/api/meal-plans/consent", json={"context_hash": state["context_hash"],
                      "adult_general_diet": True, "constraints_reviewed": True}).status_code == 200


def edit_body(plan):
    items = [{key: item[key] for key in ("food_id", "lower", "upper")} for item in plan["items"]]
    items[0].update(lower=120, upper=140)
    return {"client_id": str(uuid4()), "items": items, "reviewed": True}


def test_portions_no_model_or_actual_writes_and_replay(plans):
    client, app, model, _ = plans
    first = generate(client).json()
    consent(client)
    body = edit_body(first)
    endpoint = f"/api/meal-plans/{first['id']}/portions"
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["portion_source"] == "user" and draft["status"] == "draft" and not draft["stale"]
    assert draft["items"][0]["lower"] == 120 and draft["items"][1:] == first["items"][1:]
    assert not draft.get("nutrition_review")
    assert client.post(endpoint, json=body).json()["id"] == draft["id"]
    assert len(model.messages) == 1 and client.get(f"/api/meals?day={DAY}").json() == []
    assert accept(client, draft).status_code == 200
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/meal-plans?day={DAY}").json()[0]["items"] == draft["items"]
    body["items"][0]["upper"] = 150
    assert client.post(endpoint, json=body).status_code == 409


@pytest.mark.parametrize("bad", ["low", "range", "food", "duplicate", "review", "unknown"])
def test_bad_portions_no_new_plan(plans, bad):
    client, _, model, _ = plans
    plan = generate(client).json()
    consent(client)
    body = edit_body(plan)
    if bad == "low": body["items"][0]["lower"] = 1
    if bad == "range": body["items"][0]["upper"] = 100
    if bad == "food": body["items"][0]["food_id"] = "corn"
    if bad == "duplicate": body["items"].append(dict(body["items"][0]))
    if bad == "review": body["reviewed"] = 1
    if bad == "unknown": body["user_id"] = 999
    assert client.post(f"/api/meal-plans/{plan['id']}/portions", json=body).status_code in (422, 502)
    assert len(client.get(f"/api/meal-plans?day={DAY}").json()) == 1 and len(model.messages) == 1


def test_stale_other_user_and_old_versions_block_edit(plans):
    client, app, _, _ = plans
    first = generate(client).json()
    consent(client)
    assert generate(client).status_code == 200
    assert client.post(f"/api/meal-plans/{first['id']}/portions", json=edit_body(first)).status_code == 409
    with TestClient(app) as other:
        register(other, "plan_other")
        consent(other)
        assert other.post(f"/api/meal-plans/{first['id']}/portions", json=edit_body(first)).status_code == 404
        assert other.post(f"/api/meal-plans/{first['id']}/record-draft", json={"reviewed": True}).status_code == 404


def test_actual_bridge_requires_quantity_and_never_duplicate_even_after_delete(plans):
    client, app, model, _ = plans
    first = generate(client).json()
    endpoint = f"/api/meal-plans/{first['id']}/record-draft"
    assert client.post(endpoint, json={"reviewed": False}).status_code == 422
    response = client.post(endpoint, json={"reviewed": True})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["record_plan_id"] == first["id"] and draft["status"] == "needs_input"
    assert all(item["grams"] is None and not item["amount_description"] for item in draft["items"])
    assert not draft.get("nutrition")
    assert client.post(endpoint, json={"reviewed": True}).json() == draft
    assert client.get(f"/api/meals?day={DAY}").json() == []
    assert client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft)).status_code == 422
    items = [{"name": item["name"], "grams": 100} for item in draft["items"]]
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, items=items)).json()
    result = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft)).json()
    assert result["status"] == "committed" and len(result["records"]) == len(items)
    client.delete(f"/api/meals/{result['records'][0]['id']}")
    assert client.post(endpoint, json={"reviewed": True}).json()["status"] == "committed"
    assert len(client.get(f"/api/meals?day={DAY}").json()) == len(items) - 1
    assert len(model.messages) == 1
