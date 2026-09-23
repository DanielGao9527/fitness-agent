import base64
import io
import json
import sqlite3
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from database import Database, SCHEMA, DRAFT_SCHEMA
from model.factory import ModelError
from model.speech import QwenSpeechModel, validate_audio
from schemas import portion_issue
from services.usage import reserve_call
from services.meal_drafts import MealDraftService
from test_draft_flow import create, changes, confirm_body
from test_foundation import DAY, application, client, meal, register


def wav(seconds=1, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * int(rate * seconds) * channels)
    return output.getvalue()


@pytest.mark.parametrize("name,amount", [("鸡蛋", "两个"), ("麻婆豆腐", "一盘"), ("水煮鱼", "单人份"),
    ("宫保鸡丁", "双人份，自己吃了半盘"), ("牛奶", "250毫升"), ("米饭", "半碗"), ("Milk", "one cup")])
def test_named_food_and_portion_can_be_recorded(client, name, amount):
    register(client)
    body = meal(name=name, grams=None, amount_description=amount)
    response = client.post("/api/meals", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["grams"] is None
    assert response.json()["amount_description"] == amount
    assert client.post("/api/meals", json=body).json() == response.json()
    summary = client.get(f"/api/summary?day={DAY}").json()
    assert summary["nutrition"]["kcal"] == {"known_total": None, "missing_count": 1}


@pytest.mark.parametrize("name,amount", [("炒菜", "一盘"), ("一盘炒菜", "一盘"), ("东西", "两份"),
    ("鸡蛋", ""), ("鸡蛋", "不知道"), ("鸡蛋", "一点"), ("鸡蛋", "0个"), ("鸡蛋", "-1个")])
def test_incomplete_portion_stays_draft(client, name, amount):
    register(client)
    assert client.post("/api/meals", json=meal(name=name, grams=None, amount_description=amount)).status_code == 422
    draft = create(client)
    root = f"/api/meal-drafts/{draft['id']}"
    draft = client.put(root, json=changes(draft, items=[{"name":name, "grams":None, "amount_description":amount}])).json()
    assert draft["status"] == "needs_input"
    assert client.post(root + "/confirm", json=confirm_body(draft)).status_code == 422


def test_portion_draft_confirmation_and_mixed_summary(client):
    register(client)
    draft = create(client)
    root = f"/api/meal-drafts/{draft['id']}"
    draft = client.put(root, json=changes(draft, items=[{"name":"鸡蛋", "grams":None, "amount_description":"两个"}])).json()
    assert draft["status"] == "ready"
    body = confirm_body(draft)
    saved = client.post(root + "/confirm", json=body).json()
    assert saved["records"][0]["amount_description"] == "两个"
    assert client.post(root + "/confirm", json=body).json() == saved
    client.post("/api/meals", json=meal(grams=100, kcal_per_100g=50, source="test label"))
    client.post("/api/meals", json=meal(grams=None, amount_description="一杯", kcal_per_100g=100, source="test label"))
    assert client.get(f"/api/summary?day={DAY}").json()["nutrition"]["kcal"] == {"known_total":50, "missing_count":2}


def test_legacy_retry_and_v2_migration(client, application, tmp_path):
    register(client)
    body = meal()
    first = client.post("/api/meals", json=body).json()
    with application.state.database.connect() as connection:
        payload = json.loads(connection.execute("SELECT payload FROM meals").fetchone()[0])
        payload.pop("amount_description")
        connection.execute("UPDATE meals SET payload=?", (json.dumps(payload),))
    assert client.post("/api/meals", json=body).json()["id"] == first["id"]
    path = tmp_path / "v2.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA + DRAFT_SCHEMA + "PRAGMA user_version=2;")
    Database(path).initialize()
    with sqlite3.connect(path.with_name(path.name + ".pre-v3.bak")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2


def test_audio_contract_and_error_sanitizing(tmp_path):
    settings = Settings(database_path=tmp_path / "none", qwen_api_key="private-test-key", speech_enabled=True)
    data = wav()
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["model"] == "qwen3-asr-flash" and body["stream"] is False
        assert "response_format" not in body and "enable_thinking" not in body
        encoded = body["messages"][0]["content"][0]["input_audio"]["data"]
        assert base64.b64decode(encoded.split(",")[1]) == data
        return httpx.Response(200, json={"choices":[{"finish_reason":"stop", "message":{"content":"早餐吃了两个鸡蛋。"}}]})
    model = QwenSpeechModel(settings, transport=httpx.MockTransport(handler))
    assert model.transcribe(data) == "早餐吃了两个鸡蛋。"
    assert len(calls) == 1
    for status in [401, 403, 429, 500]:
        model = QwenSpeechModel(settings, transport=httpx.MockTransport(lambda request: httpx.Response(status, text="private-test-key")))
        with pytest.raises(ModelError) as error:
            model.transcribe(data)
        assert "private-test-key" not in str(error.value)


@pytest.mark.parametrize("data", [b"invalid", wav(31), wav(rate=8000), wav(channels=2), wav()[:-20]], ids=["invalid", "too-long", "wrong-rate", "stereo", "truncated"])
def test_audio_limits(data):
    with pytest.raises(HTTPException):
        validate_audio(data)


def test_speech_route_boundaries_and_no_record_writes(client, application, monkeypatch):
    audio = wav()
    headers = {"content-type":"audio/wav"}
    assert client.post("/api/speech/transcribe", content=audio, headers=headers).status_code == 401
    register(client)
    assert client.post("/api/speech/transcribe", json={}).status_code == 415
    assert client.post("/api/speech/transcribe", content=audio, headers={**headers,"origin":"https://other.invalid"}).status_code == 403
    assert client.post("/api/speech/transcribe", content=b"x" * 1_000_001, headers=headers).status_code == 413
    assert client.post("/api/speech/transcribe", content=b"bad", headers=headers).status_code == 422
    assert client.post("/api/speech/transcribe", content=audio, headers=headers).status_code == 503
    class Model:
        def transcribe(self, data):
            assert data == audio
            return "午餐一盘麻婆豆腐"
    monkeypatch.setattr("api.routes.QwenSpeechModel", lambda settings: Model())
    result = client.post("/api/speech/transcribe", content=audio, headers=headers)
    assert result.json() == {"text":"午餐一盘麻婆豆腐"}
    assert result.headers["cache-control"] == "no-store"
    assert client.get("/api/meal-drafts").json() == []
    assert client.get(f"/api/meals?day={DAY}").json() == []
    with application.state.database.connect() as connection:
        assert connection.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0] == 1


def test_usage_atomic_global_and_restart(client, application):
    user = register(client)
    limits = replace(application.state.settings, ai_user_daily_limit=1, ai_global_daily_limit=1)
    def attempt(_):
        try:
            reserve_call(application.state.database, limits, user["id"])
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 429]
    assert attempt(0) == 429
    Database(limits.database_path).initialize()
    other = register(client, "other")
    with pytest.raises(HTTPException):
        reserve_call(application.state.database, limits, other["id"])


