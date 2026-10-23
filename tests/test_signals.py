"""Offline tests for the signal engine."""
import json

import pytest

import src.signals as signals
from src.db import connect
from src.signals import (apy_numbers, build_periods, cohort_for, evaluate_all, leverage_limit, lifecycle,
                         mark_anomalies)

H = 3600
PAIR = "AAA|hyperliquid:AAA>lighter:AAA"


@pytest.fixture(autouse=True)
def _any_venue(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "")  # fixtures below use Hyperliquid/Lighter pairs


def _hourly(start, n, rate, quality="CONFIRMED"):
    return {start + k * H: (rate, H, quality) for k in range(n)}


def _p(nets):
    return [{"end": (k + 1) * H, "hours": 1.0, "net": n, "excluded": False, "tags": set()}
            for k, n in enumerate(nets)]


def test_variational_held_forward_hourly_without_double_counting():
    var_short = {8 * H: (8e-5, 8 * H, "ESTIMATED")}  # 8h settlement -> 1e-5 per hour
    hl_long = _hourly(H, 16, 1e-6)
    ps = build_periods(hl_long, var_short, "short")
    assert [p["end"] for p in ps] == [k * H for k in range(8, 17)]  # starts at the first known settlement
    assert all(abs(p["net"] - (1e-5 - 1e-6)) < 1e-15 and p["hours"] == 1.0 for p in ps)
    assert abs(sum(p["net"] for p in ps[1:]) - (8e-5 - 8e-6)) < 1e-15  # one interval's worth over 8 hours


def test_variational_hold_expires_and_missing_hour_stays_missing():
    var_short = {8 * H: (8e-5, 8 * H, "ESTIMATED")}
    hl_long = _hourly(H, 30, 1e-6)
    del hl_long[10 * H]
    ps = {p["end"]: p["net"] for p in build_periods(hl_long, var_short, "short")}
    assert ps[10 * H] is None and ps[17 * H] is not None  # missing HL hour; held up to interval + 1h
    assert ps[18 * H] is None and ps[30 * H] is None       # next settlement never recorded -> not extended


def test_dividend_window_hours_excluded():
    var_long = {8 * H: (1e-5, 8 * H, "ESTIMATED"), 10 * H: (1e-5, 2 * H, "ESTIMATED")}  # shortened interval
    ps = {p["end"]: p for p in build_periods(var_long, _hourly(H, 12, 5e-6), "long", normal_interval=8 * H)}
    assert not ps[9 * H]["excluded"] and ps[10 * H]["excluded"] and ps[11 * H]["excluded"]


def test_hourly_grid_gap_is_not_zero_filled():
    a = _hourly(H, 5, 1e-6)
    b = _hourly(H, 5, 3e-6)
    del b[3 * H]
    ps = build_periods(a, b, None)
    assert [p["net"] is None for p in ps] == [False, False, True, False, False]


def test_termination_after_three_and_positive_resets():
    run_start, neg, terms, _, _ = lifecycle(_p([1, 1, -1, -1, 1, -1, -1, -1]))
    assert terms == [8 * H] and run_start is None
    run_start, neg, terms, _, _ = lifecycle(_p([1, -1, -1, 1, -1, -1]))
    assert terms == [] and neg == 2 and run_start == 0


def test_missing_and_zero_handling():
    run_start, neg, terms, _, _ = lifecycle(_p([1, -1, None, 0, -1]))
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
    assert [cohort_for(h) for h in (5, 12, 30, 100, 200)] == ["FRESH", "EMERGING", "ACTIONABLE", "ESTABLISHED",
                                                              "DURABLE"]


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


def test_required_venue_drops_pairs_without_it(monkeypatch):
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)
    assert evaluate_all(conn, now)["pairs"] == 2
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched,"
                 " active, updated_at) VALUES ('variational','AAA-PERP','AAA','AAA-PERP',3600,'crypto','',1,1,0)")
    res = evaluate_all(conn, now)
    ids = {r[0] for r in conn.execute("SELECT pair_id FROM signals")}
    assert res["removed"] == 2 and res["pairs"] == 4  # hyperliquid<>lighter gone, both legs vs variational kept
    assert all("variational:" in pid for pid in ids)


def _var_pair(conn, now, var_rates, hl_rates):
    """Variational short (4h settlements) vs Hyperliquid long (hourly). var_rates: {ts: rate per 4h}."""
    for v, sym, ivl in (("hyperliquid", "AAA", 3600), ("variational", "AAA-PERP", 14400)):
        conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched,"
                     " active, updated_at, max_leverage) VALUES (?,?,'AAA',?,?,'crypto','',1,1,0,20)", (v, sym, sym, ivl))
    for t, r in var_rates.items():
        conn.execute("INSERT INTO settlements VALUES ('variational','AAA-PERP',?,14400,?,NULL,'ESTIMATED','t',NULL,0)",
                     (t, r))
    for t, r in hl_rates.items():
        conn.execute("INSERT INTO settlements VALUES ('hyperliquid','AAA',?,3600,?,0,'CONFIRMED','t',NULL,0)", (t, r))


