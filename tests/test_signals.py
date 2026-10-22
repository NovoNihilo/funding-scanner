"""Offline tests for the signal engine."""
import json

from src.db import connect
from src.signals import (apy_numbers, build_periods, cohort_for, evaluate_all, leverage_limit, lifecycle,
                         mark_anomalies)

H = 3600
PAIR = "AAA|hyperliquid:AAA>lighter:AAA"


def _hourly(start, n, rate, quality="CONFIRMED"):
    return {start + k * H: (rate, H, quality) for k in range(n)}


def _p(nets):
    return [{"end": (k + 1) * H, "hours": 1.0, "net": n, "excluded": False, "tags": set()}
            for k, n in enumerate(nets)]


def test_mixed_interval_no_double_count():
    var_short = {8 * H: (8e-5, 8 * H, "ESTIMATED")}
    hl_long = _hourly(H, 8, 1e-6)
    ps = build_periods(hl_long, var_short, "short")
    assert len(ps) == 1 and ps[0]["hours"] == 8.0
    assert abs(ps[0]["net"] - (8e-5 - 8e-6)) < 1e-15
    assert ps[0]["quality"] == "ESTIMATED"


def test_missing_fine_hour_makes_period_incomplete():
    var_short = {8 * H: (8e-5, 8 * H, "ESTIMATED")}
    hl_long = _hourly(H, 8, 1e-6)
    del hl_long[4 * H]
    assert build_periods(hl_long, var_short, "short")[0]["net"] is None


def test_hourly_grid_gap_is_not_zero_filled():
    a = _hourly(H, 5, 1e-6)
    b = _hourly(H, 5, 3e-6)
    del b[3 * H]
    ps = build_periods(a, b, None)
    assert [p["net"] is None for p in ps] == [False, False, True, False, False]


def test_termination_after_three_and_positive_resets():
    run_start, neg, terms, _ = lifecycle(_p([1, 1, -1, -1, 1, -1, -1, -1]))
    assert terms == [8 * H] and run_start is None
    run_start, neg, terms, _ = lifecycle(_p([1, -1, -1, 1, -1, -1]))
    assert terms == [] and neg == 2 and run_start == 0


def test_missing_and_zero_handling():
    run_start, neg, terms, _ = lifecycle(_p([1, -1, None, 0, -1]))
    assert terms == [5 * H]  # None skipped (no reset), zero counts as non-profitable


def test_anomaly_excluded_but_reversal_kept():
    nets = [1e-5] * 20 + [-1e-5, 5e-3] + [1e-5] * 5
    ps = _p(nets)
    assert mark_anomalies(ps) == 1
    assert ps[21]["excluded"] and not ps[20]["excluded"]


def test_apy_capital_basis_and_leverage_range():
    spread, apy1, apymax = apy_numbers(2e-5, 4)
    assert abs(spread - 0.1752) < 1e-9 and abs(apy1 - 0.0876) < 1e-9 and abs(apymax - 0.3504) < 1e-9
    assert leverage_limit(40, None) == 5.0 and leverage_limit(3.5, 50) == 3.0


def test_cohorts():
    assert [cohort_for(h) for h in (5, 30, 100, 200)] == ["FRESH", "ACTIONABLE", "ESTABLISHED", "DURABLE"]


def _seed(conn, now, short_rate, long_rate, hours=60, rt_cost=0.0002, p99=0.04, rt_maker=0.0001):
    for v, s, lev in (("hyperliquid", "AAA", 20), ("lighter", "AAA", 20)):
        conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched,"
                     " active, updated_at, max_leverage) VALUES (?,?,?,?,3600,'crypto','',1,1,0,?)", (v, s, "AAA", s, lev))
        for size in (1000.0, 5000.0):
            conn.execute("INSERT INTO book_costs (venue, symbol, size_usd, rt_cost, mid, executable, flags, "
                         "captured_at, rt_cost_maker) VALUES (?,?,?,?,100.0,1,'',?,?)", (v, s, size, rt_cost, now, rt_maker))
    conn.execute("INSERT INTO price_stats VALUES ('AAA','hyperliquid',?,400,?)", (p99, now))
    for k in range(hours):
        t = now - k * H
        conn.execute("INSERT INTO settlements VALUES ('lighter','AAA',?,3600,?,0,'CONFIRMED','t',NULL,0)", (t, short_rate))
        conn.execute("INSERT INTO settlements VALUES ('hyperliquid','AAA',?,3600,?,0,'CONFIRMED','t',NULL,0)",
                     (t, long_rate))


