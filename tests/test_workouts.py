import json
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.workouts as routes
from app import create_app
from config import ROOT
from model.factory import ModelError, create_workout_model, workout_status
from services.workouts import PREVIEW_SECONDS
from test_foundation import DAY, application, client, register, workout


class Model:
    def __init__(self):
        self.calls = []
        self.response = None

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return self.response
        items = json.loads(kwargs["message"])["items"]
        return json.dumps({"items": [{"index": item["index"], "status": "estimated",
            "kcal": {"lower": item["minutes"] * 3, "upper": item["minutes"] * 5},
            "assumptions": ["Synthetic fixture, not nutritional evidence"]} for item in items]})


@pytest.fixture
def model(monkeypatch):
    result = Model()
    monkeypatch.setattr(routes, "create_workout_model", lambda settings: result)
    return result


def estimate_input(**kwargs):
    return {"client_id": str(uuid4()), "name": "Walking", "minutes": 30, "weight_kg": 70,
            "intensity": "moderate", "details": "level ground", **kwargs}


def preview(client, **kwargs):
    response = client.post("/api/workouts/calories/preview-batch", json={"items": [estimate_input(**kwargs)]})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def record(p, **kwargs):
    return workout(**p["workout"], status="completed", calorie_preview_id=p["id"]) | kwargs


def edit(row, **kwargs):
    return {key: value for key, value in row.items() if key not in ("id", "calorie_estimate")} | kwargs


def test_profile_limits_preserved_separate_and_isolated(client, application):
    user = register(client)
    with application.state.database.connect() as connection:
        connection.execute("INSERT INTO profiles(user_id,payload) VALUES (?,?)", (user["id"], json.dumps({"preferences": "Old allergy and preference notes"})))
    old = client.get("/api/profile").json()
    assert old["preferences"] == "Old allergy and preference notes" and old["food_allergies"] == ""
    updated = old | {"food_allergies": "西兰花过敏"}
    assert client.put("/api/profile", json=updated).json() == updated
    with TestClient(create_app(application.state.settings)) as other:
        other.cookies.update(client.cookies)
        assert other.get("/api/profile").json() == updated
        other.cookies.clear()
        register(other, "bob")
        assert other.get("/api/profile").json()["food_allergies"] == ""
    prompt = (ROOT / "prompts/assistant.md").read_text(encoding="utf-8")
    assert "硬约束" in prompt and "西兰花" in prompt and "一般成人" in prompt


def test_batch_workouts_atomic_idempotent_and_restart(client, application):
    register(client)
    items = [workout(name=f"Walking {i}", status="completed" if i == 0 else "planned") for i in range(8)]
    response = client.post("/api/workouts/batch", json={"items": items})
    assert response.status_code == 201 and len(response.json()) == 8
    assert client.post("/api/workouts/batch", json={"items": items}).json() == response.json()
    summary = client.get(f"/api/summary?day={DAY}").json()
    assert summary["completed_minutes"] == 30 and summary["estimated_workout_calories"]["unknown_count"] == 1
    assert summary["estimated_workout_calories"]["lower_total"] is None
    conflict = [workout(status="completed"), items[0] | {"minutes": 31}]
    assert client.post("/api/workouts/batch", json={"items": conflict}).status_code == 409
    assert len(client.get(f"/api/workouts?day={DAY}").json()) == 8
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.post("/api/workouts/batch", json={"items": items}).json() == response.json()


@pytest.mark.parametrize("items", [[], [workout()] * 2, [workout(), workout(day="2026-09-14")],
    [workout() for _ in range(31)], [workout(minutes=0)], [workout(status="unknown")]])
def test_invalid_batches(client, items):
    register(client)
    assert client.post("/api/workouts/batch", json={"items": items}).status_code == 422
    assert client.get(f"/api/workouts?day={DAY}").json() == []


def test_old_workout_retry_compatible(client, application):
    user = register(client)
    item = workout(status="planned")
    payload = {k: v for k, v in item.items() if k != "client_id"} | {"notes": ""}
    with application.state.database.connect() as connection:
        connection.execute("INSERT INTO workouts(user_id,client_id,day,payload) VALUES (?,?,?,?)", (user["id"], item["client_id"], DAY, json.dumps(payload)))
    assert client.post("/api/workouts", json=item).status_code == 201


