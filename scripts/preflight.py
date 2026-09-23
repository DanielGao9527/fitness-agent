"""Read-only local configuration checks; this command never deploys or calls a model."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import ROOT, Settings
from services.backups import inspect_connection, readonly, verify_backup
from services.business_time import BUSINESS_TIMEZONE
from services.usage import shared_calls


def check(settings, *, for_sharing=False, backup=None):
    checks = []
    try:
        selected = replace(settings, access_mode="shared") if for_sharing else settings
        selected.validate_access()
        checks.append({"name": "access_configuration", "passed": True})
    except ValueError:
        checks.append({"name": "access_configuration", "passed": False,
            "action": "Set explicit allowed hosts, HTTPS public origin and secure cookies for shared mode."})
    try:
        with closing(readonly(settings.database_path)) as connection:
            connection.execute("BEGIN")
            metadata = inspect_connection(connection)
            today = datetime.now(timezone.utc).date().isoformat()
            calls = (shared_calls(connection, today) if metadata['schema'] == 14 else
                     connection.execute("SELECT COALESCE(SUM(calls),0) FROM ai_usage WHERE day=?", (today,)).fetchone()[0])
        checks.append({"name": "personal_database", "passed": True})
    except (OSError, ValueError, sqlite3.Error):
        calls = None
        checks.append({"name": "personal_database", "passed": False, "action": "Check the local database; no data was changed."})
    static = (ROOT / "static").resolve()
    private = settings.database_path.resolve()
    checks.append({"name": "private_data_outside_static", "passed": not private.is_relative_to(static)})
    if backup:
        try:
            metadata = verify_backup(backup)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(metadata["created_at"])
            valid = timedelta(0) <= age <= timedelta(days=7)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            valid = False
        checks.append({"name": "recent_verified_backup", "passed": valid})
    elif for_sharing:
        checks.append({"name": "recent_verified_backup", "passed": False, "action": "Provide --backup after a restore rehearsal."})
    checks.append({"name": "bounded_provider_attempts", "passed": 0 <= settings.ai_user_daily_limit <= 1000
                   and 0 <= settings.ai_global_daily_limit <= 1000})
    return {"checks_passed": all(item["passed"] for item in checks), "checks": checks,
        "business_timezone": BUSINESS_TIMEZONE, "quota_timezone": "UTC", "provider_calls_made": 0,
        "model_key_configured": bool(settings.qwen_api_key), "today_shared_attempts": calls,
        "user_daily_limit": settings.ai_user_daily_limit, "shared_daily_limit": settings.ai_global_daily_limit,
        "deployment_performed": False,
        "still_required": ["real phone testing", "TLS and trusted reverse proxy configuration",
                           "private backup storage and recovery procedure", "explicit deployment approval"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--for-sharing", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    try:
        result = check(Settings.from_env(), for_sharing=args.for_sharing, backup=args.backup)
    except ValueError:
        print("Invalid environment configuration; no values or secrets are printed.", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
