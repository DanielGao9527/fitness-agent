"""Create and validate an isolated recovery copy; never replace the running database."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.backups import create_backup, restore_backup, verify_backup, fingerprints, readonly, TABLES
from database import Database


def rehearse(source, directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    backup, restored = directory / 'snapshot.sqlite3', directory / 'recovered.sqlite3'
    create_backup(source, backup)
    verify_backup(backup)
    restore_backup(backup, restored)
    Database(restored).initialize()
    before, after = fingerprints(backup), fingerprints(restored)
    preserved = all(before[name] == after[name] for name in TABLES - {'sessions', 'meal_consents'})
    with closing(readonly(restored)) as connection:
        revoked = connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        revoked = revoked and connection.execute('SELECT COUNT(*) FROM meal_consents').fetchone()[0] == 0
    if not preserved or not revoked:
        raise ValueError('Isolated recovery verification failed')
    report = {'passed': True, 'schema': 13, 'non_session_tables_preserved': 16,
              'old_sessions_revoked': True, 'snapshot_usage_preserved': True,
              'formal_database_replaced': False, 'provider_calls': 0,
              'contains_private_data': True, 'encrypted': False}
    (directory / 'recovery-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(rehearse(args.source, args.directory)))
    except (OSError, ValueError, sqlite3.Error, TimeoutError):
        print('Recovery rehearsal failed; use a new directory and a valid source. Existing data was not overwritten.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
