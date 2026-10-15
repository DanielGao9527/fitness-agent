"""Isolation and honest reporting for the offline historical compatibility probe."""
import hashlib
import io
import json
from types import SimpleNamespace
import socket
import tarfile

import pytest

from scripts import check_memory_baseline as probe


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as stream:
        for name, kind, raw in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(raw) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../outside"
            stream.addfile(member, io.BytesIO(raw) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


@pytest.mark.parametrize("name", ["../outside.py", "/outside.py", "C:/outside.py",
                                  "folder\\outside.py", "data/runtime/secret.sqlite3"])
def test_archive_rejects_unsafe_paths_before_writing_any_member(tmp_path, name):
    target = tmp_path / "snapshot"
    raw = archive([("app.py", tarfile.REGTYPE, b"safe"), (name, tarfile.REGTYPE, b"unsafe")])
    with pytest.raises(ValueError, match="Unsafe snapshot"):
        probe.extract_snapshot(raw, target)
    assert not target.exists()
    assert not (tmp_path / "outside.py").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_archive_rejects_links_and_special_files(tmp_path, kind):
    with pytest.raises(ValueError, match="Unsafe snapshot"):
        probe.extract_snapshot(archive([("dangerous", kind, b"")]), tmp_path / "snapshot")
    assert list(tmp_path.iterdir()) == []


def test_archive_preserves_bytes_and_does_not_overwrite_existing_directory(tmp_path):
    raw = b"# source\nprint('synthetic')\n"
    target = tmp_path / "snapshot"
    hashes = probe.extract_snapshot(archive([("agent/app.py", tarfile.REGTYPE, raw)]), target)
    assert (target / "agent/app.py").read_bytes() == raw
    assert hashes == {"agent/app.py": hashlib.sha256(raw).hexdigest()}
    with pytest.raises(FileExistsError):
        probe.extract_snapshot(archive([]), target)
    assert (target / "agent/app.py").read_bytes() == raw


@pytest.mark.parametrize("revision", ["HEAD", "--output=secret", "07b6ec2; command", "a" * 41])
def test_revision_rejects_non_sha_arguments_without_running_git(monkeypatch, revision):
    monkeypatch.setattr(probe, "git", lambda *args: pytest.fail("Git must not run"))
    with pytest.raises(ValueError, match="hexadecimal"):
        probe.resolve_commit(revision)


def test_old_revision_does_not_need_new_evaluation_directory(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        if args[0] == "ls-tree":
            return b"app.py\nconfig.py\ndatabase.py\nschemas.py\nscripts\n"
        return b"archive"

    monkeypatch.setattr(probe, "git", fake_git)
    assert probe.snapshot_archive("a" * 40) == b"archive"
    assert "evals" not in calls[1]
    assert calls[1][0] == "archive" and "scripts" in calls[1]


def test_missing_memory_contract_is_not_scored_as_zero_accuracy():
    result = probe.compare_contracts({"contract": {}}, {"contract": dict.fromkeys(probe.REQUIRED, True)})
    assert result["baseline_missing"] == list(probe.REQUIRED)
    assert not result["baseline_current_suite_compatible"]
    assert result["candidate_current_suite_compatible"]
    assert result["model_quality_evaluated"] is False
    assert not any("accuracy" in key or "score" in key for key in result)


def test_network_guard_blocks_external_connection_and_dns_but_allows_local_event_loop(monkeypatch):
    calls = []
    # Monkeypatch originals first so pytest also restores the guard's replacements.
    monkeypatch.setattr(socket.socket, "connect", lambda *args: calls.append("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *args: calls.append("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", lambda *args: calls.append("create"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: calls.append("lookup"))
    progress = {"network_attempts": 0}
    probe.block_external_network(progress)
    with socket.socket() as connection:
        for address in [("8.8.8.8", 443), ("api.example.org", 443)]:
            with pytest.raises(RuntimeError, match="forbidden"):
                connection.connect(address)
        with pytest.raises(RuntimeError, match="forbidden"):
            connection.connect_ex(("8.8.8.8", 443))
        connection.connect(("127.0.0.1", 12345))
    with pytest.raises(RuntimeError, match="forbidden"):
        socket.create_connection(("api.example.org", 443))
    with pytest.raises(RuntimeError, match="forbidden"):
        socket.getaddrinfo("api.example.org", 443)
    socket.create_connection(("::1", 12345))
    socket.getaddrinfo("127.0.0.1", 12345)
    assert calls == ["connect", "create", "lookup"]
    assert progress["network_attempts"] == 5


def fake_repository(monkeypatch, tmp_path, *, dirty=False):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "resolve_commit", lambda value: value)
    monkeypatch.setattr(probe, "git", lambda *args: b"dirty" if args[0] == "status" and dirty else b"b" * 40 if args[0] == "rev-parse" else b"")
    monkeypatch.setattr(probe, "snapshot_archive", lambda commit: archive([("app.py", tarfile.REGTYPE, b"# synthetic\n")]))


def test_output_must_be_new_and_under_artifacts(monkeypatch, tmp_path):
    fake_repository(monkeypatch, tmp_path)
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("No worker should run"))
    with pytest.raises(ValueError, match="artifacts"):
        probe.run("a" * 40, tmp_path / "public-docs")
    assert not (tmp_path / "public-docs").exists()
    existing = tmp_path / "artifacts" / "existing"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        probe.run("a" * 40, existing)
    assert (existing / "keep.txt").read_text() == "keep"


def test_uncommitted_application_changes_stop_comparison(monkeypatch, tmp_path):
    fake_repository(monkeypatch, tmp_path, dirty=True)
    with pytest.raises(ValueError, match="Commit application"):
        probe.run("a" * 40, tmp_path / "artifacts" / "not-created")
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("failure", [None, "worker", "source"])
def test_isolated_workers_strip_keys_and_preserve_failure_receipts(monkeypatch, tmp_path, failure):
    fake_repository(monkeypatch, tmp_path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-never-copy")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-copy")
    monkeypatch.setenv("FITNESS_DB_PATH", "private-never-open.sqlite3")
    monkeypatch.setenv("FITNESS_QWEN_MODEL", "private-override")
    monkeypatch.setenv("PYTHONPATH", "private-import-path")
    labels = []

    def child(arguments, **kwargs):
        label = kwargs["cwd"].name
        labels.append(label)
        assert arguments[1:3] == ["-I", "-B"]
        assert kwargs["env"]["DASHSCOPE_API_KEY"] == ""
        assert "OPENAI_API_KEY" not in kwargs["env"] and "PYTHONPATH" not in kwargs["env"]
        assert not any(key.startswith("FITNESS_") for key in kwargs["env"])
        assert kwargs["timeout"] == 90
        if failure == "worker" and label == "candidate":
            value = {"status": "failed", "error_type": "ProbeAPIError", "phase": "set_profile", "http_status": 409}
            return SimpleNamespace(returncode=1, stdout=json.dumps(value).encode())
        if failure == "source" and label == "candidate":
            (kwargs["cwd"] / "app.py").write_text("changed", encoding="utf-8")
        value = {"status": "completed", "contract": dict.fromkeys(probe.REQUIRED, label == "candidate")}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    monkeypatch.setattr(probe.subprocess, "run", child)
    output = tmp_path / "artifacts" / "receipt"
    if failure:
        with pytest.raises(RuntimeError):
            probe.run("a" * 40, output)
    else:
        probe.run("a" * 40, output)
    assert labels == ["baseline", "candidate"]
    raw = (output / "report.json").read_text(encoding="utf-8")
    assert "private-never-open" not in raw and "synthetic-never-copy" not in raw
    report = json.loads(raw)
    assert report["provider_calls"] == 0 and not report["model_quality_evaluated"]
    assert not report["production_database_used"]
    assert report["versions"]["baseline"]["status"] == "completed"
    assert report["versions"]["baseline"]["source_unchanged"]
    assert list(output.iterdir()) == [output / "report.json"]
    if failure == "worker":
        assert report["status"] == "failed" and "comparison" not in report
        assert report["versions"]["candidate"]["phase"] == "set_profile"
        assert report["versions"]["candidate"]["http_status"] == 409
    elif failure == "source":
        assert report["status"] == "failed" and not report["versions"]["candidate"]["source_unchanged"]
    else:
        assert report["status"] == "completed" and report["versions"]["candidate"]["source_unchanged"]
        assert report["comparison"]["candidate_current_suite_compatible"]
