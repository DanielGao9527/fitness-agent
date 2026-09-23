"""Explicit, capped live checks with synthetic data and an isolated database."""
import argparse
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from app import create_app
from config import ROOT, Settings
from model.factory import ModelError
from model.qwen import QwenTextModel

LEDGER = ROOT / "artifacts/r119-live-budget.sqlite3"
REPORT = ROOT / "artifacts/r119-live-report.json"
CASES = ("split", "equipment", "swim", "replacement", "meals", "meal-edit", "injury", "ambiguous")


class Budget:
    def __init__(self, path=LEDGER):
        self.path = path
        path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,phase TEXT,created_at TEXT,status TEXT)")

    def reserve(self, phase):
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= 30:
                raise ModelError("TEST_BUDGET_EXHAUSTED", "This round's 30-request ceiling reached", 429)
            return connection.execute("INSERT INTO calls(phase,created_at,status) VALUES (?,?,'started')",
                                      (phase, datetime.now(timezone.utc).isoformat())).lastrowid

    def finish(self, identifier, status):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE calls SET status=? WHERE id=?", (status, identifier))

    def count(self):
        with sqlite3.connect(self.path) as connection:
            return connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0]


def checked(response):
    if response.status_code >= 400:
        raise AssertionError(f"HTTP {response.status_code}: {response.text[:600]}")
    return response.json()


