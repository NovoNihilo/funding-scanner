"""Offline tests for the signal engine."""
from src.db import connect
from src.signals import (apy_numbers, build_periods, cohort_for, evaluate_all, leverage_limit, lifecycle,
                         mark_anomalies)

H = 3600


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


def _seed(conn, now, short_rate, long_rate, hours=60):
    for v, s, lev in (("hyperliquid", "AAA", 20), ("lighter", "AAA", 20)):
        conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched,"
                     " active, updated_at, max_leverage) VALUES (?,?,?,?,3600,'crypto','',1,1,0,?)", (v, s, "AAA", s, lev))
    for k in range(hours):
        t = now - k * H
        conn.execute("INSERT INTO settlements VALUES ('lighter','AAA',?,3600,?,0,'CONFIRMED','t',NULL,0)", (t, short_rate))
        conn.execute("INSERT INTO settlements VALUES ('hyperliquid','AAA',?,3600,?,0,'CONFIRMED','t',NULL,0)",
                     (t, long_rate))


def test_end_to_end_qualifies_after_24h_and_reverse_does_not():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)  # net 3e-5/h -> 26.3% APR -> 13.1% capital APY
    res = evaluate_all(conn, now)
    st = {r["pair_id"]: r for r in conn.execute("SELECT * FROM signals")}
    good, rev = st["AAA|hyperliquid>lighter"], st["AAA|lighter>hyperliquid"]
    assert good["status"] == "QUALIFIED" and good["cohort"] == "ESTABLISHED"
    assert abs(good["apy_1x"] - 3e-5 * 8760 / 2) < 1e-9 and good["max_lev"] == 5.0
    assert rev["status"] == "MONITORING"
    assert res["events"] == 2


def test_below_min_apy_is_not_qualified():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=1e-5, long_rate=0.0)  # 8.76% APR -> 4.38% capital APY < 5%
    evaluate_all(conn, now)
    row = conn.execute("SELECT status, reasons FROM signals WHERE pair_id='AAA|hyperliquid>lighter'").fetchone()
    assert row["status"] == "MONITORING" and "APY 1x" in row["reasons"]


def test_qualified_then_failing_gate_is_suspended():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)
    evaluate_all(conn, now)
    # data then goes stale (no new settlements) for 4 hours
    evaluate_all(conn, now + 4 * H)
    row = conn.execute("SELECT status, reasons FROM signals WHERE pair_id='AAA|hyperliquid>lighter'").fetchone()
    assert row["status"] == "SUSPENDED" and "stale" in row["reasons"]
