"""管理员安装的固定发布器：只接受公开 deploy 分支的完整提交，不接受任意命令。"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .check_release import schema_signature
    from .github_source import GitHubSource, extract_archive, valid_sha
else:
    from check_release import schema_signature
    from github_source import GitHubSource, extract_archive, valid_sha

ROOT = Path("/opt/fitness-agent")
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
STATE = Path("/var/lib/fitness-agent-deploy")
FAILED = STATE / "failed-sha"
DATABASE = Path("/var/lib/fitness-agent/fitness.sqlite3")
TOOLS = Path("/usr/local/lib/fitness-agent-deploy")
UV = "/opt/fitness-tools/uv"


def run(*command, capture=False, timeout=180):
    return subprocess.run(command, check=True, text=True, timeout=timeout,
                          stdout=subprocess.PIPE if capture else None)


def current_release():
    if not CURRENT.exists() and not CURRENT.is_symlink():
        return None
    if not CURRENT.is_symlink():
        raise ValueError("current must be a managed symlink")
    selected = CURRENT.resolve(strict=True)
    if selected.parent != RELEASES or not re.fullmatch(r"[0-9a-f]{40}", selected.name):
        raise ValueError("current points outside a managed release")
    return selected


def switch_to(release):
    # 同一文件系统中原子替换指针；不移动虚拟环境，避免破坏其绝对路径。
    temporary = ROOT / f".current-{os.getpid()}"
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, CURRENT)


def app_task(release, operation, *arguments):
    return run(
        "systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--service-type=exec",
        "--property=User=fitness", "--property=Group=fitness", "--property=UMask=0077",
        f"--property=WorkingDirectory={release}", "--property=EnvironmentFile=/etc/fitness-agent.env",
        "--property=Environment=PYTHONDONTWRITEBYTECODE=1", "--property=NoNewPrivileges=true",
        "--property=ProtectHome=true", "--property=PrivateTmp=true", "--property=ProtectSystem=strict",
        "--property=ReadWritePaths=/var/lib/fitness-agent",
        str(release / ".venv/bin/python"), str(TOOLS / "check_release.py"), operation,
        "--release", str(release), *arguments, timeout=90,
    )


def build(release, sha, source):
    release.mkdir(mode=0o755)
    with tempfile.TemporaryDirectory(prefix="archive-", dir=STATE) as directory:
        archive = Path(directory) / "release.tar.gz"
        source.download_archive(sha, archive)
        extract_archive(archive, release)
    python = run("env", "UV_PYTHON_INSTALL_DIR=/opt/fitness-python", UV,
                 "python", "find", "--managed-python", "3.12", capture=True).stdout.strip()
    if not Path(python).resolve().is_relative_to(Path("/opt/fitness-python")):
        raise ValueError("Managed Python must be installed outside /root")
    run("chown", "-R", "fitness-build:fitness-build", str(release))
    builder = ("runuser", "-u", "fitness-build", "--", "env", "-i", "PATH=/usr/bin:/bin",
               "HOME=/var/cache/fitness-build", "UV_PYTHON_INSTALL_DIR=/opt/fitness-python",
               "PYTHONDONTWRITEBYTECODE=1")
    try:
        run(*builder, UV, "venv", "--seed", "--python", python, str(release / ".venv"), timeout=300)
        run(*builder, str(release / ".venv/bin/python"), "-m", "pip", "install", "--only-binary=:all:",
            "-r", str(release / "requirements.lock.txt"), timeout=900)
        run(*builder, str(release / ".venv/bin/python"), "-m", "pip", "check")
    finally:
        run("chown", "-R", "root:root", str(release))
        run("chmod", "-R", "u=rwX,go=rX", str(release))


def wait_healthy(release):
    for _ in range(15):
        try:
            pid = run("systemctl", "show", "--property=MainPID", "--value", "fitness-agent", capture=True).stdout.strip()
            # 防止将旧进程或另一个占用同端口的程序误当成本次部署成功。
            if pid.isdigit() and pid != "0" and Path(f"/proc/{pid}/cwd").resolve() == release:
                app_task(release, "health")
                return
        except (subprocess.SubprocessError, OSError):
            pass
        time.sleep(2)
    raise RuntimeError("The selected application did not become healthy")


def activate(release, previous):
    before = schema_signature(DATABASE) if DATABASE.exists() else None
    stopped = False
    try:
        # 所有安装和结构检查先完成，短维护窗口内才停旧服务。
        stopped = True
        run("systemctl", "stop", "fitness-agent")
        if DATABASE.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = DATABASE.parent / "backups" / f"before-{release.name[:12]}-{stamp}.sqlite3"
            app_task(previous or release, "backup", "--destination", str(backup))
        switch_to(release)
        run("systemd-analyze", "verify", "/etc/systemd/system/fitness-agent.service")
        run("systemctl", "start", "fitness-agent")
        wait_healthy(release)
    except Exception:
        if stopped:
            run("systemctl", "stop", "fitness-agent")
            try:
                unchanged = before is not None and DATABASE.exists() and schema_signature(DATABASE) == before
            except Exception:
                unchanged = False
            if previous and unchanged:
                switch_to(previous)
                run("systemctl", "start", "fitness-agent")
                wait_healthy(previous)
                print("Deployment failed; previous code restored. Database was not restored.", flush=True)
            else:
                if not previous and CURRENT.is_symlink() and CURRENT.resolve() == release:
                    CURRENT.unlink()
                if previous:
                    (STATE / "manual-recovery-required").write_text("Database structure changed; do not restart old code.\n")
                print("Service stopped: no previous release or database structure changed. Manual recovery required.", flush=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", nargs="?")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--retry", action="store_true", help="保留失败目录后明确重试当前发布分支")
    args = parser.parse_args()
    if (args.poll and (args.sha or args.retry)) or (not args.poll and not args.sha):
        parser.error("Use --poll, or a full SHA with optional --retry")
    if args.sha:
        valid_sha(args.sha)
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run the installed deployment tool as a Linux administrator")
    import fcntl

    with (STATE / "deployment.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if (STATE / "manual-recovery-required").exists():
            raise ValueError("Manual database recovery review is required before further deployments")
        previous = current_release()
        source = GitHubSource(STATE)
        selected = source.tip(previous.name if previous else None)
        if args.sha and args.sha != selected:
            raise ValueError("Requested SHA is not the current tested release branch")
        if previous and previous.name == selected:
            return 0
        if FAILED.exists() and FAILED.read_text().strip() == selected and not args.retry:
            if not args.poll:
                print("This release failed earlier. Inspect the server and use --retry explicitly.")
            return 0
        release = RELEASES / selected
        try:
            if release.exists():
                if not args.retry:
                    raise ValueError("A previous attempt directory exists; inspect it before --retry")
                saved = RELEASES / f"{selected}.failed-{time.time_ns()}"
                release.rename(saved)
            build(release, selected, source)
            app_task(release, "preflight")
            if source.tip(previous.name if previous else None) != selected:
                raise ValueError("A newer tested release is available; defer to the next poll")
            activate(release, previous)
            (STATE / "last-successful-sha").write_text(selected + "\n")
            if FAILED.exists():
                FAILED.unlink()
            print(f"Deployment healthy: {selected}. Public HTTPS and phone checks remain separate.", flush=True)
            return 0
        except Exception:
            FAILED.write_text(selected + "\n")
            print(f"Deployment stopped for {selected}; inspect the local service log before retrying.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Deployment prerequisites/network check failed; no private configuration is printed.", file=sys.stderr)
        raise SystemExit(1)