def test_parse_temporary_missing_and_statuses(client, model):
    register(client)
    model.response = json.dumps({"items": [{"name": "Walking", "minutes": 30, "status": "completed", "source_text": "Walked 30 minutes"},
        {"name": "Cycling", "status": "planned", "source_text": "will cycle"}, {"name": "Swimming", "source_text": "swimming"}]})
    text = "Walked 30 minutes, will cycle, swimming"
    response = client.post("/api/workouts/parse-text", json={"text": text})
    assert response.status_code == 200
    assert response.json()["items"][1]["minutes"] is None and response.json()["items"][2]["status"] == "unknown"
    assert len(response.json()["questions"]) == 2
    assert client.get(f"/api/workouts?day={DAY}").json() == []
    assert model.calls[0]["message"] == text
    assert "不从组数" in model.calls[0]["system_prompt"] and "否定句" in model.calls[0]["system_prompt"]


@pytest.mark.parametrize("raw", ["not json", '{"items":[{"name":"Walk","minutes":true}]}',
    '{"items":[{"name":"Walk","status":"completed","calories":200}]}', '{"items":[],"user_id":2}'])
def test_invalid_parse_output(client, model, raw):
    register(client)
    model.response = raw
    assert client.post("/api/workouts/parse-text", json={"text": "fixture"}).status_code == 502


def test_preview_eight_items_cached_partial_and_thirty(client, model, application):
    register(client)
    inputs = [estimate_input(name=f"Walking {i}") for i in range(8)]
    result = client.post("/api/workouts/calories/preview-batch", json={"items": inputs})
    assert result.status_code == 200 and len(model.calls) == 2
    assert client.post("/api/workouts/calories/preview-batch", json={"items": inputs}).json() == result.json()
    assert len(model.calls) == 2
    inputs[0] = estimate_input(name="Walking 0", minutes=40)
    changed = client.post("/api/workouts/calories/preview-batch", json={"items": inputs}).json()
    assert len(model.calls) == 3 and changed["items"][1:] == result.json()["items"][1:]
    assert client.post("/api/workouts/calories/preview-batch", json={"items": [estimate_input() for _ in range(30)]}).status_code == 200
    assert len(model.calls) == 9
    assert client.get(f"/api/workouts?day={DAY}").json() == []
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 9


def test_estimate_save_edit_toggle_and_summary(client, model):
    register(client)
    p = preview(client)
    saved = client.post("/api/workouts/batch", json={"items": [record(p), record(p, status="planned")]})
    assert saved.status_code == 201
    rows = saved.json()
    assert rows[0]["calorie_estimate"]["basis"] == "gross_activity" and "calorie_preview_id" not in rows[0]
    summary = client.get(f"/api/summary?day={DAY}").json()["estimated_workout_calories"]
    assert summary["lower_total"] == 90 and summary["count"] == 1
    toggled = client.put(f"/api/workouts/{rows[1]['id']}", json=edit(rows[1], status="completed")).json()
    assert toggled["calorie_estimate"] == rows[1]["calorie_estimate"]
    changed = preview(client, minutes=40)
    assert client.get(f"/api/workouts?day={DAY}").json()[0]["minutes"] == 30
    updated = client.put(f"/api/workouts/{rows[0]['id']}", json=edit(rows[0], minutes=40, calorie_preview_id=changed["id"])).json()
    assert updated["calorie_estimate"]["kcal"]["lower"] == 120
    invalidated = client.put(f"/api/workouts/{rows[0]['id']}", json=edit(updated, intensity="vigorous")).json()
    assert "calorie_estimate" not in invalidated


@pytest.mark.parametrize("change", [{"minutes": 40}, {"name": "Other"}, {"intensity": "vigorous"}, {"weight_kg": 71}, {"details": "hill"}])
def test_stale_preview_rejected(client, model, change):
    register(client)
    p = preview(client)
    assert client.post("/api/workouts", json=record(p, **change)).status_code == 409


def test_ownership_forgery_and_expiry(client, application, model):
    register(client)
    p = preview(client)
    saved = client.post("/api/workouts", json=record(p)).json()
    with TestClient(application) as other:
        assert other.post("/api/workouts/parse-text", json={"text": "fixture"}).status_code == 401
        register(other, "bob")
        assert other.post("/api/workouts", json=record(p)).status_code == 404
        assert other.get(f"/api/workouts?day={DAY}").json() == []
    assert client.post("/api/workouts", json=workout(calorie_estimate=saved["calorie_estimate"])).status_code == 422
    with application.state.database.connect() as connection:
        connection.execute("UPDATE workout_previews SET created_at=?", (int(time.time())-PREVIEW_SECONDS-1,))
    assert client.post("/api/workouts", json=record(p)).status_code == 409
    preview(client)
    assert client.get(f"/api/workouts?day={DAY}").json() == [saved]


def test_budget_preflight_and_later_failure_no_partial(client, model, application):
    register(client)
    inputs = [estimate_input() for _ in range(8)]
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=1)
    assert client.post("/api/workouts/calories/preview-batch", json={"items": inputs}).status_code == 429
    assert not model.calls
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=10)
    generate = model.generate
    def fail_second(**kwargs):
        if len(model.calls) == 1:
            raise ModelError("MODEL_TIMEOUT", "fixture timeout", 504)
        return generate(**kwargs)
    model.generate = fail_second
    assert client.post("/api/workouts/calories/preview-batch", json={"items": inputs}).status_code == 504
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workout_previews").fetchone()[0] == 0
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 2


@pytest.mark.parametrize("item", [estimate_input(weight_kg=None), estimate_input(weight_kg=0), estimate_input(minutes=0), estimate_input(user_id=2)])
def test_bad_estimate_input_no_call(client, model, item):
    register(client)
    assert client.post("/api/workouts/calories/preview-batch", json={"items": [item]}).status_code == 422
    assert not model.calls


@pytest.mark.parametrize("raw", ["oops", '{"items":[]}', '{"items":[{"index":1,"status":"unknown","question":"q"}]}',
    '{"items":[{"index":0,"status":"estimated","kcal":{"lower":100,"upper":10},"assumptions":["x"]}]}',
    '{"items":[{"index":0,"status":"estimated","kcal":{"lower":100,"upper":10000},"assumptions":["x"]}]}',
    '{"items":[{"index":0,"status":"unknown","kcal":{"lower":1,"upper":2},"question":"q"}]}'])
def test_bad_calorie_output(client, model, raw):
    register(client)
    model.response = raw
    assert client.post("/api/workouts/calories/preview-batch", json={"items": [estimate_input()]}).status_code == 502


def test_unknown_and_clear(client, model):
    register(client)
    model.response = '{"items":[{"index":0,"status":"unknown","question":"What activity?"}]}'
    p = preview(client)
    saved = client.post("/api/workouts", json=record(p)).json()
    assert "calorie_estimate" not in saved
    model.response = None
    p = preview(client)
    updated = client.put(f"/api/workouts/{saved['id']}", json=edit(saved, calorie_preview_id=p["id"])).json()
    assert "calorie_estimate" in updated
    assert client.put(f"/api/workouts/{saved['id']}", json=edit(updated, clear_calorie_estimate=True)).status_code == 200
    assert "calorie_estimate" not in client.get(f"/api/workouts?day={DAY}").json()[0]


def test_four_reported_sessions_accept_null_optional_question(client, model):
    register(client)
    inputs = [estimate_input(name=name, minutes=minutes, intensity="normal_assumed")
              for name, minutes in [("游泳", 30), ("胸和三头", 30), ("肩", 20), ("二头", 20)]]
    model.response = json.dumps({"items": [{"index": i, "status": "estimated", "kcal": {"lower": 60, "upper": 90},
        "assumptions": ["Synthetic range only"], "question": None} for i in range(4)]})
    response = client.post("/api/workouts/calories/preview-batch", json={"items": inputs})
    assert response.status_code == 200, response.text
    assert len(model.calls) == 1 and len(response.json()["items"]) == 4
    assert sum(row["workout"]["minutes"] for row in response.json()["items"]) == 100
    saved = client.post("/api/workouts/batch", json={"items": [record(p) for p in response.json()["items"]]})
    assert saved.status_code == 201
    assert client.get(f"/api/summary?day={DAY}").json()["estimated_workout_calories"]["count"] == 4


@pytest.mark.parametrize("update", [
    {"kcal": {"lower": "60", "upper": 90}}, {"kcal": {"lower": 90, "upper": 60}},
    {"status": "unknown"}, {"assumptions": None}, {"question": "无需补问"}, {"index": True},
])
def test_optional_compatibility_does_not_relax_calorie_contract(client, model, application, update):
    register(client)
    item = {"index": 0, "status": "estimated", "kcal": {"lower": 60, "upper": 90},
            "assumptions": ["Synthetic"], "question": None, **update}
    model.response = json.dumps({"items": [item]})
    response = client.post("/api/workouts/calories/preview-batch", json={"items": [estimate_input()]})
    assert response.status_code == 502
    assert "未通过检查" in response.text and "Synthetic" not in response.text
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workout_previews").fetchone()[0] == 0