def test_speech_provider_errors_preserve_inputs(client, application, monkeypatch):
    register(client)
    class Model:
        def transcribe(self, data):
            raise ModelError("MODEL_TIMEOUT", "Timeout", 504)
    monkeypatch.setattr("api.routes.QwenSpeechModel", lambda settings: Model())
    response = client.post("/api/speech/transcribe", content=wav(), headers={"content-type":"audio/wav"})
    assert response.status_code == 504
    assert client.get("/api/meal-drafts").json() == []
    assert client.get(f"/api/meals?day={DAY}").json() == []


def test_paid_routes_share_daily_budget(client, application, monkeypatch):
    register(client)
    application.state.settings = replace(application.state.settings, ai_user_daily_limit=1)
    calls = []
    class Model:
        def generate(self, **kwargs):
            calls.append("text")
            return '{"input_type":"text","items":[{"name":"鸡蛋","amount_description":"两个"}]}'
        def transcribe(self, data):
            calls.append("speech")
            return "text"
    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: Model())
    monkeypatch.setattr("api.routes.QwenSpeechModel", lambda settings: Model())
    assert client.post("/api/meal-drafts/parse-text", json={"text":"两个鸡蛋"}).status_code == 200
    assert client.post("/api/speech/transcribe", content=wav(), headers={"content-type":"audio/wav"}).status_code == 429
    draft = create(client)
    assert client.post(f"/api/meal-drafts/{draft['id']}/parse", json={"version":1}).status_code == 429
    assert calls == ["text"]
    assert client.post("/api/meals", json=meal()).status_code == 201


def test_asr_timeout_bad_output_and_no_retries(tmp_path):
    settings = Settings(database_path=tmp_path / "none", qwen_api_key="test-only-key", speech_enabled=True)
    calls = []
    def timeout(request):
        calls.append(1)
        raise httpx.ReadTimeout("private-provider-details")
    with pytest.raises(ModelError) as error:
        QwenSpeechModel(settings, transport=httpx.MockTransport(timeout)).transcribe(wav())
    assert error.value.code == "MODEL_TIMEOUT" and len(calls) == 1
    for content in ["", "x" * 2001, {"text":"not a string"}]:
        response = httpx.Response(200, json={"choices":[{"finish_reason":"stop","message":{"content":content}}]})
        with pytest.raises(ModelError):
            QwenSpeechModel(settings, transport=httpx.MockTransport(lambda request: response)).transcribe(wav())


def test_missing_portion_question_requests_basic_amount_not_weight():
    class Model:
        def generate(self, **kwargs):
            return json.dumps({"input_type":"text", "items":[{"name":"宫保鸡丁"}], "questions":["多少克？"]})
    result = MealDraftService(Model()).parse_text("宫保鸡丁")
    assert result.questions == ["请补充基本份量，例如2个、半碗、一盘或单人份"]
