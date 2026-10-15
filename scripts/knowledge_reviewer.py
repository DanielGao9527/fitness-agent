"""Grant/revoke local public-excerpt pre-review; never grant roles over HTTP."""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import Database
from services.backups import inspect_connection, readonly
from services.knowledge_review import set_reviewer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("grant", "revoke"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    if not args.database.is_absolute():
        parser.error("Use an existing absolute private database path")
    try:
        with closing(readonly(args.database)) as connection:
            if inspect_connection(connection)["schema"] not in (20, 21, 22):
                raise ValueError("Upgrade to schema v20, v21 or v22 first")
        result = set_reviewer(Database(args.database), args.username, args.operation == "grant")
    except (OSError, ValueError, sqlite3.Error):
        print("Role update failed; use an existing non-guest account and a verified v20/v21/v22 database.", file=sys.stderr)
        return 1
    print(f"Reviewer enabled={result['enabled']}, revision={result['revision']}; no knowledge published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
