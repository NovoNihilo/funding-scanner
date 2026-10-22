"""Offline unit tests for symbol normalization, price validation and DB upsert."""
import pytest

import src.matching as matching
from src.db import connect
from src.matching import normalize_symbol, save, validate


def test_normalize_symbol():
    assert normalize_symbol("BTC-PERP") == ("BTC", 1.0)
    assert normalize_symbol("kPEPE") == ("PEPE", 1000.0)
    assert normalize_symbol("1000PEPE") == ("PEPE", 1000.0)
    assert normalize_symbol("1000000MOG") == ("MOG", 1e6)
    assert normalize_symbol("1MBABYDOGE") == ("BABYDOGE", 1e6)
    assert normalize_symbol("1INCH") == ("1INCH", 1.0)
    assert normalize_symbol("KAITO") == ("KAITO", 1.0)
    assert normalize_symbol("WTI") == ("CL", 1.0)
    assert normalize_symbol("xyz:TSLA") == ("TSLA", 1.0)
    assert normalize_symbol("xyz:GOLD") == ("XAU", 1.0)


@pytest.fixture(autouse=True)
def _any_venue(monkeypatch):
    monkeypatch.setattr(matching, "REQUIRED_VENUE", "")  # fixtures below use Hyperliquid/Lighter pairs


def _r(venue, asset, px):
    return dict(venue=venue, symbol=asset, asset=asset, multiplier=1.0, unit_price=px,
                asset_class="crypto", flags=[], market_id=asset, interval_s=3600, volume_24h_usd=0.0)


def test_validate_agree_and_mismatch():
    rows = [_r("hyperliquid", "AAA", 1.00), _r("lighter", "AAA", 1.01), _r("variational", "AAA", 1.50),
            _r("hyperliquid", "BBB", 1.0), _r("lighter", "BBB", 2.0)]
    validate(rows)
    m = {(r["venue"], r["asset"]): r for r in rows}
    assert m[("hyperliquid", "AAA")]["matched"] == 1
    assert m[("lighter", "AAA")]["matched"] == 1
    assert m[("variational", "AAA")]["matched"] == 0
    assert "PRICE_MISMATCH" in m[("variational", "AAA")]["flags"]
    assert m[("hyperliquid", "BBB")]["matched"] == 0


def test_db_upsert():
    conn = connect(":memory:")
    rows = validate([_r("hyperliquid", "AAA", 1.0), _r("lighter", "AAA", 1.0)])
    save(conn, rows, ("hyperliquid", "lighter"))
    save(conn, rows, ("hyperliquid", "lighter"))
    assert conn.execute("SELECT COUNT(*) FROM markets WHERE matched=1").fetchone()[0] == 2


def test_fetch_hyperliquid_adds_allowlisted_hip3_rwa_but_skips_duplicate_crypto(monkeypatch):
    def meta(dex=""):
        if dex == "para":
            return [{"name": "para:TSLA", "maxLeverage": 10}], [{"markPx": "251.0", "dayNtlVlm": "1"}]
        if dex == "xyz":
            return ([{"name": "xyz:TSLA", "maxLeverage": 10}, {"name": "xyz:BTC", "maxLeverage": 20},
                     {"name": "xyz:OLD", "isDelisted": True}],
                    [{"markPx": "250.0", "dayNtlVlm": "1"}, {"markPx": "84000", "dayNtlVlm": "1"}, {}])
        return [{"name": "BTC", "maxLeverage": 40}], [{"markPx": "84000", "dayNtlVlm": "1"}]
    monkeypatch.setattr(matching.hl, "meta_and_asset_ctxs", meta)
    monkeypatch.setattr(matching, "HIP3_DEXES", ["xyz"])  # para exists but is not allowlisted
    rows = {r["symbol"]: r for r in matching.fetch_hyperliquid()}
    assert set(rows) == {"BTC", "xyz:TSLA"}
    t = rows["xyz:TSLA"]
    assert t["asset"] == "TSLA" and t["asset_class"] == "tradfi" and "HIP3" in t["flags"] and t["max_leverage"] == 10


def test_class_from_matched_rows_and_power_of_ten_rescue():
    qnt = [_r("variational", "QNT", 207.5), _r("lighter", "QNT", 206.8), _r("hyperliquid", "QNT", 38.5)]
    qnt[0]["asset_class"], qnt[2]["asset_class"] = None, "tradfi"  # unrelated HIP-3 stock with the same ticker
    spx = [_r("variational", "US500", 770.6), _r("hyperliquid", "US500", 769.8), _r("lighter", "US500", 7721.0)]
    for r in spx:
        r["asset_class"] = "tradfi"
    rows = {(r["venue"], r["asset"]): r for r in validate(qnt + spx)}
    assert rows[("variational", "QNT")]["asset_class"] == "crypto" and rows[("hyperliquid", "QNT")]["matched"] == 0
    lt_spx = rows[("lighter", "US500")]
    assert lt_spx["matched"] == 1 and lt_spx["multiplier"] == 10.0 and "SCALE=10" in lt_spx["flags"]
    assert abs(lt_spx["unit_price"] - 772.1) < 1e-9


def test_required_venue_keeps_only_assets_tradable_on_it(monkeypatch):
    monkeypatch.setattr(matching, "REQUIRED_VENUE", "variational")
    rows = validate([_r("hyperliquid", "AAA", 1.0), _r("lighter", "AAA", 1.0),
                     _r("hyperliquid", "BBB", 1.0), _r("lighter", "BBB", 1.0), _r("variational", "BBB", 1.0),
                     _r("hyperliquid", "CCC", 1.0), _r("lighter", "CCC", 1.0), _r("variational", "CCC", 2.0)])
    m = {(r["venue"], r["asset"]): r for r in rows}
    assert m[("hyperliquid", "AAA")]["matched"] == 0 and "PRICE_MISMATCH" not in m[("hyperliquid", "AAA")]["flags"]
    assert all(m[(v, "BBB")]["matched"] == 1 for v in ("hyperliquid", "lighter", "variational"))
    assert all(m[(v, "CCC")]["matched"] == 0 for v in ("hyperliquid", "lighter", "variational"))
    assert "PRICE_MISMATCH" in m[("variational", "CCC")]["flags"]
