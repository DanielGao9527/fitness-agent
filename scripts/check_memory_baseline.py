"""Offline API/protocol compatibility probe for two committed application versions.

Uses isolated Git snapshots and disposable accounts. No model output, paid calls,
quality score, old-database migration, checkout or production process changes.
"""
import argparse
from contextlib import redirect_stdout
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ("app.py", "config.py", "database.py", "schemas.py", "security.py", "agent", "api",
               "model", "rag", "services", "data", "prompts", "static")
# Current release services import shared evaluation code. Keep those dependencies
# in the snapshot without treating this developer tool as running application code.
SNAPSHOT_PATHS = (*APPLICATION, "scripts", "evals")
REQUIRED = ("understanding.equipment_memory", "confirmation.equipment_scope", "context.day_equipment")


class ProbeAPIError(RuntimeError):
    def __init__(self, status):
        super().__init__("Synthetic probe API call failed")
        self.http_status = status


def block_external_network(progress):
    """Windows event loops need loopback socket pairs; reject external sockets."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection
    original_lookup = socket.getaddrinfo

    def allowed(address):
        try:
            return isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            return False

    def check(address):
        if not allowed(address):
            progress["network_attempts"] += 1
            raise RuntimeError("External network access is forbidden in the offline probe")

    def connect(connection, address):
        check(address)
        return original_connect(connection, address)

    def connect_ex(connection, address):
        check(address)
        return original_connect_ex(connection, address)

    def create_connection(address, *args, **kwargs):
        check(address)
        return original_create(address, *args, **kwargs)

    def lookup(host, *args, **kwargs):
        if host is not None:
            check((host, 0))
        return original_lookup(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = lookup


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, stderr=subprocess.PIPE)


def resolve_commit(value):
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
        raise ValueError("Revision must be a hexadecimal commit ID")
    result = git("rev-parse", "--verify", value + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", result):
        raise ValueError("Commit did not resolve to a local SHA")
    return result


def snapshot_archive(commit):
    # Earlier revisions may predate evals/. Git archive rejects absent pathspecs.
    paths = git("ls-tree", "--name-only", commit, "--", *SNAPSHOT_PATHS).decode().splitlines()
    if not {"app.py", "config.py", "database.py", "schemas.py"}.issubset(paths):
        raise ValueError("Revision does not contain the supported application layout")
    return git("archive", "--format=tar", commit, "--", *paths)


def extract_snapshot(content, directory):
    """Reject traversal, links and special files before writing anything."""
    directory = directory.resolve()
    if directory.exists():
        raise FileExistsError("Snapshot destination already exists")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > 5000 or sum(item.size for item in members) > 40 * 1024 * 1024:
            raise ValueError("Snapshot exceeds the bounded application size")
        for item in members:
            name = PurePosixPath(item.name)
            if (name.is_absolute() or ".." in name.parts or ":" in item.name or "\\" in item.name
                    or not (item.isfile() or item.isdir()) or item.size < 0
                    or not (directory / item.name).resolve().is_relative_to(directory)
                    or name.parts[:2] == ("data", "runtime")):
                raise ValueError("Unsafe snapshot member")
        directory.mkdir(parents=True)
        hashes = {}
        for item in members:
            target = directory / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as stream:
                    raw = stream.read()
                target.write_bytes(raw)
                hashes[item.name] = hashlib.sha256(raw).hexdigest()
    return hashes


def compare_contracts(baseline, candidate):
    return {"required": list(REQUIRED),
            "baseline_missing": [key for key in REQUIRED if not baseline["contract"].get(key)],
            "candidate_missing": [key for key in REQUIRED if not candidate["contract"].get(key)],
            "baseline_current_suite_compatible": all(baseline["contract"].get(key) for key in REQUIRED),
            "candidate_current_suite_compatible": all(candidate["contract"].get(key) for key in REQUIRED),
            "model_quality_evaluated": False,
            "note": "Missing protocol support is not a zero accuracy score; a version-specific semantic adapter is required."}


def worker(snapshot, progress=None):
    progress = progress if progress is not None else {}
    progress["phase"] = "validate_snapshot"
    snapshot = Path(snapshot).resolve()
    if snapshot.name not in {"baseline", "candidate"} or not snapshot.parent.name.startswith("memory-baseline-"):
        raise ValueError("Worker requires a disposable snapshot")
    runtime = snapshot / ".probe-runtime"
    runtime.mkdir(exist_ok=False)
    os.environ["FITNESS_DB_PATH"] = str(runtime / "module-default.sqlite3")
    os.environ["DASHSCOPE_API_KEY"] = ""
    sys.path.insert(0, str(snapshot))
    # Disabled providers are the first boundary; forbid external sockets as well.
    # Windows event loops may use loopback socket pairs, not provider requests.
    progress["network_attempts"] = 0
    block_external_network(progress)
    progress["phase"] = "import_snapshot"
    # Imported app versions receive only explicit disabled-provider configuration.
    from fastapi.testclient import TestClient
    from app import create_app
    from config import Settings
    import schemas
    from services.business_time import business_today

    arguments = dict(database_path=runtime / "synthetic.sqlite3", knowledge_index_path=runtime / "knowledge.sqlite3",
        model_provider="disabled", qwen_api_key="", coach_enabled=True, training_plan_enabled=True,
        registration_enabled=True, guest_enabled=False, ai_user_daily_limit=0, ai_global_daily_limit=0)
    if "retrieval_mode" in {field.name for field in fields(Settings)}:
        arguments["retrieval_mode"] = "lexical"
    settings = Settings(**arguments)
    base = "/api/coach/conversations"

    def checked(response):
        if response.status_code >= 400:
            raise ProbeAPIError(response.status_code)
        return response.json()

    def advise(client, label, message="今天练什么"):
        progress["phase"] = label + ".create_conversation"
        conversation = checked(client.post(base, json={"client_id": str(uuid4()), "day": business_today().isoformat()}))
        progress["phase"] = label + ".message"
        data = checked(client.post(f"{base}/{conversation['id']}/messages", json={
            "client_id": str(uuid4()), "version": conversation["version"], "message": message}))
        progress["phase"] = label + ".recommend"
        response = client.post(f"{base}/{data['id']}/recommend-training", json={
            "client_id": str(uuid4()), "version": data["version"]})
        value = (response.json().get("turns", [{}])[-1].get("response", {}).get("training_recommendation", {})
                 if response.status_code == 200 else {})
        return data, {"http": response.status_code, "status": value.get("status"),
                      "equipment": value.get("equipment"), "equipment_source": value.get("equipment_source"),
                      "exercise_count": len(value.get("exercises", [])), "pending": bool(data.get("pending"))}

    progress["phase"] = "create_application"
    app = create_app(settings)
    with TestClient(app) as client:
        progress["phase"] = "register_account"
        checked(client.post("/api/auth/register", json={"username": "baseline_probe", "password": "synthetic-probe-only-42"}))
        progress["phase"] = "set_profile"
        checked(client.put("/api/profile", json={"equipment": "在家，只有哑铃", "experience": "experienced",
                                                 "minutes_per_session": 40}))
        first, profile_advice = advise(client, "profile")
        progress["phase"] = "correct_profile"
        checked(client.put("/api/profile", json={"equipment": "在家，徒手"}))
        _, corrected_advice = advise(client, "corrected_profile")
        _, local_message = advise(client, "local_phrase", "在家，只有哑铃")
        cookies = dict(client.cookies)
        progress["phase"] = "account_isolation"
        with TestClient(app) as other:
            checked(other.post("/api/auth/register", json={"username": "other_probe", "password": "synthetic-other-only-42"}))
            isolation = other.get(f"{base}/{first['id']}").status_code == 404
        contract = {"understanding.equipment_memory": "equipment_memory" in schemas.CoachUnderstanding.model_fields,
                    "confirmation.equipment_scope": "equipment_scope" in schemas.CoachReviewConfirm.model_fields,
                    "context.day_equipment": "day_equipment" in first.get("context", {})}
    progress["phase"] = "restart_profile"
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.update(cookies)
        persistent = checked(restarted.get("/api/profile"))["equipment"] == "在家，徒手"
    return {"status": "completed", "contract": contract, "manual_profile_advice": profile_advice,
            "corrected_profile_advice": corrected_advice, "local_phrase_observation": local_message,
            "account_isolation": isolation, "restart_profile_persistent": persistent,
            "provider_calls": 0, "network_attempts": progress["network_attempts"], "model_quality_evaluated": False}


def run(baseline, output):
    baseline = resolve_commit(baseline)
    candidate = resolve_commit(git("rev-parse", "HEAD").decode().strip())
    if git("status", "--porcelain=v1", "--untracked-files=all", "--", *APPLICATION):
        raise ValueError("Commit application changes before comparing committed snapshots")
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("Probe output must be a new ignored artifacts directory")
    output.mkdir(parents=True, exist_ok=False)
    report = {"protocol": "memory-baseline-compatibility-v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "baseline_commit": baseline, "candidate_commit": candidate, "provider_calls": 0,
              "model_quality_evaluated": False, "production_database_used": False, "versions": {}}
    try:
        with TemporaryDirectory(prefix="memory-baseline-", dir=output) as temporary:
            for label, commit in (("baseline", baseline), ("candidate", candidate)):
                snapshot = Path(temporary) / label
                hashes = extract_snapshot(snapshot_archive(commit), snapshot)
                environment = {key: value for key, value in os.environ.items()
                               if not key.startswith("FITNESS_") and key not in {"DASHSCOPE_API_KEY", "OPENAI_API_KEY", "PYTHONPATH"}}
                environment["DASHSCOPE_API_KEY"] = ""
                result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--_worker", str(snapshot)],
                    cwd=snapshot, env=environment, capture_output=True, timeout=90)
                observed = json.loads(result.stdout)
                unchanged = all(hashlib.sha256((snapshot / name).read_bytes()).hexdigest() == sha for name, sha in hashes.items())
                report["versions"][label] = {**observed, "snapshot_file_hashes": hashes, "source_unchanged": unchanged}
                if result.returncode or observed.get("status") != "completed":
                    raise RuntimeError("Isolated worker failed: " + observed.get("error_type", "unknown"))
                if not unchanged:
                    raise RuntimeError("Snapshot source changed during probe")
        report["comparison"] = compare_contracts(report["versions"]["baseline"], report["versions"]["candidate"])
        report["status"] = "completed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="07b6ec2")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        progress = {}
        try:
            with redirect_stdout(io.StringIO()):
                result = worker(args._worker, progress)
            print(json.dumps(result, ensure_ascii=True))
            return 0
        except Exception as error:
            print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                              "phase": progress.get("phase"), "http_status": getattr(error, "http_status", None),
                              "network_attempts": progress.get("network_attempts", 0)}))
            return 1
    if args.output is None:
        parser.error("--output must name a new directory under artifacts")
    try:
        report = run(args.baseline, args.output)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print("Compatibility probe failed; no model calls or production database changes. Inspect the local receipt if created.")
        return 1
    print(json.dumps({"status": report["status"], "comparison": report["comparison"], "provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
