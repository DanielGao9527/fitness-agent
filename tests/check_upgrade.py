"""Local upgrade check: private rows stay local; print comparisons, never contents."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import ROOT
from database import Database

TABLES = ("users", "profiles", "meals", "workouts", "meal_drafts", "sessions", "ai_usage", "nutrition_previews", "workout_previews", "meal_plans", "training_plans", "coach_conversations", "coach_turns", "meal_consents")


def inspect(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        fingerprints = {}
        for table in TABLES:
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            fingerprints[table] = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
        return {"version": connection.execute("PRAGMA user_version").fetchone()[0],
                "fingerprints": fingerprints,
                "foreign_keys_ok": not connection.execute("PRAGMA foreign_key_check").fetchall()}


def main():
    formal = ROOT / "data/runtime/fitness.sqlite3"
    if sys.argv[1:] == ["--copy"]:
        if inspect(formal)["version"] != 9:
            raise SystemExit("--copy requires schema v9; no copy or migration performed.")
        target = ROOT / f"artifacts/r183-review-upgrade-{uuid4()}.sqlite3"
        with sqlite3.connect(formal.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        before = inspect(target)
        Database(target).initialize()
        after = inspect(target)
        backup = inspect(target.with_name(target.name + ".pre-v10.bak"))
    elif sys.argv[1:] == ["--verify"]:
        before = backup = inspect(formal.with_name(formal.name + ".pre-v10.bak"))
        after = inspect(formal)
    else:
        raise SystemExit("Use --copy before restarting, --verify after restarting.")
    assert before["fingerprints"] == after["fingerprints"] == backup["fingerprints"]
    assert before["version"] == 9 and after["version"] == 10 and after["foreign_keys_ok"]
    print(json.dumps({"check": sys.argv[1], "before_version": 9, "after_version": 10,
                      "all_fourteen_tables_unchanged": True, "backup_verified": True, "foreign_keys_ok": True}))


if __name__ == "__main__":
    main()
