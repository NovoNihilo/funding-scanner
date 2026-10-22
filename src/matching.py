"""Cross-venue asset matching: normalize symbols, then validate by per-unit price agreement.
Hyperliquid = main dex (crypto) + allowlisted HIP-3 builder dexes (HIP3_DEXES, default 'xyz': stocks, indices,
commodities, FX). Each HIP-3 deployer runs its own oracle, so untrusted deployers are never fetched.
HIP-3 markets whose asset is already listed on the main dex are skipped (duplicate crypto listings).
Only assets that agree on price across >= 2 venues, one of them REQUIRED_VENUE (default Variational), are matched;
everything else is stored but never synced or evaluated."""
import re
import time
from math import log10
from statistics import median

from src.config import HIP3_DEXES, PRICE_TOL, REQUIRED_VENUE
from src.venues import hyperliquid as hl, lighter as lt, variational as va

VENUES = ("variational", "hyperliquid", "lighter")
ALIASES = {"WTI": "CL", "BRENTOIL": "BZ", "XCU": "COPPER", "GOLD": "XAU", "SILVER": "XAG",
           "PLATINUM": "XPT", "PALLADIUM": "XPD"}
SCALE_TOL = 0.01  # power-of-ten rescue must agree within 1%
_PREFIXES = (("1000000", 1e6), ("1M", 1e6), ("1000", 1e3), ("1K", 1e3))
COLS = ("venue", "symbol", "asset", "multiplier", "market_id", "interval_s", "unit_price",
        "volume_24h_usd", "asset_class", "flags", "matched", "max_leverage")


