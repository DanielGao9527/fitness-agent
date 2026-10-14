"""Cross-conversation recall uses only confirmed messages owned by the caller."""
from dataclasses import replace
from fastapi.testclient import TestClient

from app import create_app
from services.coach_memory import recall_confirmed_history
from test_coach import BASE, conversation, send
from test_coach_reviews import reviews, confirm, understand
from test_foundation import application, client, register
from test_knowledge import library
from test_meal_plans import plans
from test_training_recommendations import recommend, result


def _confirmed_training(client, model, message="在家，只有哑铃"):
    model.edit = lambda _, out: out.update(scope="strength", command="")
    sent = send(client, conversation(client), message).json()
    ready = understand(client, sent).json()
    assert ready["review"]["status"] == "ready"
    selection = {"equipment_scope": "long_term"} if ready["review"].get("equipment_memory") else {}
    accepted = confirm(client, ready, **selection)
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def test_confirmed_equipment_is_saved_and_old_message_remains_traceable(reviews):
    client, app, model, _ = reviews
    old = _confirmed_training(client, model)
    now = send(client, conversation(client), "上次说过的哑铃训练，我想继续").json()
    found = client.post(f"{BASE}/memory/search", json={"query": "哑铃"})
    assert found.status_code == 200, found.text
    assert found.json()["items"][0]["conversation_id"] == old["id"]
    assert found.json()["used_as_current_constraint"] is False
    assert client.get("/api/profile").json()["equipment"] == "在家，只有哑铃"
    assert now["constraints"] == {} and now["training_context"].get("equipment") != "在家，只有哑铃"

    model.edit = lambda _, out: out.update(scope="strength", command="")
    reviewed = understand(client, now)
    assert reviewed.status_code == 200, reviewed.text
    evidence = model.messages[-1]["historical_evidence"]
    assert len(evidence) == 1 and evidence[0]["conversation_id"] == old["id"]
    assert evidence[0]["status"] == "historical_confirmed_message"

    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.post(f"{BASE}/memory/search", json={"query": "哑铃"}).json()["items"][0]["conversation_id"] == old["id"]


def test_pending_future_deleted_and_other_users_are_not_recalled(reviews):
    client, app, model, _ = reviews
    confirmed = _confirmed_training(client, model, "在家，只有哑铃")
    pending = send(client, conversation(client), "新添跳绳训练").json()
    assert client.post(f"{BASE}/memory/search", json={"query": "跳绳"}).json()["items"] == []
    _confirmed_training(client, model, "有跳绳可以训练")
    with app.state.database.connect() as connection:
        owner = connection.execute("SELECT user_id FROM coach_conversations WHERE id=?", (confirmed["id"],)).fetchone()[0]
        assert recall_confirmed_history(connection, owner, "哑铃", before_day="2026-09-14") == []
        pending_rowid = connection.execute("SELECT rowid FROM coach_turns WHERE conversation_id=? AND version=?",
                                           (pending["id"], pending["version"])).fetchone()[0]
        assert recall_confirmed_history(connection, owner, "跳绳", before_turn_rowid=pending_rowid) == []

    with TestClient(app) as other:
        register(other, "bob")
        assert other.post(f"{BASE}/memory/search", json={"query": "哑铃"}).json()["items"] == []
    assert client.request("DELETE", f"{BASE}/{confirmed['id']}", json={"version": confirmed["version"]}).status_code == 204
    assert client.post(f"{BASE}/memory/search", json={"query": "哑铃"}).json()["items"] == []
    assert pending["id"]


def test_memory_endpoint_requires_login_and_bounds_query(client):
    assert client.post(f"{BASE}/memory/search", json={"query": "哑铃"}).status_code == 401
    register(client)
    assert client.post(f"{BASE}/memory/search", json={"query": ""}).status_code == 422
    assert client.post(f"{BASE}/memory/search", json={"query": "x" * 201}).status_code == 422


