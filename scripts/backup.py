"""Back up or restore locally, without starting the application or contacting a model."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.backups import create_backup, restore_backup, verify_backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify", "restore"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.operation != "verify" and args.destination is None:
        parser.error("create/restore require a new --destination path")
    try:
        action = {"create": create_backup, "verify": verify_backup, "restore": restore_backup}[args.operation]
        result = action(args.source) if args.operation == "verify" else action(args.source, args.destination)
        print(json.dumps(result))
    except (OSError, ValueError, sqlite3.Error, TimeoutError):
        print("Backup operation failed. Check source integrity and use a new destination; no existing database is overwritten.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
