import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from model.factory import ModelError, create_meal_text_model, meal_text_status
from model.qwen import QwenTextModel

SECRET = "test-key-not-real"
DAY = "2026-09-16"
TEXT = "I ate 100 g of eggs and one cup of milk, not toast. I plan to eat rice."
DRAFT = {
    "input_type": "text",
    "items": [
        {"name": "Eggs", "grams": 100, "amount_description": "100 g"},
        {"name": "Milk", "grams": None, "amount_description": "one cup"},
    ],
}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(database_path=tmp_path / "test.sqlite3"))) as result:
        assert result.post("/api/auth/register", json={
            "username": "alice", "password": "test-password-123"
        }).status_code == 201
        yield result


def configured(settings):
    return replace(settings, model_provider="qwen", qwen_api_key=SECRET)


def install_transport(monkeypatch, client, handler):
    model = QwenTextModel(configured(client.app.state.settings), transport=httpx.MockTransport(handler))
    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: model)


def completion(content, finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{
        "finish_reason": finish_reason, "message": {"content": content}
    }]})


def test_text_request_and_transient_draft(monkeypatch, client):
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert str(request.url) == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        assert body["model"] == "qwen3.7-plus"
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is False
        assert "enable_search" not in body and "tools" not in body
        assert body["max_tokens"] == 2048
        assert body["stream"] is False
        assert len(body["messages"]) == 2
        assert body["messages"][1] == {"role": "user", "content": TEXT}
        assert "JSON Schema" in body["messages"][0]["content"]
        assert "other-private-profile" not in request.content.decode()
        assert "alice-private-profile" not in request.content.decode()
        assert request.extensions["timeout"]["read"] == 20
        return completion(json.dumps(DRAFT))

    client.put("/api/profile", json={"display_name": "alice-private-profile"})
    with TestClient(client.app) as other:
        other.post("/api/auth/register", json={"username": "other", "password": "test-password-456"})
        other.put("/api/profile", json={"display_name": "other-private-profile"})
    install_transport(monkeypatch, client, handler)
    response = client.post("/api/meal-drafts/parse-text", json={"text": TEXT})
    assert response.status_code == 200
    draft = response.json()
    assert [item["name"] for item in draft["items"]] == ["Eggs", "Milk"]
    assert draft["items"][1]["grams"] is None
    assert draft["questions"] == []  # A named food and one cup no longer require weighing.
    assert draft["status"] == "awaiting_confirmation"
    assert all(item["confidence"] == "needs_confirmation" for item in draft["items"])
    assert len(calls) == 1
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"/api/meals?day={DAY}").json() == []
    with client.app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
    # The existing commit boundary still rejects an unknown portion.
    assert client.post("/api/meals", json={
        "day": DAY, "meal_type": "breakfast", "name": "Milk", "grams": None,
        "client_id": "9dd7142d-cc0f-4148-93ec-0a27c05b67fc",
    }).status_code == 422


@pytest.mark.parametrize("body", [{"text": " "}, {"text": "x" * 2001}, {"text": TEXT, "user_id": 2}])
def test_input_rejected_before_model(monkeypatch, client, body):
    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: pytest.fail("Model must not run"))
    assert client.post("/api/meal-drafts/parse-text", json=body).status_code == 422


def test_login_required_and_no_key_mode(monkeypatch, client):
    response = client.post("/api/meal-drafts/parse-text", json={"text": TEXT})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_NOT_CONFIGURED"
    monkeypatch.setattr("api.routes.create_meal_text_model", lambda settings: pytest.fail("Model must not run"))
    client.cookies.clear()
    assert client.post("/api/meal-drafts/parse-text", json={"text": TEXT}).status_code == 401


@pytest.mark.parametrize("content", [
    "not JSON", "[]", '{"input_type":"photo"}',
    '{"input_type":"text","items":[{"name":"Milk","grams":-1}]}',
    '{"input_type":"text","items":[{"name":"Milk","grams":true}]}',
    '{"input_type":"text","items":[{"name":"Milk","kcal":60}]}',
    '{"input_type":"text","status":"committed"}',
    json.dumps({"input_type": "text", "items": [{"name": "Milk"}] * 31}),
    json.dumps({"input_type": "text", "questions": ["x" * 241]}),
])
def test_invalid_output_is_not_a_success(monkeypatch, client, content):
    install_transport(monkeypatch, client, lambda request: completion(content))
    response = client.post("/api/meal-drafts/parse-text", json={"text": TEXT})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MODEL_INVALID_OUTPUT"
    assert client.get(f"/api/meals?day={DAY}").json() == []


