"""Offline tests for the RISEx venue, using response shapes recorded from the live API (scripts/dev/probe_risex)."""
import json

import pytest

import src.costs as costs
import src.matching as matching
import src.signals as signals
from src.dashboard import build_page, flag_notes
from src.db import connect
from src.funding import ingest_risex, risex_rows
from src.signals import evaluate_all
from src.venues import risex as rx

H = 3600
NS = 10 ** 9


def _market(mid, name, category="crypto", active=True, mark="76180.34", lev="50", unlocked=True, reduce_only=False):
    return {"market_id": mid, "config": {"name": name, "max_leverage": lev, "unlocked": unlocked},
            "mark_price": mark, "last_price": mark, "quote_volume_24h": "69220561.29", "category": category,
            "active": active, "reduce_only": reduce_only, "post_only": False, "funding_interval": str(H * NS),
            "current_funding_rate": "0.00000529931816587", "funding_rate_8h": "0.00004239454532696"}


def _record(end_s, rate):
    return {"funding_rate": str(rate), "accumulated_funding": "664.84", "index_price": "76000.2",
            "start_time": str((end_s - H) * NS), "end_time": str(end_s * NS), "block_number": "1",
            "block_time": str(end_s * NS), "tx_hash": "0x0"}


@pytest.fixture
def api(monkeypatch):
    """Fake RISEx API: markets, paged funding history (2 records per page, newest first) and an order book."""
    state = {"markets": [], "history": {}, "calls": []}

    def fake_get(path, params=None):
        state["calls"].append((path, params))
        if path == "markets":
            return {"data": {"markets": state["markets"]}, "request_id": "x"}
        if path.endswith("funding-rate-history"):
            mid = path.split("/")[2]
            lo, hi = int(params["start_time"]) // NS, int(params["end_time"]) // NS
            recs = sorted((r for r in state["history"].get(mid, []) if lo <= int(r["end_time"]) // NS < hi),
                          key=lambda r: -int(r["end_time"]))
            page, per = int(params["page"]), 2
            chunk = recs[(page - 1) * per: page * per]
            return {"data": {"market_id": mid, "records": chunk, "page": page,
                             "has_next_page": page * per < len(recs)}}
        if path == "orderbook":
            return {"data": {"market_id": params["market_id"],
                             "bids": [{"price": "76183.5", "quantity": "2.86021", "order_count": 1}],
                             "asks": [{"price": "76183.6", "quantity": "0.00176", "order_count": 1},
                                      {"price": "76190.6", "quantity": "2.861085", "order_count": 1}]}}
        raise AssertionError(path)

    monkeypatch.setattr(rx, "_get", fake_get)
    return state


def test_fetch_risex_keeps_active_crypto_and_reads_leverage_and_flags(api):
    api["markets"] = [_market("1", "BTC/USDC"), _market("2", "ETH/USDC", unlocked=False),
                      _market("17", "XAU/USDC", category="commodity"), _market("36", "NVDA/USDC", category="stocks"),
                      _market("9", "OLD/USDC", active=False), _market("6", "XRP/USDC", reduce_only=True)]
    rows = {r["asset"]: r for r in matching.fetch_risex()}
    assert sorted(rows) == ["BTC", "ETH", "XRP"]                         # stocks, commodities, inactive dropped
    btc = rows["BTC"]
    assert (btc["symbol"], btc["market_id"], btc["interval_s"], btc["max_leverage"]) == ("BTC/USDC", "1", 3600, 50.0)
    assert abs(btc["unit_price"] - 76180.34) < 1e-6 and btc["asset_class"] == "crypto"
    assert "LOCKED" in rows["ETH"]["flags"] and "REDUCE_ONLY" in rows["XRP"]["flags"]


def test_risex_rows_use_period_end_and_hourly_fraction():
    rows = risex_rows("BTC/USDC", [_record(10 * H, "0.00000529931816587"), _record(9 * H, "-0.0000477")])
    assert [(r[2], r[3], r[4]) for r in rows] == [(10 * H, H, 5.29931816587e-06), (9 * H, H, -4.77e-05)]
    assert rows[0][6] == "CONFIRMED" and json.loads(rows[0][8])["index_price"] == "76000.2"


def test_ingest_risex_pages_through_history_and_repairs_gaps(api):
    conn = connect(":memory:")
    now = 1000 * H
    conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched, "
                 "active, updated_at) VALUES ('risex','BTC/USDC','BTC','1',3600,'crypto','',1,1,0)")
    api["history"]["1"] = [_record(now - k * H, 1e-5) for k in range(12) if k != 5]  # the venue lacks one hour
    st = ingest_risex(conn, now, 24 * H)
    assert st["errors"] == 0 and st["written"] == 11
    pages = [c for c in api["calls"] if c[0].endswith("funding-rate-history")]
    assert len(pages) == 6                                                # 11 records, 2 per page
    api["history"]["1"].append(_record(now - 5 * H, 2e-5))                # the venue later publishes the hole
    api["calls"].clear()
    st = ingest_risex(conn, now + H, 24 * H)
    assert st["gap_repairs"] == 1 and st["written"] == 1
    assert int(api["calls"][0][1]["start_time"]) // NS == now - 6 * H     # restarted just before the hole
    assert conn.execute("SELECT rate FROM settlements WHERE venue='risex' AND ts=?", (now - 5 * H,)).fetchone()[0] == 2e-5


