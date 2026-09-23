"""以应用账户检查配置、数据库和本机健康；不调用模型，不输出秘密或记录。"""
import argparse
import hashlib
import http.client
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


def schema_signature(path):
    """只读比较结构；不读取用户行，也不把数据库恢复当作代码回退。"""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
    return hashlib.sha256(json.dumps([version, rows], ensure_ascii=True).encode()).hexdigest()


def validate_settings(settings):
    settings.validate_access()
    if settings.access_mode != "shared":
        raise ValueError("Deployment requires shared mode")
    if settings.database_path.resolve() != Path("/var/lib/fitness-agent/fitness.sqlite3"):
        raise ValueError("Deployment uses a fixed persistent database location")
    if urlsplit(settings.public_origin).hostname.endswith(".example.com"):
        raise ValueError("Configure the real public domain")
    if not (0 <= settings.ai_user_daily_limit <= 1000 and 0 <= settings.ai_global_daily_limit <= 1000):
        raise ValueError("Configure bounded quotas")


def check_schema(database_class, inspect_connection, path):
    # 先在隔离临时库运行新版本初始化。已存在的正式库必须结构完全相同。
    # 需要迁移的版本会停在切换之前，交由单独的迁移/恢复计划处理。
    with tempfile.TemporaryDirectory(prefix="fitness-schema-") as directory:
        probe = Path(directory) / "probe.sqlite3"
        database_class(probe).initialize()
        with closing(sqlite3.connect(probe)) as connection:
            inspect_connection(connection)
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                inspect_connection(connection)
            if schema_signature(path) != schema_signature(probe):
                raise ValueError("A database migration requires a separate reviewed procedure")


def health(settings):
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
    try:
        connection.request("GET", "/api/health", headers={"Host": urlsplit(settings.public_origin).netloc})
        response = connection.getresponse()
        payload = json.loads(response.read(65537))
        if (response.status != 200 or payload.get("status") != "ok"
            or payload.get("registration_enabled") is not settings.registration_enabled
            or payload.get("guest_enabled") is not settings.guest_enabled):
            raise ValueError("Health check failed")
        if "stage" in payload or "text_model" in payload:
            raise ValueError("Unexpected local diagnostics in shared mode")
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "backup", "health"))
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    # 此工具固定安装在 root 管理的目录；导入本次候选版本的实际业务代码。
    sys.path.insert(0, str(args.release.resolve()))
    try:
        from config import Settings
        from database import Database
        from services.backups import create_backup, inspect_connection, verify_backup

        settings = Settings.from_env()
        validate_settings(settings)
        if args.operation == "preflight":
            check_schema(Database, inspect_connection, settings.database_path)
        elif args.operation == "backup":
            if args.destination is None or args.destination.parent != Path("/var/lib/fitness-agent/backups"):
                raise ValueError("Invalid backup destination")
            create_backup(settings.database_path, args.destination)
            verify_backup(args.destination)
        else:
            health(settings)
    except Exception:
        print(f"{args.operation} failed; check configuration/schema/service locally. No private values printed.", file=sys.stderr)
        return 1
    print(f"{args.operation} passed; no model calls made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
