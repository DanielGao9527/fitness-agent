import io
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import api.meal_photos as photo_api
from app import create_app
from config import Settings
from model.factory import ModelError
from model.vision import normalize_photo, QwenVisionModel, photo_status
from test_foundation import DAY, application, client, register
from test_draft_flow import create, changes, confirm_body
from test_knowledge_answers import calls


def picture(format="JPEG", size=(100, 80), **kwargs):
    stream = io.BytesIO()
    Image.new("RGB", size, "white").save(stream, format=format, **kwargs)
    return stream.getvalue()


class PhotoModel:
    def __init__(self):
        self.requests = []
        self.on_call = None
        self.output = json.dumps({"input_type": "photo", "items": [{"name": "鸡蛋", "grams": 100,
                                 "amount_description": "2个", "confidence": "needs_confirmation"}], "questions": []})

    def generate(self, **kwargs):
        self.requests.append(kwargs)
        if self.on_call:
            self.on_call()
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


@pytest.fixture
def photos(client, application, monkeypatch):
    register(client)
    application.state.settings = replace(application.state.settings, model_provider="qwen", photo_enabled=True,
                                         qwen_api_key="synthetic-key")
    model = PhotoModel()
    monkeypatch.setattr(photo_api, "QwenVisionModel", lambda _: model)
    return client, application, model


def upload(client, draft, *, request_id=None, data=None, mime="image/jpeg", headers=None):
    return client.post(f"/api/meal-drafts/{draft['id']}/photo", params={"client_id": request_id or str(uuid4()),
                       "version": draft["version"]}, content=picture() if data is None else data,
                       headers={"content-type": mime, **(headers or {})})


def test_photo_requires_actual_portion_and_explicit_commit(photos):
    client, app, model = photos
    initial = create(client, text="合成测试照片")
    key = str(uuid4())
    response = upload(client, initial, request_id=key)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["input_type"] == "photo" and draft["status"] == "needs_input"
    assert draft["items"][0]["grams"] is None and draft["items"][0]["amount_description"] == ""
    assert client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft)).status_code == 422
    assert upload(client, initial, request_id=key).json() == draft
    assert len(model.requests) == calls(app) == 1
    assert "data:image" not in json.dumps(draft) and "synthetic-key" not in json.dumps(draft)
    assert client.get(f"/api/meals?day={DAY}").json() == []
    body = changes(draft, items=[{"name": "鸡蛋", "amount_description": "1个", "grams": None}])
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=body).json()
    saved = client.post(f"/api/meal-drafts/{draft['id']}/confirm", json=confirm_body(draft))
    assert saved.status_code == 200 and len(saved.json()["records"]) == 1
    assert saved.json()["records"][0]["amount_description"] == "1个"
    with TestClient(create_app(app.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/meal-drafts/{draft['id']}").json()["status"] == "committed"


@pytest.mark.parametrize("data,mime,status", [(b"bad", "image/jpeg", 422), (b"x", "text/plain", 415),
    (b"", "image/jpeg", 413), (picture("PNG"), "image/jpeg", 422),
    (picture(size=(8, 8)), "image/jpeg", 422), (b"x" * (10 * 1024 * 1024 + 1), "image/jpeg", 413)],
    ids=["corrupt", "mime", "empty", "mismatch", "tiny", "too-large"])
def test_invalid_images_never_call(photos, data, mime, status):
    client, app, model = photos
    draft = create(client)
    response = upload(client, draft, data=data, mime=mime)
    assert response.status_code == status, response.text
    assert calls(app) == 0 and not model.requests
    assert client.get(f"/api/meal-drafts/{draft['id']}").json() == draft


def test_metadata_removed_and_size_bounded():
    exif = Image.Exif()
    exif[270] = "private camera information"
    raw = picture(size=(2400, 1800), exif=exif)
    cleaned = normalize_photo(raw, "image/jpeg")
    with Image.open(io.BytesIO(cleaned)) as image:
        assert image.size == (1600, 1200) and not image.getexif()
    assert b"private camera information" not in cleaned
    with pytest.raises(HTTPException):
        normalize_photo(picture("PNG", (6000, 5000)), "image/png")


def test_failure_retry_stale_and_foreign_do_not_overwrite(photos):
    client, app, model = photos
    draft = create(client)
    draft = client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft)).json()
    key = str(uuid4())
    model.output = "{}"
    assert upload(client, draft, request_id=key).status_code == 502
    current = client.get(f"/api/meal-drafts/{draft['id']}").json()
    assert current["items"] == draft["items"] and current["version"] == draft["version"]
    assert upload(client, draft, request_id=key).status_code == 409
    assert len(model.requests) == 1
    with TestClient(app) as other:
        register(other, "photo_other")
        assert upload(other, draft).status_code == 404
    model.output = PhotoModel().output
    model.on_call = lambda: client.put(f"/api/meal-drafts/{draft['id']}", json=changes(draft, notes="new edit"))
    assert upload(client, draft).status_code == 409
    current = client.get(f"/api/meal-drafts/{draft['id']}").json()
    assert current["notes"] == "new edit" and current["items"] == draft["items"]


def test_simultaneous_request_blocked_and_origin_enforced(photos):
    client, app, model = photos
    draft = create(client)
    model.on_call = lambda: assert_blocked(client, draft)
    assert upload(client, draft, headers={"origin": "https://untrusted.example"}).status_code == 403
    assert upload(client, draft).status_code == 200
    assert len(model.requests) == calls(app) == 1


def assert_blocked(client, draft):
    assert upload(client, draft).status_code == 409


def test_unconfigured_budget_and_single_provider_request(client, application, monkeypatch):
    register(client)
    draft = create(client)
    assert upload(client, draft).status_code == 503 and calls(application) == 0
    settings = replace(application.state.settings, model_provider="qwen", photo_enabled=True, qwen_api_key="test-key")
    application.state.settings = replace(settings, ai_user_daily_limit=0)
    assert upload(client, draft).status_code == 429 and calls(application) == 0
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == settings.qwen_vision_model and body["enable_thinking"] is False
        assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(429, text="private provider details")
    with pytest.raises(ModelError) as caught:
        QwenVisionModel(settings, transport=httpx.MockTransport(handler)).generate(system_prompt="JSON", message="test", image=picture())
    assert caught.value.code == "MODEL_RATE_LIMITED" and len(requests) == 1
    assert "private provider" not in str(caught.value)
    assert photo_status(replace(settings, photo_enabled=False)) == "not_configured"
