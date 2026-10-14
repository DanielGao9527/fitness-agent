"""One bounded Qwen call on a disposable account/database; never prints credentials."""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from services.business_time import business_today


BASE = "/api/coach/conversations"


def checked(response):
    if response.status_code >= 400:
        detail = response.json().get("detail", {})
        code = detail.get("code", "unknown") if isinstance(detail, dict) else "unknown"
        raise RuntimeError(f"API returned HTTP {response.status_code} ({code})")
    return response.json()


def conversation(client):
    return checked(client.post(BASE, json={"client_id": str(uuid4()), "day": business_today().isoformat()}))


def send(client, current, message):
    return checked(client.post(f"{BASE}/{current['id']}/messages", json={
        "client_id": str(uuid4()), "version": current["version"], "message": message}))


def recommendation(client, current):
    answer = checked(client.post(f"{BASE}/{current['id']}/recommend-training", json={
        "client_id": str(uuid4()), "version": current["version"]}))
    return answer["turns"][-1]["response"]["training_recommendation"]


def main():
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise SystemExit("DASHSCOPE_API_KEY is absent; live validation not run")
    with TemporaryDirectory(prefix="fitness-memory-live-") as directory:
        settings = Settings(database_path=Path(directory) / "personal.sqlite3",
                            knowledge_index_path=Path(directory) / "knowledge.sqlite3",
                            retrieval_mode="lexical", model_provider="qwen", qwen_api_key=key,
                            coach_enabled=True, training_plan_enabled=True,
                            ai_user_daily_limit=1, ai_global_daily_limit=1,
                            registration_enabled=True, guest_enabled=False)
        app = create_app(settings)
        with TestClient(app) as client:
            checked(client.post("/api/auth/register", json={
                "username": "memory_live_check", "password": "synthetic-only-password-42"}))
            old = send(client, conversation(client), "在家，只有哑铃")
            reviewed = checked(client.post(f"{BASE}/{old['id']}/understand", json={
                "client_id": str(uuid4()), "version": old["version"]}))
            review = reviewed["review"]
            if review["status"] != "ready" or review.get("profile_equipment_on_confirmation") != "在家，只有哑铃":
                raise RuntimeError(f"Real model understanding failed review gate: {review['status']}")
            checked(client.post(f"{BASE}/{old['id']}/confirm-understanding", json={
                "version": old["version"], "review_id": review["id"], "reviewed": True}))
            new = send(client, conversation(client), "今天练什么")
            first = recommendation(client, new)
            if first["status"] != "ready" or first.get("equipment") != "在家，只有哑铃" or not first["exercises"]:
                raise RuntimeError("New-session recommendation did not use confirmed dumbbells")
            checked(client.put("/api/profile", json={"equipment": "在家，徒手"}))
            old_view = checked(client.get(f"{BASE}/{old['id']}"))
            old_search = checked(client.post(f"{BASE}/memory/search", json={"query": "哑铃"}))
            second = recommendation(client, new)
            if (old_view["training_context"].get("equipment") == "在家，只有哑铃"
                    or old_search["items"] or second["status"] != "ready"
                    or second.get("equipment") != "在家，徒手"
                    or not second["exercises"]
                    or any(row["equipment"] != ["floor"] for row in second["exercises"])):
                raise RuntimeError("Correction did not invalidate old equipment throughout the final recommendation")
            print(json.dumps({"real_model_review": review["status"],
                              "new_session": first["status"], "old_equipment": first["equipment"],
                              "corrected_session": second["status"], "corrected_equipment": second["equipment"],
                              "stale_history_hits": len(old_search["items"]), "max_model_calls": 1}, ensure_ascii=False))


if __name__ == "__main__":
    main()