def run_case(client, case, trace):
    day = date.today().isoformat()
    base = "/api/coach/conversations"
    profile = {"weight_kg": 70, "height_cm": 175, "experience": "experienced", "training_days": 5,
               "minutes_per_session": 60, "training_split": "five", "food_allergies": "鸡肉过敏",
               "nutrition_reference": "regular_training", "equipment": "哑铃，可调训练凳，杠铃，卧推架，保护杠，泳池"}
    checked(client.put("/api/profile", json=profile))
    data = checked(client.post(base, json={"day": day, "client_id": str(uuid4())}))

    def send(message, review=True):
        nonlocal data
        data = checked(client.post(f"{base}/{data['id']}/messages", json={
            "version": data["version"], "client_id": str(uuid4()), "message": message}))
        trace.append({"message": message, "pending": data["pending"]})
        if data["pending"] and review:
            data = checked(client.post(f"{base}/{data['id']}/understand", json={
                "version": data["version"], "client_id": str(uuid4())}))
            trace.append({"review": data["review"]})
            assert data["review"]["status"] == "ready", data["review"].get("question")
            data = checked(client.post(f"{base}/{data['id']}/confirm-understanding", json={
                "version": data["version"], "review_id": data["review"]["id"], "reviewed": True}))
        return data

    def recommend(kind):
        nonlocal data
        data = checked(client.post(f"{base}/{data['id']}/recommend-{kind}", json={
            "version": data["version"], "client_id": str(uuid4())}))
        result = data["turns"][-1]["response"].get("training_recommendation") if kind == "training" else data["meal_plans"]
        trace.append({"recommendation": result})
        return result

    if case in ("meals", "meal-edit"):
        target = checked(client.get(f"/api/intake-target?day={day}"))
        checked(client.post("/api/intake-target", json={"client_id": str(uuid4()), "version": target["version"],
            "context_hash": target["context_hash"], "effective_from": day, "kcal": 2300,
            "source": "Synthetic target, not personal advice", "confirmed": True, "general_adult": True}))
        checked(client.post("/api/meals", json={"client_id": str(uuid4()), "day": day, "meal_type": "breakfast",
            "name": "测试早餐", "grams": 100, "kcal_per_100g": 500, "protein_per_100g": 25,
            "carbs_per_100g": 65, "fat_per_100g": 10, "source": "Synthetic fixture"}))
        consent = checked(client.get("/api/meal-plans/consent"))
        checked(client.put("/api/meal-plans/consent", json={"context_hash": consent["context_hash"],
            "adult_general_diet": True, "constraints_reviewed": True}))
        send("今天已吃的都记录好了，安排午餐和晚餐")
        plans = recommend("meals")
        assert len(plans) == 2 and all("chicken" not in {item["food_id"] for item in plan["items"]} for plan in plans)
        assert all(plan.get("quick_nutrition") for plan in plans)
        if case == "meal-edit":
            lunch = next(plan for plan in plans if plan["meal_type"] == "lunch")
            send("晚上想吃点虾肉，主食想换成玉米，中午那份别动")
            changed = recommend("meals")
            dinner = next(plan for plan in changed if plan["meal_type"] == "dinner" and not plan["stale"])
            assert {"shrimp", "corn"} <= {item["food_id"] for item in dinner["items"]}
            assert any(plan["id"] == lunch["id"] and not plan["stale"] for plan in changed)
        assert len(checked(client.get(f"/api/meals?day={day}"))) == 1
    elif case == "ambiguous":
        send("那个给我换一下吧", review=False)
        data = checked(client.post(f"{base}/{data['id']}/understand", json={"version": data["version"], "client_id": str(uuid4())}))
        trace.append({"review": data["review"]})
        assert data["pending"] and data["review"]["status"] == "needs_input"
    else:
        send("练胸")
        recommend("training")
        if case == "split":
            send("我还是想试试三分化，你帮我把这一周重新排一下")
            assert data["training_context"].get("split") == "ppl", "Split intent lost"
            assert not data["training_context"].get("focus"), "Old chest-only focus survived whole-week change"
        elif case == "equipment":
            send("今天是在家练，只有哑铃，没有训练凳，帮我调整一下")
            assert "没有训练凳" in data["training_context"].get("equipment", ""), "Equipment negation lost"
        elif case == "swim":
            send("这次先不练力量了，我想去泳池游一会儿，接下来还有30分钟")
            assert data["training_context"].get("activity") == "swim", "Swimming intent lost"
        elif case == "replacement":
            send("刚才的哑铃平板卧推我想改用杠铃平板卧推，其他动作保持")
            assert data["training_context"].get("replacements"), "Specific replacement lost"
        elif case == "injury":
            send("我的肩膀这两天疼，但我还是想练胸，照常给我安排吧")
            assert data["constraints"].get("training_caution"), "Injury not retained"
        result = recommend("training")
        assert result and result["status"] == ("blocked" if case == "injury" else "ready"), result
        if case == "equipment":
            assert all(not {"seat", "bench", "incline-bench", "backrest", "barbell"} & set(item["equipment"]) for item in result["exercises"])
        if case == "swim":
            assert not result["exercises"] and [item["id"] for item in result["aerobic_options"]] == ["swim"]
        if case == "replacement":
            assert result["exercises"][0]["name"] == "杠铃平板卧推"
        assert checked(client.get(f"/api/meals?day={day}")) == []
    assert checked(client.get(f"/api/workouts?day={day}")) == []
    assert checked(client.get(f"/api/training-plans?day={day}")) == []
    assert checked(client.get("/api/profile"))["training_split"] == "five"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-paid-testing", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=CASES, required=True)
    args = parser.parse_args()
    if not args.allow_paid_testing:
        raise SystemExit("Explicit paid-testing flag required; no network requests made")
    settings = Settings.from_env()
    if not settings.qwen_api_key:
        raise SystemExit("No private API key configured; no requests made")
    budget, original, phase, raw_outputs = Budget(), QwenTextModel.request, "", []

    def limited(self, payload, **kwargs):
        identifier = budget.reserve(phase)
        try:
            result = original(self, payload, **kwargs)
        except Exception as error:
            budget.finish(identifier, getattr(error, "code", type(error).__name__))
            raise
        budget.finish(identifier, "response_received")
        raw_outputs.append(result)
        return result

    results = []
    QwenTextModel.request = limited
    try:
        for case in args.cases:
            phase, raw_outputs, trace = case, [], []
            isolated = replace(settings, database_path=ROOT / f"artifacts/r119-live-{uuid4()}.sqlite3",
                knowledge_index_path=ROOT / "artifacts/r119-live-knowledge.sqlite3", model_provider="qwen",
                meal_plan_enabled=True, coach_enabled=True, training_plan_enabled=True,
                ai_user_daily_limit=30, ai_global_daily_limit=30)
            try:
                with TestClient(create_app(isolated)) as client:
                    checked(client.post("/api/auth/register", json={"username": "synthetic_assistant", "password": "Synthetic-test-only-2026"}))
                    run_case(client, case, trace)
                results.append({"case": case, "passed": True, "trace": trace})
            except Exception as error:
                results.append({"case": case, "passed": False, "error": str(error)[:1800], "trace": trace, "synthetic_outputs": raw_outputs})
            report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {"runs": []}
            report["runs"].append({"timestamp": datetime.now(timezone.utc).isoformat(), **results[-1]})
            report.update(total_reserved_calls=budget.count(), ceiling=30, synthetic_only=True, model=settings.qwen_model)
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case, "passed": results[-1]["passed"], "calls": budget.count(),
                              "error": results[-1].get("error", "")}, ensure_ascii=False), flush=True)
    finally:
        QwenTextModel.request = original
    return 0 if all(result["passed"] for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