def test_order_book_and_costs_use_risex_fees(api):
    bids, asks = rx.order_book("1")
    assert bids == [(76183.5, 2.86021)] and asks[0] == (76183.6, 0.00176)
    conn = connect(":memory:")
    st = costs.refresh_costs(conn, [("risex", "BTC/USDC", "1")], now=1000 * H)
    assert st["errors"] == 0
    row = conn.execute("SELECT * FROM book_costs WHERE venue='risex' AND size_usd=1000").fetchone()
    assert row["executable"] == 1 and abs(row["rt_cost_maker"] - 2 * costs.RX_MAKER_FEE) < 1e-15
    assert row["rt_cost"] > 2 * costs.RX_TAKER_FEE                       # spread + walk + two taker fees


def test_variational_risex_pair_is_evaluated_and_shown(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn = connect(":memory:")
    now = 1000 * H
    for v, sym, ivl in (("risex", "BTC/USDC", H), ("variational", "BTC-PERP", 4 * H)):
        conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched,"
                     " active, updated_at, max_leverage) VALUES (?,?,'BTC',?,?,'crypto','',1,1,0,50)",
                     (v, sym, sym, ivl))
    for k in range(0, 60, 4):
        conn.execute("INSERT INTO settlements VALUES ('variational','BTC-PERP',?,14400,1.2e-4,NULL,'ESTIMATED',"
                     "'var_pre',NULL,0)", (now - k * H,))
    for k in range(60):
        conn.execute("INSERT INTO settlements VALUES ('risex','BTC/USDC',?,3600,0,0,'CONFIRMED','rx_history',NULL,0)",
                     (now - k * H,))
    evaluate_all(conn, now)
    row = conn.execute("SELECT status, apy_1x FROM signals WHERE pair_id='BTC|risex:BTC/USDC>variational:BTC-PERP'"
                       ).fetchone()
    assert row["status"] == "QUALIFIED" and abs(row["apy_1x"] - 3e-5 * 8760 / 2) < 1e-9
    page = build_page(conn, now=now, db_path=":memory:")
    assert 'title="RISEx">RX</abbr>' in page and "RISEx markets</dt>" in page and '<th class="n" scope="col">' \
        '<button class="sort" data-col="4">RISEx</button>' in page


def test_locked_flag_blocks_and_is_explained():
    assert "LOCKED" in signals.BLOCKING_FLAGS
    notes = {n["code"]: n["text"] for n in flag_notes(["RIS_LOCKED"])}
    assert notes["RIS_LOCKED"] == "The RISEx market is locked by the venue: new positions can't be opened."
