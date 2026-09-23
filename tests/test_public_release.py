"""Public deployment boundaries, using only temporary synthetic data."""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from test_foundation import PASSWORD, register


def test_shared_mode_hides_debug_docs_and_keeps_login_capabilities(tmp_path):
    local = Settings(database_path=tmp_path / "release.sqlite3")
    with TestClient(create_app(local)) as client:
        register(client)
        assert client.get("/openapi.json").status_code == 200
        assert "stage" in client.get("/api/health").json()
    shared = replace(local, access_mode="shared", cookie_secure=True,
                     registration_enabled=False, allowed_hosts=("fitness.example.com",),
                     public_origin="https://fitness.example.com")
    with TestClient(create_app(shared), base_url=shared.public_origin) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["registration_enabled"] is False
        assert not {"stage", "text_model", "model", "coach"} & health.keys()
        assert {"meal_text", "speech", "photo", "nutrition", "workout",
                "knowledge_qa", "meal_plan", "training_plan", "coach_understanding"} <= health.keys()
        assert client.get("/api/profile").status_code == 401
        login = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert login.status_code == 200
        assert "Secure" in login.headers["set-cookie"]
        assert client.get("/api/profile").status_code == 200
        assert client.post("/api/auth/register", json={"username": "bob", "password": PASSWORD}).status_code == 403


def send_chunks(app, chunks, *, content_length=None):
    messages, consumed = [], []

    async def receive():
        index = len(consumed)
        if index == len(chunks):
            return {"type": "http.disconnect"}
        consumed.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    async def send(message):
        messages.append(message)

    headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "method": "POST", "scheme": "http", "path": "/api/auth/login",
             "raw_path": b"/api/auth/login", "query_string": b"", "root_path": "",
             "http_version": "1.1", "headers": headers,
             "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}
    asyncio.run(app(scope, receive, send))
    return next(item["status"] for item in messages if item["type"] == "http.response.start"), consumed, messages


@pytest.mark.parametrize("content_length", [None, 1])
def test_oversized_stream_stops_reading_before_remaining_chunks(tmp_path, content_length):
    app = create_app(Settings(database_path=tmp_path / "unused.sqlite3"))
    status, consumed, _ = send_chunks(app, [b" " * 65536, b"x", b" " * 1048576],
                                      content_length=content_length)
    assert status == 413
    assert consumed == [0, 1]


@pytest.mark.parametrize("size", [32, 65536])
def test_bounded_stream_reaches_json_validation_intact(tmp_path, size):
    app = create_app(Settings(database_path=tmp_path / "unused.sqlite3"))
    payload = b'{"username":"alice"}'
    status, consumed, messages = send_chunks(app, [payload[:8], payload[8:] + b" " * (size - len(payload))])
    assert status == 422
    assert consumed == [0, 1]
    body = b"".join(item.get("body", b"") for item in messages)
    assert b'"password"' in body
    assert b'"username"' not in body