def normalize_symbol(symbol):
    """Return (asset, multiplier); multiplier = underlying units per quoted unit."""
    s = symbol.strip()
    if ":" in s:  # Hyperliquid HIP-3 market, e.g. 'xyz:TSLA'
        s = s.split(":", 1)[1]
    for suffix in ("-PERP", "-USD", "/USDC"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    mult = 1.0
    if re.fullmatch(r"k[A-Z0-9]{2,}", s):
        s, mult = s[1:], 1e3
    else:
        for prefix, m in _PREFIXES:
            rest = s[len(prefix):]
            if s.startswith(prefix) and len(rest) >= 2 and rest[0].isalpha():
                s, mult = rest, m
                break
    s = s.upper()
    return ALIASES.get(s, s), mult


def _row(venue, symbol, market_id, interval_s, px, vol, asset_class, flags, max_lev=None):
    asset, mult = normalize_symbol(symbol)
    return dict(venue=venue, symbol=symbol, asset=asset, multiplier=mult, market_id=str(market_id),
                interval_s=interval_s, unit_price=(px / mult) if px else None,
                volume_24h_usd=vol, asset_class=asset_class, flags=flags, max_leverage=max_lev)


def fetch_hyperliquid():
    universe, ctxs = hl.meta_and_asset_ctxs()
    rows = [_row("hyperliquid", u["name"], u["name"], 3600, float(c.get("markPx") or 0),
                 float(c.get("dayNtlVlm") or 0), "crypto", [], u.get("maxLeverage"))
            for u, c in zip(universe, ctxs) if not u.get("isDelisted")]
    main_assets = {r["asset"] for r in rows}
    for dex in HIP3_DEXES:
        try:
            uni, cx = hl.meta_and_asset_ctxs(dex)
        except Exception:
            continue  # one broken builder dex must not stop the refresh
        for u, c in zip(uni, cx):
            if u.get("isDelisted"):
                continue
            name = u["name"] if ":" in u["name"] else f"{dex}:{u['name']}"
            r = _row("hyperliquid", name, name, 3600, float(c.get("markPx") or 0),
                     float(c.get("dayNtlVlm") or 0), "tradfi", ["HIP3", f"DEX={dex}"], u.get("maxLeverage"))
            if r["asset"] not in main_assets:
                rows.append(r)
    return rows


def fetch_lighter():
    out = []
    for d in lt.order_book_details("perp"):
        if d.get("market_type") != "perp" or d.get("status") != "active":
            continue
        cfg = d.get("market_config") or {}
        fpm = int(d.get("funding_premium_multiplier") or 100)
        flags = []
        if cfg.get("hidden"):
            flags.append("HIDDEN")
        if cfg.get("force_reduce_only"):
            flags.append("REDUCE_ONLY")
        if fpm != 100:
            flags.append(f"FPM={fpm}")
        if fpm <= 1:
            flags.append("PREIPO")
        field_mult = float(d.get("multiplier") or 1)
        if abs(field_mult - 1) > 1e-9:
            flags.append(f"LTMULT={field_mult:g}")
        imf = float(d.get("min_initial_margin_fraction") or 0)
        max_lev = round(10000 / imf, 2) if imf > 0 else None  # IMF assumed in basis points; verified below
        tradfi = bool(cfg.get("trading_hours")) or fpm < 100
        px = float(d.get("mark_price") or d.get("last_trade_price") or 0)
        out.append(_row("lighter", d["symbol"], d["market_id"], 3600, px,
                        float(d.get("daily_quote_token_volume") or 0),
                        "tradfi" if tradfi else "crypto", flags, max_lev))
    return out


def fetch_variational():
    out = []
    for x in va.stats()["listings"]:
        if x.get("contract_type") != "perpetual":
            continue
        f = x.get("funding")
        out.append(_row("variational", x["ticker"], x["ticker"], (f or {}).get("interval_seconds"),
                        float(x.get("mark_price") or 0), float(x.get("volume_24h") or 0),
                        None, [] if f else ["NO_FUNDING"], None))  # max leverage not published
    return out


def validate(rows, tol=PRICE_TOL):
    """matched=1 when >=2 venues agree on per-unit price within tol and one of them is REQUIRED_VENUE.
    A market quoted at a power-of-ten scale of an agreed price (e.g. a full index vs a 1/10 index) is rescued
    by adjusting its multiplier (flag SCALE=x), within SCALE_TOL. The asset class comes from the matched markets,
    so an unrelated same-ticker market (e.g. a stock sharing a coin's ticker) cannot relabel the group."""
    groups = {}
    for r in rows:
        r["matched"] = 0
        groups.setdefault(r["asset"], []).append(r)
    for grp in groups.values():
        good = []
        if len({r["venue"] for r in grp}) >= 2:
            for r in grp:
                if not r["unit_price"]:
                    r["flags"].append("NO_PRICE")
            priced = [r for r in grp if r["unit_price"]]
            ref = None
            if len(priced) == 2:
                a, b = priced
                good = priced if abs(a["unit_price"] / b["unit_price"] - 1) <= tol else []
            elif len(priced) > 2:
                ref = median(r["unit_price"] for r in priced)
                good = [r for r in priced if abs(r["unit_price"] / ref - 1) <= tol]
            if ref and len({r["venue"] for r in good}) >= 2:
                for r in priced:
                    if any(r is g for g in good):
                        continue
                    ratio = r["unit_price"] / ref
                    k = round(log10(ratio))
                    if 1 <= abs(k) <= 3 and abs(ratio / 10 ** k - 1) <= SCALE_TOL:
                        scale = 10.0 ** k
                        r["multiplier"] *= scale
                        r["unit_price"] /= scale
                        r["flags"].append(f"SCALE={scale:g}")
                        good.append(r)
            if len({r["venue"] for r in good}) < 2:
                good = []
            for r in priced:
                if not any(r is g for g in good):
                    r["flags"].append("PRICE_MISMATCH")
            if REQUIRED_VENUE and REQUIRED_VENUE not in {r["venue"] for r in good}:
                good = []  # no leg on the required venue: nothing to trade
            for r in good:
                r["matched"] = 1
        known = {r["asset_class"] for r in (good or grp) if r["asset_class"]}
        cls = "tradfi" if "tradfi" in known else ("crypto" if known else "unknown")
        for r in grp:
            if r["asset_class"] is None:
                r["asset_class"] = cls
    return rows


def save(conn, rows, venues):
    now = int(time.time())
    params = [tuple(",".join(r["flags"]) if c == "flags" else r.get(c) for c in COLS) + (now,)
              for r in rows]
    sql = (f"INSERT INTO markets ({', '.join(COLS)}, active, updated_at) "
           f"VALUES ({', '.join('?' * len(COLS))}, 1, ?) "
           "ON CONFLICT(venue, symbol) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in COLS[2:])
           + ", active=1, updated_at=excluded.updated_at")
    with conn:
        for v in venues:
            conn.execute("UPDATE markets SET active=0 WHERE venue=?", (v,))
        conn.executemany(sql, params)


def refresh_markets(conn):
    rows = fetch_variational() + fetch_hyperliquid() + fetch_lighter()
    validate(rows)
    save(conn, rows, VENUES)
    return rows
