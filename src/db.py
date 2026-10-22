"""SQLite access. DB_PATH env var points at the server data directory in production."""
import sqlite3
from pathlib import Path

from src.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset TEXT NOT NULL,
    multiplier REAL NOT NULL DEFAULT 1,
    market_id TEXT,
    interval_s INTEGER,
    unit_price REAL,
    volume_24h_usd REAL,
    asset_class TEXT,
    flags TEXT NOT NULL DEFAULT '',
    matched INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    max_leverage REAL,
    PRIMARY KEY (venue, symbol)
);
CREATE INDEX IF NOT EXISTS ix_markets_asset ON markets(asset);

-- rate: signed fraction of notional for one settlement interval; positive = longs pay shorts
CREATE TABLE IF NOT EXISTS settlements (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    interval_s INTEGER NOT NULL,
    rate REAL NOT NULL,
    rate_err REAL,
    quality TEXT NOT NULL,
    source TEXT NOT NULL,
    raw TEXT,
    inserted_at INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, ts)
);

CREATE TABLE IF NOT EXISTS var_snapshots (
    symbol TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    next_ts INTEGER,
    interval_s INTEGER,
    interval_rate REAL,
    annualized_rate REAL,
    mark_price REAL,
    bid_1k REAL,
    ask_1k REAL,
    bid_100k REAL,
    ask_100k REAL,
    quotes_updated_at TEXT,
    PRIMARY KEY (symbol, captured_at)
);

CREATE TABLE IF NOT EXISTS sync_state (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last_ts INTEGER,
    last_attempt INTEGER,
    last_error TEXT,
    PRIMARY KEY (venue, symbol)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    ok INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    pair_id TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    long_venue TEXT NOT NULL,
    long_symbol TEXT NOT NULL,
    short_venue TEXT NOT NULL,
    short_symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    cohort TEXT,
    run_start INTEGER,
    qualified_at INTEGER,
    age_h REAL,
    neg_streak INTEGER,
    apy_1x REAL,
    apy_max REAL,
    max_lev REAL,
    metrics TEXT,
    flags TEXT,
    reasons TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    event TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    detail TEXT,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_pair ON signal_events(pair_id, ts);
"""

MIGRATIONS = [("markets", "max_leverage", "REAL")]


def connect(path=None):
    path = path or DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn
