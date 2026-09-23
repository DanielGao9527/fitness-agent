"""Create an account from a private terminal without enabling web registration."""
import argparse
import getpass
import sqlite3
import sys
import warnings
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT
from pydantic import ValidationError
from schemas import Credentials
from security import hash_password
from services.backups import inspect_connection


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual message may echo an accidentally supplied password.
        self.print_usage(sys.stderr)
        self.exit(2, "参数无效：仅支持 --database 指定已有数据库的绝对路径；密码必须在终端交互输入。\n")


def create_account(path, credentials):
    password_hash = hash_password(credentials.password)
    # mode=rw refuses missing files, including deletion after the path check.
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            inspect_connection(connection)
            # Same normalized username and unique constraint as /auth/register.
            connection.execute(
                "INSERT INTO users(username, password_hash) VALUES (?, ?)",
                (credentials.username.lower(), password_hash),
            )


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--database", type=Path, required=True,
                        help="已有应用数据库的绝对路径；不读取环境变量中的数据库或密码")
    args = parser.parse_args(argv)
    try:
        if not sys.stdin.isatty():
            print("需要交互式终端，不能通过管道或重定向输入账号密码。", file=sys.stderr)
            return 2
        if not args.database.is_absolute():
            raise ValueError
        path = args.database.resolve(strict=True)
        if not path.is_file() or path.is_relative_to((ROOT / "static").resolve()):
            raise ValueError
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
            inspect_connection(connection)
        username = input("用户名（3–32 位字母、数字或下划线）：")
        with warnings.catch_warnings():
            # Do not allow getpass to fall back to echoing a password.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("密码（8–128 位，输入不显示）：")
            confirmation = getpass.getpass("再次输入密码：")
        if password != confirmation:
            print("两次密码不一致，未创建账号。", file=sys.stderr)
            return 2
        credentials = Credentials(username=username, password=password)
        create_account(path, credentials)
    except ValidationError:
        print("输入无效：用户名需为 3–32 位字母、数字或下划线，密码需为 8–128 位。未创建账号。", file=sys.stderr)
        return 2
    except sqlite3.IntegrityError:
        print("用户名已被使用，未修改现有账号。", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("操作已取消，未创建账号。", file=sys.stderr)
        return 2
    except Exception:
        # Paths, validation values and database errors may contain private data.
        print("未能创建账号。请检查终端是否支持隐藏输入，以及数据库路径、权限和完整性；未输出私密信息。", file=sys.stderr)
        return 2
    print("账号已创建，可以通过登录页登录；网页注册设置保持不变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
