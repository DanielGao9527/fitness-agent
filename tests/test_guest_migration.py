from pathlib import Path
import sqlite3

import pytest

from database import Database
from deploy.check_release import check_schema
from scripts.migrate_guests import main, migrate
from services.backups import fingerprints, inspect_connection, verify_backup


def old_database(tmp_path):
    path = tmp_path/'old.sqlite3'
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.executescript('DROP TABLE guest_accounts; DROP TABLE guest_usage; PRAGMA user_version=13;')
        connection.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'retained','synthetic')")
        connection.execute("INSERT INTO ai_usage VALUES ('2026-09-23',1,10)")
    return path


def test_deployment_remains_blocked_until_explicit_migration(tmp_path):
    path = old_database(tmp_path)
    before = fingerprints(path)
    with pytest.raises(ValueError, match='migration'):
        check_schema(Database, inspect_connection, path)
    assert fingerprints(path) == before
    backup = tmp_path/'approved-backup.sqlite3'
    result = migrate(path, backup)
    assert result['schema'] == 14 and result['tables'] == 20
    assert result['old_tables_preserved'] and result['provider_calls'] == 0
    assert verify_backup(backup)['schema'] == 13
    assert fingerprints(backup) == before
    check_schema(Database, inspect_connection, path)
    with pytest.raises(ValueError, match='v13'):
        migrate(path, tmp_path/'second.sqlite3')
    assert not (tmp_path/'second.sqlite3').exists()


def test_cli_requires_explicit_stopped_confirmation_and_fresh_backup(tmp_path):
    path = old_database(tmp_path)
    backup = tmp_path/'occupied.sqlite3'
    backup.write_text('do not replace', encoding='utf8')
    before = fingerprints(path)
    with pytest.raises(SystemExit):
        main(['--database', str(path), '--backup', str(tmp_path/'copy.sqlite3')])
    assert main(['--database', str(path), '--backup', str(backup), '--confirm-service-stopped']) == 1
    assert backup.read_text(encoding='utf8') == 'do not replace'
    assert fingerprints(path) == before
