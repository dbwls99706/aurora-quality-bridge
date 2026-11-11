"""Small explicit SQLite/Postgres dialect boundary for Aurora's fixed SQL.

Postgres transactions take one app-wide advisory lock, preserving SQLite's
serialized mutation/audit semantics across rolling deployments and processes.
No session-level settings or prepared statements: supports transaction poolers.
"""
import re


class Record(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in (cursor.description or ())]
    return lambda values: Record(zip(names, values))


def postgres_sql(sql):
    sql = sql.strip().rstrip(';')
    if sql == 'PRAGMA journal_mode=WAL':
        return None
    if sql == 'BEGIN IMMEDIATE':
        return None  # transaction is begun lazily by the adapter
    if sql.startswith('PRAGMA table_info('):
        table = sql[len('PRAGMA table_info('):-1]
        if table not in ('cases', 'decisions'):
            raise ValueError('Unsupported schema inspection')
        return "SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='" + table + "'"
    sql = sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
    if sql.startswith('INSERT OR IGNORE INTO metadata '):
        sql = sql.replace('INSERT OR IGNORE', 'INSERT', 1) + ' ON CONFLICT (key) DO NOTHING'
    if sql.startswith('INSERT OR REPLACE INTO customer_commitments '):
        sql = sql.replace('INSERT OR REPLACE', 'INSERT', 1) + ' ON CONFLICT (case_id) DO UPDATE SET supply_revision=EXCLUDED.supply_revision, quality_revision=EXCLUDED.quality_revision, entries=EXCLUDED.entries, updated_at=EXCLUDED.updated_at'
    # Replace placeholders only outside SQL string literals, including escaped quotes.
    return re.sub(r"'(?:''|[^'])*'|\?", lambda m: '%s' if m.group() == '?' else m.group(), sql)


class PostgresConnection:
    def __init__(self, url, connector=None, lock_key=718401216):
        if connector is None:
            import psycopg
            connector = psycopg.connect
        self.raw = connector(url, row_factory=row_factory, connect_timeout=10, prepare_threshold=None)
        self.started = False
        if type(lock_key) is not int:
            raise ValueError("Invalid transaction lock")
        self.lock_key = lock_key

    def execute(self, sql, parameters=()):
        if not self.started:
            self.raw.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_key,))
            self.started = True
        translated = postgres_sql(sql)
        if translated is None:
            return self.raw.execute('SELECT 1')
        return self.raw.execute(translated, parameters) if parameters else self.raw.execute(translated)

    def executescript(self, sql):
        for statement in sql.split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.raw.commit()
        self.started = False

    def rollback(self):
        self.raw.rollback()
        self.started = False

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.rollback() if kind else self.commit()
        finally:
            self.close()
