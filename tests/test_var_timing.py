"""Offline test for the Variational timing verdict."""
from src.db import connect
from src.var_timing import timing_report


def _fill(conn, drift):
    for sym in range(60):
        for k in range(24):  # 24 snapshots, 5 min apart; settlement every 6 snapshots
            t = 1000 + k * 300
            next_ts = 1000 + (k // 6 + 1) * 1800
            rate = (k * 1e-6) if drift else ((k // 6) * 1e-6)
            conn.execute("INSERT INTO var_snapshots (symbol, captured_at, next_ts, interval_rate) "
                         "VALUES (?,?,?,?)", (f"S{sym}", t, next_ts, rate))


def test_verdict_post_when_frozen():
    conn = connect(":memory:")
    _fill(conn, drift=False)
    assert timing_report(conn, lookback_s=10**9, now=10**6)["verdict"] == "USE_POST"


def test_verdict_pre_when_drifting():
    conn = connect(":memory:")
    _fill(conn, drift=True)
    assert timing_report(conn, lookback_s=10**9, now=10**6)["verdict"] == "USE_PRE"
