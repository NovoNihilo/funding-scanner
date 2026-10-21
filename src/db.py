"""SQLite access. DB_PATH env var points at the Railway volume in production."""
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
"""


def connect(path=None):
    path = path or DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