@pytest.mark.parametrize("response", [
    completion("{}", "length"), completion(None), httpx.Response(200, json={}),
    httpx.Response(200, content="not JSON"), completion("x" * 24001),
])
def test_incomplete_provider_envelope(monkeypatch, client, response):
    install_transport(monkeypatch, client, lambda request: response)
    result = client.post("/api/meal-drafts/parse-text", json={"text": TEXT})
    assert result.status_code == 502
    assert result.json()["detail"]["code"] == "MODEL_INVALID_OUTPUT"


@pytest.mark.parametrize("upstream,status,code", [
    (401, 503, "MODEL_AUTH_ERROR"), (403, 503, "MODEL_AUTH_ERROR"),
    (429, 429, "MODEL_RATE_LIMITED"), (500, 502, "MODEL_UPSTREAM_ERROR"),
    (400, 502, "MODEL_UPSTREAM_ERROR"), (302, 502, "MODEL_UPSTREAM_ERROR"),
])
def test_provider_errors_are_redacted_without_retries(monkeypatch, client, upstream, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(upstream, text=SECRET + TEXT, headers={"Location": "https://unrelated.example"})

    install_transport(monkeypatch, client, handler)
    response = client.post("/api/meal-drafts/parse-text", json={"text": TEXT})
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert SECRET not in response.text and TEXT not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize("exception,status,code", [
    (httpx.ReadTimeout, 504, "MODEL_TIMEOUT"), (httpx.ConnectError, 502, "MODEL_UNAVAILABLE"),
])
def test_network_failures(monkeypatch, client, exception, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception(SECRET, request=request)

    install_transport(monkeypatch, client, handler)
    response = client.post("/api/meal-drafts/parse-text", json={"text": TEXT})
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert SECRET not in response.text
    assert len(calls) == 1


def test_empty_result_has_question(monkeypatch, client):
    install_transport(monkeypatch, client, lambda request: completion('{"input_type":"text","items":[]}'))
    result = client.post("/api/meal-drafts/parse-text", json={"text": "I have not eaten yet."}).json()
    assert result["items"] == [] and result["questions"]


@pytest.mark.parametrize("url", [
    "http://dashscope.aliyuncs.com/compatible-mode/v1", "https://unrelated.example/compatible-mode/v1",
    "https://dashscope.aliyuncs.com/wrong-path", "https://dashscope.aliyuncs.com/compatible-mode/v1?key=secret",
    "https://user:secret@dashscope.aliyuncs.com/compatible-mode/v1", "https://[broken",
])
def test_invalid_configuration_is_explicit(client, url):
    settings = replace(configured(client.app.state.settings), qwen_base_url=url)
    assert meal_text_status(settings) == "configuration_error"
    with pytest.raises(ModelError):
        create_meal_text_model(settings)


def test_configuration_is_lazy_and_secret_free(monkeypatch, client):
    settings = configured(client.app.state.settings)
    client.app.state.settings = settings
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("Health must not call model"))
    health = client.get("/api/health")
    assert health.json()["meal_text"] == "configured_unverified"
    assert health.json()["model"] == "not_configured"
    assert SECRET not in health.text and SECRET not in repr(settings)
    assert meal_text_status(replace(settings, qwen_api_key="")) == "not_configured"
    assert meal_text_status(replace(settings, model_provider="unknown")) == "configuration_error"
    assert isinstance(create_meal_text_model(replace(settings,
        qwen_base_url="https://test-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )), QwenTextModel)
    monkeypatch.setenv("FITNESS_MODEL_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    monkeypatch.setenv("FITNESS_QWEN_MODEL", "qwen-flash")
    monkeypatch.setenv("FITNESS_QWEN_BASE_URL", settings.qwen_base_url)
    assert Settings.from_env().qwen_model == "qwen-flash"
    assert Settings.from_env().qwen_api_key == SECRET


def test_plus_default_keeps_asr_and_explicit_model_override(monkeypatch, tmp_path):
    monkeypatch.delenv("FITNESS_QWEN_MODEL", raising=False)
    monkeypatch.delenv("FITNESS_QWEN_ASR_MODEL", raising=False)
    assert Settings.from_env().qwen_model == "qwen3.7-plus"
    assert Settings(database_path=tmp_path / "test.sqlite3").qwen_model == "qwen3.7-plus"
    assert Settings.from_env().qwen_asr_model == "qwen3-asr-flash"
    monkeypatch.setenv("FITNESS_QWEN_MODEL", "qwen3.7-max")
    assert Settings.from_env().qwen_model == "qwen3.7-max"
