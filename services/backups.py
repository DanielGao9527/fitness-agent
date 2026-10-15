"""Local-only SQLite snapshots. Restore never overwrites an existing file."""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from config import ROOT

V14_TABLES = frozenset(("users", "sessions", "profiles", "meals", "workouts", "meal_drafts",
    "ai_usage", "nutrition_previews", "workout_previews", "meal_plans", "training_plans",
    "coach_conversations", "coach_turns", "meal_consents", "coach_reviews", "intake_targets",
    "meal_intake_reviews", "body_measurements", "guest_accounts", "guest_usage"))
V15_TABLES = V14_TABLES | {"coach_memory_entries", "coach_memory_digests"}
V16_TABLES = V15_TABLES | {"profile_fact_events"}
V17_TABLES = V16_TABLES | {"coach_day_equipment"}
V18_TABLES = V17_TABLES | {"coach_memory_revisions"}
V19_TABLES = V18_TABLES | {"knowledge_feedback", "knowledge_candidates", "knowledge_candidate_events"}
V20_TABLES = V19_TABLES | {"knowledge_reviewers", "knowledge_submissions", "knowledge_review_gates", "knowledge_review_events"}
V21_TABLES = V20_TABLES | {"knowledge_releases", "knowledge_release_members", "knowledge_release_events"}
V22_TABLES = V21_TABLES | {"knowledge_publications", "knowledge_live", "knowledge_live_events"}
TABLES = V22_TABLES | {"training_record_revisions"}
LEGACY_TABLES = V14_TABLES - {"guest_accounts", "guest_usage"}
SCHEMA_TABLES = {13: LEGACY_TABLES, 14: V14_TABLES, 15: V15_TABLES, 16: V16_TABLES, 17: V17_TABLES, 18: V18_TABLES, 19: V19_TABLES, 20: V20_TABLES, 21: V21_TABLES, 22: V22_TABLES, 23: TABLES}


def readonly(path):
    path = Path(path).resolve(strict=True)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)


def inspect_connection(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables != SCHEMA_TABLES.get(version):
        raise ValueError("Only a complete schema-v13 through v23 personal database is supported")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Database foreign key check failed")
    return {"schema": version, "tables": len(tables), "integrity": "ok", "foreign_keys": "ok"}


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest_path(path):
    return Path(str(path) + ".manifest.json")


def table_columns(path):
    with closing(readonly(path)) as connection:
        connection.execute("BEGIN")
        metadata = inspect_connection(connection)
        return {name: tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{name}")'))
                for name in sorted(SCHEMA_TABLES[metadata["schema"]])}


def fingerprints(path, *, columns=None):
    with closing(readonly(path)) as connection:
        connection.execute("BEGIN")
        available = SCHEMA_TABLES[inspect_connection(connection)["schema"]]
        if columns is not None and (not columns or not set(columns) <= available):
            raise ValueError("Unknown fingerprint tables")
        result = {}
        for name in sorted(columns if columns is not None else available):
            selection = "*"
            if columns is not None:
                actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
                requested = columns[name]
                if not requested or len(requested) != len(set(requested)) or not set(requested) <= actual:
                    raise ValueError("Unknown fingerprint columns")
                selection = ",".join('"' + field.replace('"', '""') + '"' for field in requested)
            rows = connection.execute(f'SELECT {selection} FROM "{name}" ORDER BY rowid').fetchall()
            result[name] = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
        return result


def verify_backup(path):
    path = Path(path).resolve(strict=True)
    metadata = json.loads(manifest_path(path).read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("format") != 1 or metadata.get("sha256") != checksum(path):
        raise ValueError("Backup checksum does not match its manifest")
    try:
        created = datetime.fromisoformat(metadata["created_at"])
        if created.tzinfo is None:
            raise ValueError("Missing timezone")
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid backup timestamp") from None
    with closing(readonly(path)) as connection:
        checked = inspect_connection(connection)
    return {**checked, "sha256": metadata["sha256"], "created_at": metadata["created_at"]}


def _copy_new(source, destination, *, restoring=False):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.resolve().is_relative_to((ROOT / "static").resolve()):
        raise ValueError("Private backups must not be placed in public static files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also rejects symlinks and concurrent attempts at the same name.
    if source == destination.resolve() or any(Path(str(destination) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Destination must be a new, unused database path")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        deadline = time.monotonic() + 30
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError("Backup timed out; no partial backup is usable")
        with closing(readonly(source)) as origin, closing(sqlite3.connect(destination)) as target:
            origin.backup(target, pages=256, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
            checked = inspect_connection(target)
            if restoring:
                # Old bearer sessions must not regain access after moving/restoring a database.
                target.execute("PRAGMA foreign_keys=ON")
                with target:
                    target.execute("DELETE FROM sessions")
                inspect_connection(target)
        return checked
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def create_backup(source, destination):
    destination = Path(destination).absolute()
    manifest = manifest_path(destination)
    if manifest.exists():
        raise FileExistsError("Backup manifest already exists")
    checked = _copy_new(source, destination)
    metadata = {"format": 1, **checked, "created_at": datetime.now(timezone.utc).isoformat(),
                "sha256": checksum(destination), "encrypted": False}
    with os.fdopen(os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2)
    return metadata


def restore_backup(source, destination):
    verified = verify_backup(source)
    before = fingerprints(source)
    checked = _copy_new(source, destination, restoring=True)
    after = fingerprints(destination)
    if checksum(source) != verified["sha256"] or any(before[name] != after[name] for name in before.keys() - {"sessions", "meal_consents"}):
        Path(destination).unlink()
        raise ValueError("Restore validation failed; no restored database retained")
    return {**checked, "source_sha256": verified["sha256"], "sessions_revoked": True,
            "usage_preserved": True}
