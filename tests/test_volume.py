"""Offline tests for the Volume tab: cheapest delta-neutral round trips per asset, at every size."""
import json
import threading
import time
import urllib.request

import pytest

import src.volume as volume
from src.dashboard import build_page, serve
from src.db import connect

NOW = 1_000_000


def _market(conn, venue, symbol, asset, lev=20, cls="crypto"):
    conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched, active,"
                 " updated_at, max_leverage) VALUES (?,?,?,?,3600,?,'',1,1,0,?)", (venue, symbol, asset, symbol, cls, lev))


def _cost(conn, venue, symbol, size, rt, maker=None, age=60, flags=""):
    conn.execute("INSERT OR REPLACE INTO book_costs (venue, symbol, size_usd, rt_cost, mid, executable, flags, "
                 "captured_at, rt_cost_maker) VALUES (?,?,?,?,100,1,?,?,?)",
                 (venue, symbol, size, rt, flags, NOW - age, maker if maker is not None else rt))


def _signal(conn, pair_id, hourly):
    conn.execute("INSERT INTO signals (pair_id, asset, long_venue, long_symbol, short_venue, short_symbol, status, "
                 "age_h, neg_streak, metrics, flags, reasons, updated_at) VALUES (?,?,?,?,?,?,'MONITORING',0,0,?,'','[]',?)",
                 (pair_id, pair_id.split("|")[0], *[x for leg in pair_id.split("|")[1].split(">")
                                                    for x in leg.split(":", 1)],
                  json.dumps({"current_hourly": hourly}), NOW))


@pytest.fixture
def db():
    conn = connect(":memory:")
    for v, s in (("variational", "AAA-PERP"), ("hyperliquid", "AAA"), ("lighter", "AAA"),
                 ("variational", "BBB-PERP"), ("hyperliquid", "BBB"), ("hyperliquid", "CCC")):
        _market(conn, v, s, s.split("-")[0])
    for size, k in ((1000, 1), (100000, 4)):                    # costs grow with size
        _cost(conn, "variational", "AAA-PERP", size, 4e-4 * k)
        _cost(conn, "hyperliquid", "AAA", size, 10e-4 * k, maker=3e-4)
        _cost(conn, "lighter", "AAA", size, 6e-4 * k, maker=0.0)
        _cost(conn, "variational", "BBB-PERP", size, 2e-4 * k)
        _cost(conn, "hyperliquid", "BBB", size, 5e-4 * k)
    _cost(conn, "lighter", "AAA", 1000, 1e-4, age=3 * 3600)     # a newer-looking but stale book is ignored
    _signal(conn, "AAA|variational:AAA-PERP>lighter:AAA", -2e-5)
    _signal(conn, "AAA|lighter:AAA>variational:AAA-PERP", 2e-5)
    return conn


def test_board_ranks_cheapest_pair_and_prices_variational_volume(db, monkeypatch):
    monkeypatch.setattr(volume, "VOLUME_MAX_AGE_S", 7200)
    _cost(db, "lighter", "AAA", 1000, 6e-4, maker=0.0)          # restore the fresh Lighter book
    rows = volume.board(db, 1000, NOW)
    assert [r["asset"] for r in rows] == ["BBB", "AAA"]          # CCC: no Variational market -> not shared
    aaa = rows[1]["best"]
    assert (aaa["venue"], aaa["short"]) == ("lighter", "LT")     # VAR+LT 10 bp beats VAR+HL 14 bp
    assert abs(aaa["taker"] - 10e-4) < 1e-12 and abs(aaa["usd"] - 1.0) < 1e-9
    assert abs(aaa["per10k_var"] - 5.0) < 1e-9                   # $1 per round trip / $2k of Variational volume
    assert abs(aaa["per10k_var_maker"] - 2.0) < 1e-9 and abs(aaa["per10k_all"] - 2.5) < 1e-9
    f = aaa["funding"]                                           # held long LT / short VAR: earns 2e-5 per hour
    assert (f["long"], f["short"]) == ("LT", "VAR") and abs(f["daily_usd"] - 2e-5 * 24 * 1000) < 1e-12


def test_stale_books_are_left_out(db):
    rows = {r["asset"]: r for r in volume.board(db, 1000, NOW)}
    assert rows["AAA"]["best"]["venue"] == "hyperliquid"         # Lighter's only $1k book is 3h old
    assert rows["AAA"]["venues"]["lighter"][0]["rt"] is None


def test_payload_merges_every_size(db, monkeypatch):
    monkeypatch.setattr(volume, "VOLUME_SIZES", [1000.0, 100000.0])
    db.execute("INSERT INTO runs (job, started_at, finished_at, ok, detail) VALUES ('volume_sweep', ?, ?, 1, ?)",
               (NOW - 900, NOW - 300, json.dumps({"markets": 410, "errors": 0, "no_depth": 3})))
    d = volume.payload(db, NOW)
    assert d["sizes"] == [1000, 100000] and [a["asset"] for a in d["assets"]] == ["BBB", "AAA"]
    bbb = d["assets"][0]
    assert abs(bbb["best"][1000]["per10k_var"] - 3.5) < 1e-9 and abs(bbb["best"][100000]["per10k_var"] - 14) < 1e-9
    assert [v["venue"] for v in d["assets"][1]["venues"]] == ["variational", "hyperliquid", "lighter"]
    assert d["sweep"]["markets"] == 410 and json.dumps(d)                      # JSON-serialisable


def test_sweep_covers_shared_markets_oldest_first(db):
    todo = volume.sweep_markets(db, NOW - 120)                   # refreshed in the last 2 minutes: skipped
    assert todo == [("lighter", "AAA", "AAA")]                   # stale (3h) Lighter book only
    todo = volume.sweep_markets(db, NOW)
    assert todo[0][0] == "variational" and ("hyperliquid", "CCC", "CCC") not in todo


def test_page_has_volume_tab_and_api(db, tmp_path):
    page = build_page(db, now=NOW, db_path=":memory:")
    assert 'id="tab-volume"' in page and 'id="t-volume"' in page
    path = str(tmp_path / "v.db")
    conn = connect(path)
    _market(conn, "variational", "AAA-PERP", "AAA")
    _market(conn, "hyperliquid", "AAA", "AAA")
    now = int(time.time())
    for v, s in (("variational", "AAA-PERP"), ("hyperliquid", "AAA")):
        conn.execute("INSERT INTO book_costs (venue, symbol, size_usd, rt_cost, mid, executable, flags, captured_at, "
                     "rt_cost_maker) VALUES (?,?,1000,5e-4,100,1,'',?,5e-4)", (v, s, now))
    conn.commit()
    stop = threading.Event()
    t = threading.Thread(target=serve, args=("127.0.0.1", 8796, stop, path), daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8796/api/volume", timeout=5).read())
        assert d["assets"][0]["asset"] == "AAA" and abs(d["assets"][0]["best"]["1000"]["per10k_var"] - 5) < 1e-9
    finally:
        stop.set()
        t.join(timeout=5)
