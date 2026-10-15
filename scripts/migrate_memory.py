"""Explicit offline v14-v20 to v21 upgrade; never stops or deploys a service."""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT
from database import Database
from services.backups import create_backup, fingerprints, inspect_connection, readonly, verify_backup, table_columns


def migrate(database, backup):
    database = Path(database).resolve(strict=True)
    backup = Path(backup).absolute()
    if database.is_relative_to((ROOT / "static").resolve()):
        raise ValueError("Private database cannot be public")
    with closing(readonly(database)) as connection:
        if inspect_connection(connection)["schema"] not in (14, 15, 16, 17, 18, 19, 20):
            raise ValueError("Only schema v14 through v20 can use this one-time upgrade")
    original_columns = table_columns(database)
    before = fingerprints(database)
    create_backup(database, backup)
    verify_backup(backup)
    if fingerprints(backup) != before:
        raise ValueError("Database changed during backup; stop the service before migration")
    Database(database).initialize()
    after = fingerprints(database, columns=original_columns)
    if any(before[name] != after[name] for name in before):
        raise ValueError("Old rows changed; keep service stopped and inspect the verified backup")
    with closing(readonly(database)) as connection:
        result = inspect_connection(connection)
    return {**result, "old_tables_preserved": True, "provider_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--confirm-service-stopped", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not args.database.is_absolute() or not args.backup.is_absolute():
        parser.error("Use absolute database and new backup paths")
    try:
        migrate(args.database, args.backup)
    except (OSError, ValueError, sqlite3.Error, RuntimeError, TimeoutError):
        print("Upgrade failed. Keep the service stopped and inspect the private backup; no automatic rollback.", file=sys.stderr)
        return 1
    print("Schema v21 verified; previous business columns and rows preserved. Service activation remains manual.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
