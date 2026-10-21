"""Offline tests: Lighter precision/sign, Variational derivation, idempotency, gap detection."""
from src.db import connect
from src.funding import completeness, derive_variational, insert_settlements, lighter_rate


def test_lighter_rate_uses_value_for_precision():
    r, err, src = lighter_rate({"rate": "0.0007", "value": "0.59146850", "direction": "short"}, 84500.0)
    assert src == "lt_value" and r < 0 and abs(-r - 0.59146850 / 84500) < 1e-12


def test_lighter_rate_cheap_coin_falls_back():
    r, err, src = lighter_rate({"rate": "0.0010", "value": "0.00000005", "direction": "long"}, 0.00467)
    assert src == "lt_rate" and abs(r - 1e-5) < 1e-12


def test_lighter_rate_inconsistent_price_falls_back():
    r, err, src = lighter_rate({"rate": "0.0007", "value": "0.59146850", "direction": "long"}, 42000.0)
    assert src == "lt_rate" and abs(r - 7e-6) < 1e-12


def _snap(conn, captured, next_ts, rate, interval=3600):
    conn.execute("INSERT INTO var_snapshots (symbol, captured_at, next_ts, interval_s, interval_rate) "
                 "VALUES ('X-PERP', ?, ?, ?, ?)", (captured, next_ts, interval, rate))


def test_derive_variational_pre_post_and_no_zero_fill():
    conn = connect(":memory:")
    _snap(conn, 6600, 7200, 1e-5)
    _snap(conn, 7300, 10800, 2e-5)
    derive_variational(conn, now=12000, mode="pre", window=900, lookback_s=86400)
    rows = conn.execute("SELECT ts, rate, quality FROM settlements").fetchall()
    assert [(r["ts"], r["rate"], r["quality"]) for r in rows] == [(7200, 1e-5, "ESTIMATED")]
    derive_variational(conn, now=12000, mode="post", window=900, lookback_s=86400)
    assert conn.execute("SELECT rate FROM settlements WHERE ts=7200").fetchone()[0] == 2e-5
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1  # 10800 had no snapshot


def test_insert_idempotent_and_gap_detection():
    conn = connect(":memory:")
    rows = [("hyperliquid", "BTC", t, 3600, 1e-6, 0.0, "CONFIRMED", "hl_history", None)
            for t in (3600, 7200, 14400)]
    insert_settlements(conn, rows)
    insert_settlements(conn, rows)
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 3
    assert completeness(conn, "hyperliquid", 0) == [("BTC", 3, 4, 1)]
