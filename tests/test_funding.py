"""Offline tests: Lighter precision/sign, Variational derivation, idempotency, gap detection."""
from src.db import connect
from src.funding import (_since, completeness, derive_variational, first_gap, housekeeping, insert_settlements,
                         lighter_rate, next_capture_time, upcoming_settlements)


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
    derive_variational(conn, now=12000, mode="pre", window=900, lookback_s=86400, late_window=900)
    rows = conn.execute("SELECT ts, rate, quality FROM settlements").fetchall()
    assert [(r["ts"], r["rate"], r["quality"]) for r in rows] == [(7200, 1e-5, "ESTIMATED")]
    derive_variational(conn, now=12000, mode="post", window=900, lookback_s=86400)
    assert conn.execute("SELECT rate FROM settlements WHERE ts=7200").fetchone()[0] == 2e-5
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 1  # 10800 had no snapshot


def test_pre_is_recorded_as_soon_as_settlement_passes():
    conn = connect(":memory:")
    _snap(conn, 7170, 7200, 3e-5)  # captured 30s before settlement
    assert derive_variational(conn, now=7199, mode="pre", window=900) == 0  # settlement not reached yet
    derive_variational(conn, now=7230, mode="pre", window=900)
    assert conn.execute("SELECT rate, source FROM settlements WHERE ts=7200").fetchone()[:] == (3e-5, "var_pre")


def test_late_fallback_then_upgrade_and_never_beyond_late_window():
    conn = connect(":memory:")
    _snap(conn, 7200 - 1800, 7200, 1e-5)  # only a 30-min-old snapshot survived
    derive_variational(conn, now=7300, mode="pre", window=900, late_window=3600)
    assert conn.execute("SELECT source FROM settlements WHERE ts=7200").fetchone()[0] == "var_pre_late"
    _snap(conn, 7200 - 60, 7200, 2e-5)  # a timely snapshot shows up (e.g. delayed write) -> replaces the fallback
    derive_variational(conn, now=7300, mode="pre", window=900, late_window=3600)
    assert conn.execute("SELECT rate, source FROM settlements WHERE ts=7200").fetchone()[:] == (2e-5, "var_pre")
    _snap(conn, 20000 - 5000, 20000, 9e-5)  # older than the late window -> nothing is recorded
    derive_variational(conn, now=20100, mode="pre", window=900, late_window=3600)
    assert conn.execute("SELECT COUNT(*) FROM settlements WHERE ts=20000").fetchone()[0] == 0


def test_capture_schedule_adds_pre_settlement_captures():
    assert next_capture_time(1000, [], pre_offsets=(180, 90, 30)) == 1230  # regular :00:30 grid
    assert next_capture_time(1000, [1200], pre_offsets=(180, 90, 30)) == 1020
    assert next_capture_time(1020, [1200], pre_offsets=(180, 90, 30)) == 1110
    assert next_capture_time(1170, [1200], pre_offsets=(180, 90, 30)) == 1230
    conn = connect(":memory:")
    _snap(conn, 900, 1200, 1e-5)
    _snap(conn, 901, 3600, 1e-5)
    _snap(conn, 50, 9999, 1e-5)  # too old to count as a current announcement
    assert upcoming_settlements(conn, 1000) == [1200, 3600]


def test_housekeeping_prunes_old_snapshots_runs_and_events():
    conn = connect(":memory:")
    now = 10 * 86400
    _snap(conn, now - 4 * 86400, now, 1e-5)
    _snap(conn, now - 3600, now, 1e-5)
    conn.execute("INSERT INTO runs (job, started_at) VALUES ('x', ?)", (now - 40 * 86400,))
    for ts in (now - 40 * 86400, now - 3600):
        conn.execute("INSERT INTO signal_events (pair_id, ts, event, to_status) VALUES ('p', ?, 'STATUS', 'X')", (ts,))
    assert housekeeping(conn, now) == {"snapshots_pruned": 1, "runs_pruned": 1, "events_pruned": 1}
    assert conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM var_snapshots").fetchone()[0] == 1


def test_gap_repair_refetches_from_hole():
    conn = connect(":memory:")
    H = 3600
    hours = [k for k in range(100) if k not in (40, 41)]
    insert_settlements(conn, [("lighter", "ETH", k * H, H, 1e-6, 0.0, "CONFIRMED", "t", None) for k in hours])
    assert first_gap(conn, "lighter", "ETH", 0) == 39 * H
    conn.execute("INSERT INTO sync_state (venue, symbol, last_ts) VALUES ('lighter', 'ETH', ?)", (99 * H,))
    assert _since(conn, "lighter", "ETH", 100 * H, 100 * H) == (39 * H, True)
    assert _since(conn, "lighter", "ETH", 100 * H, 50 * H) == (96 * H, False)  # hole outside the window


def test_insert_idempotent_and_gap_detection():
    conn = connect(":memory:")
    rows = [("hyperliquid", "BTC", t, 3600, 1e-6, 0.0, "CONFIRMED", "hl_history", None)
            for t in (3600, 7200, 14400)]
    insert_settlements(conn, rows)
    insert_settlements(conn, rows)
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 3
    assert completeness(conn, "hyperliquid", 0) == [("BTC", 3, 4, 1)]
