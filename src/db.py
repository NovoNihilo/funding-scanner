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