def test_confirmed_equipment_flows_into_new_session_plan_and_profile_correction(reviews):
    client, app, model, _ = reviews
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    old = _confirmed_training(client, model)
    today = send(client, conversation(client), "今天练什么").json()
    first = result(recommend(client, today))
    assert first["status"] == "ready" and first["equipment"] == "在家，只有哑铃"
    assert first["equipment_source"] == "profile"
    assert first["exercises"] and not model.messages[-1].get("historical_evidence")
    assert client.put("/api/profile", json={"weight_kg": 70}).json()["equipment"] == "在家，只有哑铃"

    assert client.put("/api/profile", json={"equipment": "在家，徒手"}).status_code == 200
    corrected_old = client.get(f"{BASE}/{old['id']}").json()
    assert corrected_old["training_context"].get("equipment") != "在家，只有哑铃"
    assert client.get(f"{BASE}/{today['id']}").json()["turns"][-1]["response"]["training_recommendation"]["stale"]
    second = result(recommend(client, today))
    assert second["status"] == "ready" and second["equipment"] == "在家，徒手"
    assert second["equipment_source"] == "profile"
    assert second["exercises"] and all(row["equipment"] == ["floor"] for row in second["exercises"])
    assert client.post(f"{BASE}/memory/search", json={"query": "哑铃"}).json()["items"] == []

    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        later = send(restarted, conversation(restarted), "今天练什么").json()
        assert result(recommend(restarted, later))["equipment"] == "在家，徒手"


def test_temporary_equipment_is_not_promoted_and_confirmed_correction_replaces_old(reviews):
    client, app, model, _ = reviews
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    _confirmed_training(client, model, "在家，只有哑铃")
    _confirmed_training(client, model, "今天在家只有哑铃")
    assert client.get("/api/profile").json()["equipment"] == "在家，只有哑铃"
    changed = _confirmed_training(client, model, "器械改为在家，徒手")
    assert client.get("/api/profile").json()["equipment"] == "在家，徒手"
    # Durable facts come from the profile, never a duplicated local override
    # that could shadow a later day-scoped condition.
    final = result(recommend(client, changed))
    assert final["status"] == "ready" and final["equipment"] == "在家，徒手"
    assert final["equipment_source"] == "profile"
    assert client.post(f"{BASE}/memory/search", json={"query": "哑铃"}).json()["items"] == []
    assert client.put("/api/profile", json={"equipment": ""}).json()["equipment"] == ""
    assert client.post(f"{BASE}/memory/search", json={"query": "徒手"}).json()["items"] == []


def test_automatic_understanding_never_writes_long_term_equipment(reviews):
    client, _, model, _ = reviews
    model.edit = lambda _, out: out.update(scope="strength", command="")
    sent = send(client, conversation(client), "在家，只有哑铃").json()
    applied = understand(client, sent, auto_apply=True)
    assert applied.status_code == 200, applied.text
    assert applied.json()["pending"] and applied.json()["review"]["status"] == "ready"
    assert client.get("/api/profile").json()["equipment"] == ""
    assert confirm(client, applied.json()).status_code == 422
    assert confirm(client, applied.json(), equipment_scope="long_term").status_code == 200
    assert client.get("/api/profile").json()["equipment"] == "在家，只有哑铃"


def test_current_conversation_override_wins_until_profile_is_changed(reviews):
    client, app, model, _ = reviews
    app.state.settings = replace(app.state.settings, training_plan_enabled=True)
    old = _confirmed_training(client, model)
    local = send(client, old, "器械改为在家，徒手").json()
    assert local["training_context"]["equipment"] == "在家，徒手"
    first = result(recommend(client, local))
    assert first["equipment"] == "在家，徒手" and first["equipment_source"] == "current_conversation"
    assert client.get("/api/profile").json()["equipment"] == "在家，只有哑铃"

    assert client.put("/api/profile", json={"equipment": "健身房和泳池"}).status_code == 200
    second = result(recommend(client, local))
    assert second["equipment"] == "健身房和泳池" and second["equipment_source"] == "profile"
