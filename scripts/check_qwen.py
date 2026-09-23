"""Opt-in live smoke checks using synthetic text only, without a database."""
import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT, Settings
from model.factory import ModelError, create_meal_text_model
from services.meal_drafts import MealDraftService
from schemas import portion_issue

CASES = {
    "consumed": {
        "text": "早餐已经吃了100克鸡蛋和200克熟米饭。没有吃面包，晚上准备吃鸡肉。",
        "expected": [("鸡蛋", 100), ("米饭", 200)],
    },
    "portions": {
        "text": "早餐吃了两个鸡蛋，喝了250毫升牛奶，还吃了半碗粥；没有吃油条。",
        "expected": [("鸡蛋", None), ("牛奶", None), ("粥", None)],
    },
    "planned": {
        "text": "今天到现在还没吃任何东西，晚上打算吃鸡肉和米饭。",
        "expected": [],
        "question_hint": "进食后",
    },
}


def check_result(draft, expected, question_hint=""):
    if draft.input_type != "text" or draft.status != "awaiting_confirmation":
        return False
    remaining = list(draft.items)
    if len(remaining) != len(expected):
        return False
    for keyword, grams in expected:
        match = next((item for item in remaining if keyword in item.name and item.grams == grams), None)
        if match is None:
            return False
        remaining.remove(match)
    if any(portion_issue(item.name, item.grams, item.amount_description) for item in draft.items) and not draft.questions:
        return False
    if question_hint and not any(question_hint in question for question in draft.questions):
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Opt in to paid Qwen requests with synthetic samples.")
    parser.add_argument("--case", action="append", choices=list(CASES))
    args = parser.parse_args()
    if not args.live:
        print("No requests made. Use --live to opt in; at most three synthetic samples, no retries.")
        return 0
    settings = replace(Settings.from_env(), model_provider="qwen")
    results = []
    try:
        service = MealDraftService(create_meal_text_model(settings))
        for name in dict.fromkeys(args.case or CASES):
            case = CASES[name]
            draft = service.parse_text(case["text"])
            passed = check_result(draft, case["expected"], case.get("question_hint", ""))
            results.append({"case": name, "passed": passed, "output": draft.model_dump(mode="json")})
            print(f"{name}: {'PASS' if passed else 'FAIL'}")
    except ModelError as error:
        results.append({"passed": False, "error_code": error.code})
        print("Check stopped: " + error.code)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.qwen_model,
        "live": True,
        "results": results,
        "note": "Synthetic samples only. No personal records. Not an accuracy guarantee.",
    }
    directory = ROOT / "artifacts"
    directory.mkdir(exist_ok=True)
    (directory / "qwen-live-last.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if results and all(item["passed"] for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