def _row(conn, cols="*"):
    return conn.execute(f"SELECT {cols} FROM signals WHERE pair_id=?", (PAIR,)).fetchone()


def test_end_to_end_qualifies_after_24h_and_reverse_does_not():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)  # net 3e-5/h -> 26.3% APR -> 13.1% capital APY
    res = evaluate_all(conn, now)
    st = {r["pair_id"]: r for r in conn.execute("SELECT * FROM signals")}
    good, rev = st[PAIR], st["AAA|lighter:AAA>hyperliquid:AAA"]
    assert good["status"] == "QUALIFIED" and good["cohort"] == "ESTABLISHED"
    assert abs(good["apy_1x"] - 3e-5 * 8760 / 2) < 1e-9 and good["max_lev"] == 5.0
    assert rev["status"] == "MONITORING"
    assert res["events"] == 2


def test_below_min_apy_is_not_qualified():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=1e-5, long_rate=0.0)  # 8.76% APR -> 4.38% capital APY < 5%
    evaluate_all(conn, now)
    row = _row(conn, "status, reasons")
    assert row["status"] == "MONITORING" and "APY 1x" in row["reasons"]


def test_qualified_then_failing_gate_is_suspended():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)
    evaluate_all(conn, now)
    # data then goes stale (no new settlements) for 4 hours
    evaluate_all(conn, now + 4 * H)
    row = _row(conn, "status, reasons")
    assert row["status"] == "SUSPENDED" and "stale" in row["reasons"]


def test_slow_payback_is_reported_not_blocking():
    conn = connect(":memory:")
    now = 1000 * H
    # 3e-5/h net on $1k = $0.03/h; taker round trip 2 x 0.5% = $10 -> ~333h; maker 2 x 0.01% = $0.20 -> ~6.7h
    _seed(conn, now, short_rate=3e-5, long_rate=0.0, rt_cost=0.005, rt_maker=0.0001)
    evaluate_all(conn, now)
    row = _row(conn, "status, metrics")
    c = json.loads(row["metrics"])["costs"]["1000"]
    assert row["status"] == "QUALIFIED"
    assert abs(c["payback_h"] - 0.01 / 3e-5) < 1e-6 and c["payback_tier"] == "SLOW"
    assert abs(c["payback_h_maker"] - 0.0002 / 3e-5) < 1e-6
    assert abs(c["net_hold_usd"] - (3e-5 * 168 - 0.01) * 1000) < 1e-9


def test_missing_costs_do_not_block_and_candidates_returned():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)
    conn.execute("DELETE FROM book_costs")
    res = evaluate_all(conn, now, persist=False)
    assert ("hyperliquid", "AAA", "AAA") in res["candidates"] and ("lighter", "AAA", "AAA") in res["candidates"]
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0  # persist=False writes nothing
    evaluate_all(conn, now)  # costs are informational: the funding signal still qualifies
    row = _row(conn, "status, metrics")
    assert row["status"] == "QUALIFIED" and json.loads(row["metrics"])["costs"]["1000"]["status"] == "MISSING"


def test_stress_test_caps_leverage_range():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0, p99=0.30)  # 37.5% buffered move, 20x venues -> 2x
    evaluate_all(conn, now)
    row = _row(conn, "status, max_lev, apy_1x, apy_max")
    assert row["status"] == "QUALIFIED" and row["max_lev"] == 2.0
    assert abs(row["apy_max"] - 2 * row["apy_1x"]) < 1e-12


def test_multiple_markets_per_venue_get_distinct_pairs_and_stale_pairs_are_removed():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)
    conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched,"
                 " active, updated_at, max_leverage) VALUES ('hyperliquid','xyz:AAA','AAA','xyz:AAA',3600,'tradfi',"
                 "'HIP3,DEX=xyz',1,1,0,10)")
    res = evaluate_all(conn, now)
    ids = {r[0] for r in conn.execute("SELECT pair_id FROM signals")}
    assert res["pairs"] == 4 and "AAA|hyperliquid:xyz:AAA>lighter:AAA" in ids
    flags = conn.execute("SELECT flags FROM signals WHERE pair_id='AAA|hyperliquid:xyz:AAA>lighter:AAA'").fetchone()[0]
    assert "DIVIDEND_UNKNOWN" in flags
    conn.execute("UPDATE markets SET active=0 WHERE symbol='xyz:AAA'")
    res = evaluate_all(conn, now + H)
    assert res["removed"] == 2 and conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