def test_unknown_null_assumptions_and_field_diagnostic(client, model):
    register(client)
    model.response = json.dumps({"items": [{"index": 0, "status": "unknown", "kcal": None, "assumptions": None, "question": "Which activity?"}]})
    assert preview(client)["estimate"]["status"] == "unknown"
    model.response = json.dumps({"items": [{"index": 0, "status": "estimated", "kcal": "PRIVATE-CANARY", "assumptions": ["Synthetic"]}]})
    response = client.post("/api/workouts/calories/preview-batch", json={"items": [estimate_input()]})
    assert response.status_code == 502 and "热量范围" in response.text and "PRIVATE-CANARY" not in response.text


def test_config_disabled_and_no_key_manual_works(client, application):
    register(client)
    assert workout_status(application.state.settings) == "not_configured"
    assert client.post("/api/workouts/parse-text", json={"text": "fixture"}).status_code == 503
    assert client.post("/api/workouts/calories/preview-batch", json={"items": [estimate_input()]}).status_code == 503
    assert client.post("/api/workouts/batch", json={"items": [workout()]}).status_code == 201
    with pytest.raises(ModelError):
        create_workout_model(replace(application.state.settings, workout_enabled=True))


def test_session_example_pipeline_with_model_substitute(client, model, application):
    register(client)
    text = "游泳游了30分钟，练三头胸练了40分钟，完成到力竭，强度就正常强度。"
    model.response = json.dumps({"items": [
        {"name": "游泳", "minutes": 30, "status": "completed", "intensity": "normal", "source_text": "游泳游了30分钟"},
        {"name": "胸和三头力量训练", "minutes": 40, "status": "completed", "intensity": "正常强度", "details": "力竭", "source_text": "练三头胸练了40分钟"},
    ], "questions": ["请补充组数和强度"]})
    parsed = client.post("/api/workouts/parse-text", json={"text": text}).json()
    assert parsed["questions"] == []
    assert sum(item["minutes"] for item in parsed["items"]) == 70
    assert parsed["items"][1]["intensity"] == "normal"
    assert "共用一个总时长" in model.calls[0]["system_prompt"] and "总计70分钟" in model.calls[0]["system_prompt"]
    assert client.get(f"/api/workouts?day={DAY}").json() == []
    model.response = None
    inputs = [{k: v for k, v in item.items() if k != "status"} | {"client_id": str(uuid4()), "weight_kg": 70}
              for item in parsed["items"]]
    previews = client.post("/api/workouts/calories/preview-batch", json={"items": inputs}).json()["items"]
    items = [record(p) for p in previews]
    saved = client.post("/api/workouts/batch", json={"items": items}).json()
    assert client.post("/api/workouts/batch", json={"items": items}).json() == saved
    assert client.get(f"/api/summary?day={DAY}").json()["completed_minutes"] == 70
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/workouts?day={DAY}").json() == saved


@pytest.mark.parametrize("item, expected", [
    ({"name": "胸和三头", "status": "completed"}, ["请补充胸和三头的总时长（分钟）。"]),
    ({"name": "游泳", "minutes": 30}, ["请确认游泳是否已完成。"]),
    ({"name": "胸和三头"}, ["请补充胸和三头的总时长（分钟），并确认是否已完成。"]),
    ({"name": "游泳", "minutes": 30, "status": "planned"}, []),
])
def test_only_missing_required_fields_are_asked(client, model, item, expected):
    register(client)
    model.response = json.dumps({"items": [item | {"source_text": "synthetic incomplete session"}], "questions": ["强度如何？", "请填写组数和休息时长"]})
    response = client.post("/api/workouts/parse-text", json={"text": "synthetic incomplete session"})
    assert response.status_code == 200
    assert response.json()["questions"] == expected
    assert response.json()["items"][0]["intensity"] == "normal_assumed"


@pytest.mark.parametrize("intensity, expected", [("unknown", "normal_assumed"), ("normal_assumed", "normal_assumed"),
    ("normal", "normal"), ("较低强度", "light"), ("过高强度", "vigorous"), ("moderate", "moderate")])
def test_known_intensity_words_and_assumption_are_distinct(client, model, intensity, expected):
    register(client)
    model.response = json.dumps({"items": [{"name": "胸和三头", "minutes": 40, "status": "completed", "intensity": intensity, "details": "力竭", "source_text": "synthetic intensity fixture"}]})
    response = client.post("/api/workouts/parse-text", json={"text": "synthetic intensity fixture"})
    assert response.json()["items"][0]["intensity"] == expected
    assert response.json()["items"][0]["details"] == "力竭"


