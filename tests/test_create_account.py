"""Private-terminal account provisioning; all accounts and databases are synthetic."""
import sqlite3
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from database import Database
from scripts import create_account
from security import verify_password

PASSWORD = "Synthetic-cli-password-42"


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    Database(path).initialize()
    return path


def interact(monkeypatch, username="Alice", passwords=(PASSWORD, PASSWORD)):
    monkeypatch.setattr(create_account.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: username)
    supplied = iter(passwords)
    monkeypatch.setattr(create_account.getpass, "getpass", lambda prompt: next(supplied))


def invoke(path):
    return create_account.main(["--database", str(path)])


def users(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT username, password_hash FROM users ORDER BY id").fetchall()


def test_created_accounts_use_existing_login_rules_and_are_isolated(database_path, monkeypatch, capsys):
    for username in ("Alice", "Bob"):
        interact(monkeypatch, username)
        assert invoke(database_path) == 0
    stored = users(database_path)
    assert [row[0] for row in stored] == ["alice", "bob"]
    assert all(verify_password(PASSWORD, row[1]) for row in stored)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert "Alice" not in output.out + output.err
    assert all(row[1] not in output.out + output.err for row in stored)

    # A fresh app instance proves persisted credentials work with registration closed.
    settings = Settings(database_path=database_path, registration_enabled=False)
    with TestClient(create_app(settings)) as alice:
        assert alice.post("/api/auth/register", json={"username": "new_user", "password": PASSWORD}).status_code == 403
        assert alice.post("/api/auth/login", json={"username": "ALICE", "password": PASSWORD}).status_code == 200
        assert alice.put("/api/profile", json={"display_name": "Synthetic private profile"}).status_code == 200
        with TestClient(create_app(settings)) as bob:
            assert bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD}).status_code == 200
            assert bob.get("/api/profile").json()["display_name"] == ""
        assert alice.get("/api/profile").json()["display_name"] == "Synthetic private profile"


def test_duplicate_username_does_not_replace_password(database_path, monkeypatch, capsys):
    interact(monkeypatch)
    assert invoke(database_path) == 0
    original = users(database_path)
    replacement = "Synthetic-replacement-password"
    interact(monkeypatch, "ALICE", (replacement, replacement))
    assert invoke(database_path) == 2
    assert users(database_path) == original
    output = capsys.readouterr()
    assert "用户名已被使用" in output.err
    assert replacement not in output.out + output.err


@pytest.mark.parametrize("username,password", [
    ("ab", PASSWORD), ("user with spaces", PASSWORD), ("valid_user", "short"),
    ("valid_user", "x" * 129),
])
def test_validation_does_not_save_or_echo_inputs(database_path, monkeypatch, capsys, username, password):
    interact(monkeypatch, username, (password, password))
    assert invoke(database_path) == 2
    assert users(database_path) == []
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert username not in output.out + output.err


def test_password_confirmation_and_noninteractive_input_are_rejected(database_path, monkeypatch, capsys):
    interact(monkeypatch, passwords=(PASSWORD, "Synthetic-different-password"))
    assert invoke(database_path) == 2
    monkeypatch.setattr(create_account.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("must not read redirected input"))
    assert invoke(database_path) == 2
    assert users(database_path) == []
    assert PASSWORD not in capsys.readouterr().err


def test_getpass_fallback_is_rejected_before_echo(database_path, monkeypatch, capsys):
    interact(monkeypatch)
    def fallback(prompt):
        warnings.warn("cannot disable echo", create_account.getpass.GetPassWarning)
        pytest.fail("getpass must not fall back to reading an echoed password")
    monkeypatch.setattr(create_account.getpass, "getpass", fallback)
    assert invoke(database_path) == 2
    assert users(database_path) == []
    assert "cannot disable echo" not in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [
    ["--password", PASSWORD], ["--database"], [],
])
def test_cli_does_not_accept_or_echo_password_arguments(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        create_account.main(arguments)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err


def test_explicit_path_is_required_and_environment_is_not_used(database_path, tmp_path, monkeypatch):
    interact(monkeypatch)
    monkeypatch.setenv("FITNESS_DB_PATH", str(database_path))
    monkeypatch.setenv("FITNESS_PASSWORD", PASSWORD)
    missing = tmp_path / "missing.sqlite3"
    assert invoke(missing) == 2
    assert not missing.exists()
    assert invoke(Path("relative.sqlite3")) == 2
    assert users(database_path) == []


def test_wrong_database_and_private_error_text_are_not_exposed(tmp_path, monkeypatch, capsys):
    interact(monkeypatch)
    unrelated = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    assert invoke(unrelated) == 2
    output = capsys.readouterr()
    assert str(unrelated) not in output.out + output.err
    with sqlite3.connect(unrelated) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]


def test_database_failure_does_not_echo_exception_or_credentials(database_path, monkeypatch, capsys):
    interact(monkeypatch)
    def fail(path, credentials):
        raise sqlite3.OperationalError("private error " + credentials.password)
    monkeypatch.setattr(create_account, "create_account", fail)
    assert invoke(database_path) == 2
    assert users(database_path) == []
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert "private error" not in output.out + output.err
