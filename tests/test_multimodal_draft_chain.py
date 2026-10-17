"""Full draft lifecycle with fixed text/ASR/vision responses, never real devices."""
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import DAY, application, client, register
from test_draft_flow import create, changes, confirm_body
from test_meal_photos import PhotoModel, upload
from test_portions_speech import wav


@pytest.mark.parametrize("input_kind", ["text", "speech", "photo"])
def test_input_correction_explicit_commit_isolation_and_restart(client, application, monkeypatch, input_kind):
    register(client)
    text_calls, speech_calls = [], []

    class TextModel:
        def generate(self, **kwargs):
            text_calls.append(kwargs["message"])
            corrected = "没吃鸡蛋" in kwargs["message"]
            return json.dumps({"input_type": "text", "items": [{
                "name": "牛奶" if corrected else "鸡蛋", "grams": None,
                "amount_description": "250毫升" if corrected else "两个",
            }]})

    class SpeechModel:
        def transcribe(self, audio):
            speech_calls.append(audio)
            return "早餐吃了两个鸡蛋"

    monkeypatch.setattr("api.routes.create_meal_text_model", lambda _: TextModel())
    monkeypatch.setattr("api.routes.QwenSpeechModel", lambda _: SpeechModel())
    photo_model = PhotoModel()
    monkeypatch.setattr("api.meal_photos.QwenVisionModel", lambda _: photo_model)
    text = "早餐吃了两个鸡蛋"
    if input_kind == "speech":
        result = client.post("/api/speech/transcribe", content=wav(), headers={"content-type": "audio/wav"})
        assert result.status_code == 200, result.text
        text = result.json()["text"]
        assert client.get("/api/meal-drafts").json() == []
    draft = create(client, text=text)
    root = f"/api/meal-drafts/{draft['id']}"
    if input_kind == "photo":
        application.state.settings = replace(application.state.settings, model_provider="qwen",
                                              photo_enabled=True, qwen_api_key="synthetic-key")
        response = upload(client, draft)
    else:
        response = client.post(root + "/parse", json={"version": draft["version"]})
    assert response.status_code == 200, response.text
    initial = response.json()
    assert initial["items"][0]["name"] == "鸡蛋"
    assert client.get(f"/api/meals?day={DAY}").json() == []

    response = client.put(root, json=changes(initial, text="纠正：实际喝了牛奶250毫升，没吃鸡蛋", items=[]))
    assert response.status_code == 200, response.text
    corrected = response.json()
    parsed = client.post(root + "/parse", json={"version": corrected["version"]})
    assert parsed.status_code == 200, parsed.text
    parsed = parsed.json()
    assert [item["name"] for item in parsed["items"]] == ["牛奶"]
    assert text_calls[-1] == corrected["text"]
    final = client.put(root, json=changes(parsed, items=[{
        "name": "牛奶", "grams": None, "amount_description": "200毫升",
    }])).json()
    assert client.post(root + "/confirm", json=confirm_body(parsed)).status_code == 409
    assert client.get(f"/api/meals?day={DAY}").json() == []

    with TestClient(application) as other:
        register(other, "draft_chain_other")
        assert other.get(root).status_code == 404
        assert other.put(root, json=changes(final)).status_code == 404
        assert other.post(root + "/parse", json={"version": final["version"]}).status_code == 404
        assert other.post(root + "/confirm", json=confirm_body(final)).status_code == 404
        assert other.get(f"/api/meals?day={DAY}").json() == []

    # Restart while still unconfirmed, then save exactly the final human edit.
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(root).json() == final
        assert restarted.get(f"/api/meals?day={DAY}").json() == []
        body = confirm_body(final)
        saved = restarted.post(root + "/confirm", json=body)
        assert saved.status_code == 200, saved.text
        records = saved.json()["records"]
        assert len(records) == 1 and records[0]["name"] == "牛奶"
        assert records[0]["amount_description"] == "200毫升" and records[0]["grams"] is None
        assert restarted.post(root + "/confirm", json=body).json() == saved.json()
        assert len(restarted.get(f"/api/meals?day={DAY}").json()) == 1
    assert len(speech_calls) == (input_kind == "speech")
    assert len(photo_model.requests) == (input_kind == "photo")
    assert len(text_calls) == (1 if input_kind == "photo" else 2)
