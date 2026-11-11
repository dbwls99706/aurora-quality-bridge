"""Offline, one-time restoration of the verified prior reservation. Never calls APIs."""
import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = 'aurora-verified-reservation-2026-11-09-v1'
MODEL = 'claude-haiku-4-5-20251001'
RESERVED = 210240


def restore(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing a symbolic-link ledger.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=5) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Existing ledger is not valid. Nothing was reset.')
        db.execute('BEGIN IMMEDIATE')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {'ai_reservations', 'aurora_budget_imports'}:
            raise ValueError('Unrecognized ledger. Nothing was reset.')
        db.execute('CREATE TABLE IF NOT EXISTS ai_reservations (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, model TEXT NOT NULL, reserved_microusd INTEGER NOT NULL CHECK(reserved_microusd > 0))')
        count, total = db.execute('SELECT COUNT(*),COALESCE(SUM(reserved_microusd),0) FROM ai_reservations').fetchone()
        imported = 'aurora_budget_imports' in tables and db.execute('SELECT COUNT(*) FROM aurora_budget_imports WHERE marker=?', (MARKER,)).fetchone()[0] == 1
        if imported:
            if count < 1 or total < RESERVED:
                raise ValueError('Imported reservation is missing. Stop; do not reset limits.')
            action = 'already_restored'
        else:
            if count:
                raise ValueError('Nonempty ledger without import receipt. Preserve it and reconcile history; do not overwrite or reset it.')
            db.execute('CREATE TABLE IF NOT EXISTS aurora_budget_imports (marker TEXT PRIMARY KEY)')
            db.execute('INSERT INTO ai_reservations(created_at,model,reserved_microusd) VALUES (?,?,?)', ('2026-11-09T00:00:00+00:00', MODEL, RESERVED))
            db.execute('INSERT INTO aurora_budget_imports VALUES (?)', (MARKER,))
            count, total = 1, RESERVED
            action = 'restored_verified_minimum'
        return {'action': action, 'attempts_reserved': count, 'reserved_usd': total / 1000000,
                'remaining_attempts_at_most': max(0, 3-count), 'network_called': False,
                'note': 'Reservation is not an invoice. Timestamp is a date placeholder, not original call time.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, default=ROOT/'runtime'/'anthropic_budget.sqlite3')
    args = parser.parse_args()
    try:
        print(json.dumps(restore(args.ledger), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError) as error:
        # No paths or raw SQLite contents are disclosed.
        print(json.dumps({'status':'blocked','reason': str(error) if isinstance(error, ValueError) else 'Ledger could not be safely read or written. Preserve existing files.'}))
        raise SystemExit(1)
