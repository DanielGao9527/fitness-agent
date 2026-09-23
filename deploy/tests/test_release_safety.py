"""使用临时文件和服务替身验证发布失败边界；不访问云端或真实数据。"""
import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from database import Database
from deploy import deploy
from deploy.check_release import check_schema, schema_signature
from services.backups import inspect_connection


@pytest.mark.parametrize("value", ["main", "abc123", "A" * 40, "a" * 40 + ";id", "../" + "a" * 40, "a" * 41])
def test_non_commit_inputs_are_rejected(value):
    with pytest.raises(ValueError):
        deploy.valid_sha(value)


@pytest.mark.parametrize("unsafe", ["../outside", "/outside", "folder/../../outside"])
def test_archive_traversal_does_not_create_file(tmp_path, unsafe):
    archive = tmp_path / "source.tar"
    destination = tmp_path / "release"
    destination.mkdir()
    with tarfile.open(archive, "w") as output:
        entry = tarfile.TarInfo(unsafe)
        entry.size = 4
        output.addfile(entry, io.BytesIO(b"data"))
    with pytest.raises(ValueError):
        deploy.extract_archive(archive, destination)
    assert not list(destination.iterdir())
    assert not (tmp_path / "outside").exists()


def test_archive_symlink_is_rejected(tmp_path):
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as output:
        entry = tarfile.TarInfo("config")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/etc/fitness-agent.env"
        output.addfile(entry)
    with pytest.raises(ValueError):
        deploy.extract_archive(archive, tmp_path / "release")


def test_schema_probe_does_not_modify_existing_personal_records(tmp_path):
    path = tmp_path / "fitness.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO users(username,password_hash) VALUES ('synthetic-user','not-a-real-hash')")
    check_schema(Database, inspect_connection, path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT username FROM users").fetchall() == [("synthetic-user",)]
    before = schema_signature(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE INDEX unexpected_index ON users(created_at)")
    with pytest.raises(ValueError):
        check_schema(Database, inspect_connection, path)
    assert schema_signature(path) != before


@pytest.fixture
def activation(tmp_path, monkeypatch):
    database = tmp_path / "fitness.sqlite3"
    Database(database).initialize()
    previous, candidate = tmp_path / "old", tmp_path / "new"
    calls = []
    monkeypatch.setattr(deploy, "DATABASE", database)
    monkeypatch.setattr(deploy, "STATE", tmp_path)
    monkeypatch.setattr(deploy, "CURRENT", tmp_path / "current")
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(deploy, "switch_to", lambda path: calls.append(("switch", path)))
    monkeypatch.setattr(deploy, "app_task", lambda *args: calls.append(("app", *args)))
    return database, previous, candidate, calls


def test_failed_health_restores_previous_code_without_restoring_database(activation, monkeypatch):
    database, previous, candidate, calls = activation
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO users(username,password_hash) VALUES ('keep-me','not-real')")

    def health(path):
        if path == candidate:
            raise RuntimeError("synthetic failure")

    monkeypatch.setattr(deploy, "wait_healthy", health)
    with pytest.raises(RuntimeError):
        deploy.activate(candidate, previous)
    assert [call for call in calls if call[0] == "switch"] == [("switch", candidate), ("switch", previous)]
    assert calls.index(("systemctl", "stop", "fitness-agent")) < next(i for i, call in enumerate(calls) if call[0] == "app")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT username FROM users").fetchall() == [("keep-me",)]


def test_schema_change_after_start_blocks_automatic_code_rollback(activation, monkeypatch):
    database, previous, candidate, calls = activation

    def health(path):
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version=99")
        raise RuntimeError("schema changed")

    monkeypatch.setattr(deploy, "wait_healthy", health)
    with pytest.raises(RuntimeError):
        deploy.activate(candidate, previous)
    assert ("switch", previous) not in calls
    assert calls[-1] == ("systemctl", "stop", "fitness-agent")
    assert (database.parent / "manual-recovery-required").exists()


def test_backup_failure_restarts_previous_service_before_any_new_code(activation, monkeypatch):
    _, previous, candidate, calls = activation

    def failed_backup(*args):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(deploy, "app_task", failed_backup)
    monkeypatch.setattr(deploy, "wait_healthy", lambda path: None)
    with pytest.raises(OSError):
        deploy.activate(candidate, previous)
    assert ("switch", candidate) not in calls
    assert calls[-1] == ("systemctl", "start", "fitness-agent")