VPAIR = "AAA|hyperliquid:AAA>variational:AAA-PERP"


def _dip(hl_rate, hours):
    """Qualified Var-short / HL-long pair (net +3e-5 per hour) whose last `hours` hours (ending now-1h) cost
    the HL long `hl_rate` per hour."""
    conn = connect(":memory:")
    now = 1000 * H
    var = {now - k * 4 * H: 1.2e-4 for k in range(0, 15)}           # short Var receives 3e-5 per hour
    hl = {now - k * H: 0.0 for k in range(60)}
    for k in range(1, hours + 1):
        hl[now - k * H] = hl_rate
    _var_pair(conn, now, var, hl)
    evaluate_all(conn, now - (hours + 1) * H)
    assert _status(conn)["status"] == "QUALIFIED"
    return conn, now


def _status(conn):
    return conn.execute("SELECT status, run_start, cohort, age_h FROM signals WHERE pair_id=?", (VPAIR,)).fetchone()


def test_three_unprofitable_hours_kill_a_run_inside_one_variational_interval(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn, now = _dip(3e-4, 3)                                         # a real reversal: -2.7e-4 per hour
    evaluate_all(conn, now - H)                                       # hours -3, -2, -1 were all negative
    row = _status(conn)
    assert row["status"] == "TERMINATED" and row["run_start"] is None
    detail = conn.execute("SELECT detail FROM signal_events WHERE to_status='TERMINATED'").fetchone()[0]
    assert detail == "killed: 3 unprofitable hours in a row lost 112% of the previous 24h's earnings"
    evaluate_all(conn, now)                                           # next positive hour starts a NEW run
    row = _status(conn)
    assert (row["status"], row["cohort"], row["age_h"]) == ("MONITORING", "FRESH", 1.0)


def test_small_dips_inside_a_healthy_run_are_tolerated(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn, now = _dip(4e-5, 3)                                         # -1e-5 per hour: 4% of the 24h cushion
    run_start = _status(conn)["run_start"]
    evaluate_all(conn, now - H)
    row = _status(conn)
    assert row["status"] == "QUALIFIED" and row["run_start"] == run_start


def test_six_unprofitable_hours_kill_a_run_whatever_their_size(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn, now = _dip(4e-5, 6)
    evaluate_all(conn, now - 2 * H)                                   # 5 small losing hours: still alive
    assert _status(conn)["status"] == "QUALIFIED"
    evaluate_all(conn, now - H)                                       # the 6th kills it
    assert _status(conn)["status"] == "TERMINATED"
    detail = conn.execute("SELECT detail FROM signal_events WHERE to_status='TERMINATED'").fetchone()[0]
    assert detail == "killed: 6 unprofitable hours in a row"


def test_lifecycle_measures_the_streak_against_the_prior_24h_only():
    def per(t, net):
        return {"end": t * H, "hours": 1.0, "net": net, "excluded": False}
    early = [per(t, 1e-3) for t in range(1, 11)]                      # big early earnings, then 30 quiet hours
    quiet = [per(t, 1e-6) for t in range(11, 41)]
    dip = [per(t, -1e-5) for t in range(41, 44)]                      # small vs the run, large vs the last 24h
    run_start, neg, terms, _, why = signals.lifecycle(early + quiet + dip)
    assert run_start is None and terms == [43 * H] and "lost" in why[0]
    soon = [per(t, -1e-5) for t in range(11, 14)]
    run_start, neg, terms, _, why = signals.lifecycle(early + soon)    # same dip right after the big hours
    assert run_start == 0 and neg == 3 and terms == []


def test_emerging_between_8_and_24_hours(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn = connect(":memory:")
    now = 1000 * H
    var = {now - k * 4 * H: 1.2e-4 for k in range(0, 4)}             # 12h of Variational history
    hl = {now - k * H: 0.0 for k in range(40)}
    _var_pair(conn, now, var, hl)
    evaluate_all(conn, now)
    row = conn.execute("SELECT status, cohort, apy_1x, flags FROM signals WHERE pair_id=?", (VPAIR,)).fetchone()
    assert row["status"] == "EMERGING" and row["cohort"] == "EMERGING" and "EARLY_APY" in row["flags"]
    assert abs(row["apy_1x"] - 3e-5 * 8760 / 2) < 1e-9


def test_payback_uses_current_rate_and_trend_detects_rise():
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=1e-5, long_rate=0.0, hours=100, rt_cost=0.0002, rt_maker=0.0001)
    for k in range(12):                                               # last 12h: rate triples
        conn.execute("UPDATE settlements SET rate=3e-5 WHERE venue='lighter' AND ts=?", (now - k * H,))
    evaluate_all(conn, now)
    m = json.loads(_row(conn, "metrics")["metrics"])
    current = (12 * 3e-5 + 12 * 1e-5) / 24
    assert abs(m["current_hourly"] - current) < 1e-12
    assert abs(m["costs"]["1000"]["payback_h"] - 0.0004 / current) < 1e-6
    assert m["trend"] == "UP" and m["trend_delta_apy"] > 0.02
