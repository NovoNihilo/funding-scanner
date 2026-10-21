"""Network smoke tests: venues reachable; HL and Lighter return hourly settled funding."""
import statistics
import time

from src.venues import hyperliquid as hl, lighter as lt, variational as va

NOW = time.time()


def _median_gap(ts):
    ts = sorted(ts)
    return statistics.median(b - a for a, b in zip(ts, ts[1:]))


def test_variational_stats():
    perps = [x for x in va.stats()["listings"] if x.get("contract_type") == "perpetual"]
    assert len(perps) > 50
    btc = next(x for x in perps if x["ticker"] == "BTC-PERP")
    assert btc["funding"]["interval_seconds"] > 0


def test_hyperliquid_funding_history_hourly():
    rows = hl.funding_history("BTC", (NOW - 24 * 3600) * 1000)
    assert len(rows) >= 20
    assert abs(_median_gap([r["time"] / 1000 for r in rows]) - 3600) < 60


def test_lighter_funding_history_hourly():
    btc = next(b for b in lt.order_books() if b["symbol"] == "BTC")
    rows = lt.fundings(btc["market_id"], NOW - 24 * 3600, NOW)
    if not rows:
        rows = lt.fundings(btc["market_id"], (NOW - 24 * 3600) * 1000, NOW * 1000)
    assert len(rows) >= 20
    ts = [float(r["timestamp"]) for r in rows]
    ts = [t / 1000 if t > 1e11 else t for t in ts]
    assert abs(_median_gap(ts) - 3600) < 60
