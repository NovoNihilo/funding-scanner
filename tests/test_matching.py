"""Offline unit tests for symbol normalization, price validation and DB upsert."""
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
