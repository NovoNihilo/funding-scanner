"""Offline tests for the Markets tab: per-venue hourly series, spreads against Variational, overview and API."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from src.dashboard import build_page, serve
from src.db import connect
from src.markets import APY_FACTOR, asset_detail, describe, hourly, overview

H = 3600
NOW = 1000 * H


def _market(conn, venue, symbol, asset, ivl, cls="crypto", lev=20):
    conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched, active,"
                 " updated_at, max_leverage) VALUES (?,?,?,?,?,?,'',1,1,0,?)", (venue, symbol, asset, symbol, ivl, cls, lev))


def _settle(conn, venue, symbol, ts, ivl, rate):
    conn.execute("INSERT INTO settlements VALUES (?,?,?,?,?,NULL,'CONFIRMED','t',NULL,0)", (venue, symbol, ts, ivl, rate))


def _seed(conn, now=NOW):
    """AAA: Variational 8h at 8e-5 per interval (1e-5/h), Hyperliquid hourly at 2e-6/h, Lighter hourly at 4e-5/h."""
    _market(conn, "variational", "AAA-PERP", "AAA", 8 * H)
    _market(conn, "hyperliquid", "AAA", "AAA", H)
    _market(conn, "lighter", "AAA", "AAA", H)
    _market(conn, "hyperliquid", "BBB", "BBB", H)  # no Variational market: not in the overview
    for k in range(0, 60, 8):
        _settle(conn, "variational", "AAA-PERP", now - k * H, 8 * H, 8e-5)
    for k in range(60):
        _settle(conn, "hyperliquid", "AAA", now - k * H, H, 2e-6)
        _settle(conn, "lighter", "AAA", now - k * H, H, 4e-5)
        _settle(conn, "hyperliquid", "BBB", now - k * H, H, 1e-6)
    conn.execute("INSERT INTO var_snapshots (symbol, captured_at, next_ts, interval_s, interval_rate) "
                 "VALUES ('AAA-PERP', ?, ?, 28800, 1.6e-4)", (now - 60, now + 8 * H))


def test_hourly_holds_variational_forward_and_converts_intervals():
    var = {8 * H: (8e-5, 8 * H, "ESTIMATED")}
    h = hourly(var, True, 7 * H, 20 * H)
    assert sorted(h) == [k * H for k in range(8, 18)]          # held for interval + 1h, then stops
    assert all(abs(v - 1e-5) < 1e-15 for v in h.values())
    hl = hourly({5 * H: (3e-6, H, "C"), 9 * H: (4e-6, H, "C")}, False, 5 * H, 9 * H)
    assert hl == {9 * H: 4e-6}                                  # window is (lo, hi]


def test_describe_gives_builder_markets_their_own_colour():
    assert describe("hyperliquid", "xyz:EWZ") == ("hip3", "Hyperliquid xyz", "HL xyz", 4)
    assert describe("variational", "BTC-PERP")[3] == 1 and describe("lighter", "BTC")[3] == 3


def test_overview_rows_and_widest_spread():
    conn = connect(":memory:")
    _seed(conn)
    rows = overview(conn, NOW)
    assert [r["asset"] for r in rows] == ["AAA"]                # only assets with a Variational leg
    r = rows[0]
    var = next(m for m in r["markets"] if m["venue"] == "variational")
    assert abs(var["avg24"] - 1e-5) < 1e-15 and r["var_interval_s"] == 8 * H
    b = r["best"]                                               # Lighter 4e-5 vs Var 1e-5: gap 3e-5 > HL's 8e-6
    assert b["other"] == "LT" and not b["short_var"] and abs(b["apy"] - 3e-5 * APY_FACTOR) < 1e-9


def test_asset_detail_series_spreads_stats_and_live_rate():
    conn = connect(":memory:")
    _seed(conn)
    d = asset_detail(conn, "AAA", NOW, days=2)
    assert [s["venue"] for s in d["series"]] == ["variational", "hyperliquid", "lighter"]
    var = d["series"][0]
    assert var["points"][-1] == [NOW, 1e-5] and abs(var["stats"]["live"] - 2e-5) < 1e-15
    assert var["stats"]["next_ts"] == NOW + 8 * H and var["stats"]["pos_share_7d"] == 1.0
    sp = {s["short"]: s for s in d["spreads"]}
    assert abs(sp["HL"]["stats"]["avg24"] - 8e-6) < 1e-15        # positive: short Variational earns
    assert abs(sp["LT"]["stats"]["avg24"] + 3e-5) < 1e-15        # negative: long Variational earns
    assert len(var["points"]) == 48 and asset_detail(conn, "ZZZ", NOW) is None


def test_page_has_markets_tab_with_one_row_per_asset():
    conn = connect(":memory:")
    _seed(conn)
    page = build_page(conn, now=NOW, db_path=":memory:")
    assert 'id="tab-markets"' in page and page.count('<tr data-asset="AAA"') == 1 and 'data-asset="BBB"' not in page
    assert "Long VAR, short LT" in page


def test_api_asset_route(tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    _seed(conn, int(time.time()) // (8 * H) * (8 * H))  # live clock: the API serves the latest 14 days
    conn.commit()
    stop = threading.Event()
    t = threading.Thread(target=serve, args=("127.0.0.1", 8797, stop, db), daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8797/api/asset?a=AAA", timeout=5).read())
        assert d["asset"] == "AAA" and len(d["series"]) == 3 and len(d["spreads"]) == 2
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:8797/api/asset?a=NOPE", timeout=5)
    finally:
        stop.set()
        t.join(timeout=5)
