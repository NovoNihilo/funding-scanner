"""Offline tests for execution costs and the leverage stress test."""
import src.costs as costs
from src.costs import book_round_trip, p99_24h_move, parse_iso, stress_leverage, var_round_trip, walk
from src.db import connect

H = 3600


def test_walk_and_depth():
    asks = [(100.0, 1.0), (101.0, 1.0)]
    assert abs(walk(asks, 150) - 150 / (1 + 50 / 101)) < 1e-9
    assert walk(asks, 1000) is None  # only ~$201 of depth


def test_book_round_trip_includes_spread_slippage_and_fees():
    bids, asks = [(99.0, 10.0)], [(101.0, 10.0)]
    cost, mid = book_round_trip(bids, asks, 500, 0.00045)
    assert mid == 100.0 and abs(cost - (0.02 + 0.0009)) < 1e-12
    assert book_round_trip(bids, asks, 5000, 0)[0] is None


def _snap(updated):
    return {"bid_1k": 99.9, "ask_1k": 100.1, "bid_100k": 99.5, "ask_100k": 100.5, "mark_price": 100.0,
            "quotes_updated_at": updated}


def test_var_quotes_interpolated_and_penalised():
    now = parse_iso("2026-10-22T01:00:00.123456789Z")
    cost1, _, flags1 = var_round_trip(_snap("2026-10-22T00:58:00.000000000Z"), 1000, now)
    assert abs(cost1 - 0.002 * 1.25) < 1e-12 and flags1 == ["INDICATIVE"]
    cost5, _, _ = var_round_trip(_snap("2026-10-22T00:58:00.000000000Z"), 5000, now)
    assert cost1 < cost5 < 0.01 * 1.25
    cost_stale, _, flags = var_round_trip(_snap("2026-10-22T00:40:00.000000000Z"), 1000, now)
    assert "STALE_QUOTES" in flags and abs(cost_stale - 0.002 * 2.0) < 1e-12


def test_p99_move():
    closes = {k * H: 100.0 * (1.001 ** k) for k in range(200)}
    p99, n = p99_24h_move(closes)
    assert n == 176 and abs(p99 - (1.001 ** 24 - 1)) < 1e-9
    assert p99_24h_move({0: 1.0, 86400: 1.1})[0] is None


def test_stress_leverage():
    # 8% p99 move -> 10% buffered; HL 40x (mmr 1.25%) -> 8.8x; Variational -> 1/(2*0.1) = 5x
    lev, move = stress_leverage(0.08, [("hyperliquid", 40), ("variational", None)])
    assert abs(move - 0.10) < 1e-12 and lev == 5.0
    lev, _ = stress_leverage(0.30, [("hyperliquid", 10), ("lighter", 10)])
    assert lev == 2.0  # 1/(0.375+0.05) = 2.35
    lev, move = stress_leverage(None, [("hyperliquid", 40), ("lighter", 50)])
    assert abs(move - 0.375) < 1e-12 and lev == 2.0


def test_refresh_costs_stores_taker_and_maker(monkeypatch):
    book = ([(99.99, 100.0)], [(100.01, 100.0)])
    monkeypatch.setattr(costs.hl, "l2_book", lambda coin: book)
    monkeypatch.setattr(costs.lt, "order_book_orders", lambda mid: book)
    monkeypatch.setattr(costs.lt, "order_book_details",
                        lambda f="perp": [{"market_id": 7, "taker_fee": "0.0200", "maker_fee": "0.0040"}])
    conn = connect(":memory:")
    stats = costs.refresh_costs(conn, [("hyperliquid", "BTC", "BTC"), ("lighter", "BTC", "7"),
                                       ("hyperliquid", "xyz:TSLA", "xyz:TSLA")], now=1000)
    assert stats == {"markets": 3, "errors": 0, "no_depth": 0}
    hip3 = conn.execute("SELECT * FROM book_costs WHERE symbol='xyz:TSLA' AND size_usd=1000").fetchone()
    assert abs(hip3["rt_cost"] - (0.0002 + 2 * 0.0009)) < 1e-9 and hip3["flags"] == "HIP3_FEE_EST"
    r = {x["venue"]: x for x in conn.execute("SELECT * FROM book_costs WHERE size_usd=1000 AND symbol='BTC'")}
    assert abs(r["hyperliquid"]["rt_cost"] - (0.0002 + 2 * 0.00045)) < 1e-9
    assert abs(r["hyperliquid"]["rt_cost_maker"] - 2 * 0.00015) < 1e-12
    assert abs(r["lighter"]["rt_cost"] - (0.0002 + 2 * 0.0002)) < 1e-9  # percent fees converted
    assert abs(r["lighter"]["rt_cost_maker"] - 2 * 0.00004) < 1e-12