def test_default_intensity_provenance_and_preview_binding(client, model):
    register(client)
    p = preview(client, name="胸和三头力量训练", intensity="normal_assumed", minutes=40)
    assert "默认假设" in model.calls[0]["system_prompt"]
    assert "未说明强度" in p["estimate"]["assumptions"][0]
    assert client.post("/api/workouts", json=record(p, intensity="normal")).status_code == 409
    saved = client.post("/api/workouts", json=record(p)).json()
    assert saved["intensity"] == "normal_assumed"
    assert saved["calorie_estimate"]["assumptions"] == p["estimate"]["assumptions"]
    assert client.put(f"/api/workouts/{saved['id']}", json=edit(saved, intensity="normal")).json().get("calorie_estimate") is None


def test_invalid_model_intensity_is_not_silently_accepted_or_echoed(client, model):
    register(client)
    model.response = json.dumps({"items": [{"name": "Walk", "minutes": 30, "intensity": "private-invalid-value"}]})
    response = client.post("/api/workouts/parse-text", json={"text": "private fixture"})
    assert response.status_code == 502
    assert "强度" in response.text and "手动填写" in response.text
    assert "private" not in response.text


def test_legacy_unknown_intensity_snapshot_not_rewritten(client, model):
    register(client)
    p = preview(client, intensity="unknown")
    saved = client.post("/api/workouts", json=record(p)).json()
    updated = client.put(f"/api/workouts/{saved['id']}", json=edit(saved, notes="Only a note")).json()
    assert updated["intensity"] == "unknown"
    assert updated["calorie_estimate"] == saved["calorie_estimate"]


def test_separate_durations_remain_three_rows(client, model):
    register(client)
    text = "我今天上午练了胸30分钟，我练了肩20分钟，晚上练了二头和三头50分钟。"
    items = [
        {"name": "胸", "minutes": 30, "status": "completed", "details": "上午", "source_text": "我今天上午练了胸30分钟"},
        {"name": "肩", "minutes": 20, "status": "completed", "details": "上午", "source_text": "我练了肩20分钟"},
        {"name": "二头和三头", "minutes": 50, "status": "completed", "details": "晚上", "source_text": "晚上练了二头和三头50分钟"},
    ]
    model.response = json.dumps({"items": items})
    response = client.post("/api/workouts/parse-text", json={"text": text})
    assert response.status_code == 200
    parsed = response.json()["items"]
    assert [item["minutes"] for item in parsed] == [30, 20, 50]
    assert all("source_text" not in item for item in parsed)
    assert "禁止相加" in model.calls[0]["system_prompt"]
    saved = client.post("/api/workouts/batch", json={"items": [workout(**item) for item in parsed]}).json()
    assert [item["minutes"] for item in saved] == [30, 20, 50]
    assert client.get(f"/api/summary?day={DAY}").json()["completed_minutes"] == 100


@pytest.mark.parametrize("items", [
    [{"name": "胸和肩", "minutes": 50, "source_text": "胸30分钟，肩20分钟"}],
    [{"name": "胸和肩", "minutes": 50, "source_text": "胸30分钟"}],
    [{"name": "胸和肩", "minutes": 50, "source_text": "胸和肩50分钟"}],
    [{"name": "胸", "minutes": 30, "source_text": "胸30分钟"}] * 2,
    [{"name": "肩", "minutes": 20, "source_text": "肩20分钟"}, {"name": "胸", "minutes": 30, "source_text": "胸30分钟"}],
    [{"name": "胸和肩", "minutes": 50}],
])
def test_bad_time_evidence_rejected_without_saving_or_retry(client, model, items):
    register(client)
    model.response = json.dumps({"items": items})
    response = client.post("/api/workouts/parse-text", json={"text": "胸30分钟，肩20分钟"})
    assert response.status_code == 502
    assert len(model.calls) == 1
    assert client.get(f"/api/workouts?day={DAY}").json() == []


@pytest.mark.parametrize("text, minutes", [("胸和肩共50分钟", 50), ("胸和三头四十分钟", 40),
    ("跑步半小时", 30), ("骑车1小时30分钟", 90), ("力量训练一个小时", 60), ("慢跑0.5小时", 30)])
def test_single_shared_or_compound_time_not_split(client, model, text, minutes):
    register(client)
    model.response = json.dumps({"items": [{"name": "合成训练", "minutes": minutes, "status": "completed", "source_text": text}]})
    response = client.post("/api/workouts/parse-text", json={"text": text})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["minutes"] == minutes
