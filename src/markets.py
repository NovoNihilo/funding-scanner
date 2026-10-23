"""Per-asset funding across venues, for the dashboard's Markets tab.

Every rate is converted to an HOURLY rate (fraction of position notional per hour; positive = longs pay shorts).
The page shows rates as APR (hourly x 8760) and spreads as APY at 1x on capital, like the rest of the dashboard.
Variational's rate is held from each settlement until the next one (never longer than its interval + 1h),
exactly as the signal engine does, so the charts show what a position actually paid or earned each hour."""
import time

from src.config import APY_BASIS, MIN_APY_1X
from src.signals import _load, var_hourly

HOUR = 3600
APR_FACTOR = 8760
APY_FACTOR = 8760 / 2 if APY_BASIS == "capital" else 8760  # spread -> APY at 1x, as on the other tabs
NAMES = {"variational": "Variational", "hyperliquid": "Hyperliquid", "lighter": "Lighter"}
SHORT = {"variational": "VAR", "hyperliquid": "HL", "lighter": "LT"}
SLOT = {"variational": 1, "hyperliquid": 2, "lighter": 3, "hip3": 4}  # fixed colour per venue, never by rank
MAX_DAYS = 14


def describe(venue, symbol):
    """(series key, label, short label, colour slot) for a market. HIP-3 markets get their own colour."""
    if venue == "hyperliquid" and ":" in symbol:
        dex = symbol.split(":", 1)[0]
        return "hip3", f"Hyperliquid {dex}", f"HL {dex}", SLOT["hip3"]
    return venue, NAMES.get(venue, venue), SHORT.get(venue, venue), SLOT.get(venue, 4)


def hourly(rates, is_var, lo, hi):
    """{hour end: hourly rate} for lo < t <= hi. Venues settling hourly use their settlements directly;
    Variational's last settlement is held forward hour by hour."""
    out = {}
    if is_var:
        keys = sorted(rates)
        if not keys:
            return out
        for t in range((lo // HOUR + 1) * HOUR, hi // HOUR * HOUR + 1, HOUR):
            v = var_hourly(rates, keys, t)
            if v is not None:
                out[t] = v[0]
        return out
    for ts, (rate, ivl, _) in rates.items():
        if lo < ts <= hi:
            out[ts] = rate / ((ivl or HOUR) / HOUR)
    return out


def _mean(vals):
    vals = list(vals)
    return sum(vals) / len(vals) if vals else None


def _window(h, now, hours):
    return [v for t, v in h.items() if t > now - hours * HOUR]


def _markets(conn, asset=None):
    q = ("SELECT venue, symbol, asset, asset_class, interval_s, max_leverage FROM markets "
         "WHERE matched=1 AND active=1")
    args = ()
    if asset is not None:
        q += " AND asset=?"
        args = (asset,)
    return conn.execute(q + " ORDER BY asset, venue, symbol", args).fetchall()


def _spread(var_h, other_h):
    """{hour: Variational hourly - other hourly} over the hours both have. Positive: short Variational earns."""
    return {t: var_h[t] - other_h[t] for t in var_h.keys() & other_h.keys()}


def overview(conn, now=None):
    """One row per asset that has a Variational market: each market's latest-hour and 24h-average rate,
    and the widest 24h-average spread between Variational and another venue. Sorted widest spread first."""
    now = int(now or time.time()) // HOUR * HOUR
    lo = now - 24 * HOUR
    by_asset = {}
    for m in _markets(conn):
        by_asset.setdefault(m["asset"], []).append(m)
    rows = []
    for asset, ms in by_asset.items():
        if not any(m["venue"] == "variational" for m in ms):
            continue
        cells, hs = [], {}
        for m in ms:
            is_var = m["venue"] == "variational"
            h = hourly(_load(conn, m["venue"], m["symbol"], lo - 9 * HOUR), is_var, lo, now)
            key, label, short, slot = describe(m["venue"], m["symbol"])
            hs[(m["venue"], m["symbol"])] = h
            cells.append({"venue": m["venue"], "symbol": m["symbol"], "key": key, "label": label, "short": short,
                          "now": h[max(h)] if h else None, "avg24": _mean(h.values()),
                          "interval_s": m["interval_s"]})
        best = None
        for v in (c for c in cells if c["venue"] == "variational"):
            for o in (c for c in cells if c["venue"] != "variational"):
                s = _spread(hs[(v["venue"], v["symbol"])], hs[(o["venue"], o["symbol"])])
                if len(s) < 12:
                    continue
                avg = _mean(s.values())
                if best is None or abs(avg) > abs(best["avg"]):
                    best = {"avg": avg, "apy": abs(avg) * APY_FACTOR, "short_var": avg > 0, "other": o["short"],
                            "hours": len(s)}
        var_ivl = next((c["interval_s"] for c in cells if c["venue"] == "variational"), None)
        rows.append({"asset": asset, "class": max((m["asset_class"] or "") for m in ms) or "unknown",
                     "markets": cells, "best": best, "var_interval_s": var_ivl})
    rows.sort(key=lambda r: (r["best"] is None, -(r["best"]["apy"] if r["best"] else 0), r["asset"]))
    return rows


def _stats(h, now):
    w7 = _window(h, now, 168)
    return {"now": h[max(h)] if h else None, "avg24": _mean(_window(h, now, 24)), "avg7d": _mean(w7),
            "pos_share_7d": (sum(1 for v in w7 if v > 0) / len(w7)) if w7 else None, "hours_7d": len(w7)}


def asset_detail(conn, asset, now=None, days=MAX_DAYS):
    """Hourly series, stats and Variational spreads for one asset over the last `days` days (None if unknown)."""
    ms = _markets(conn, asset)
    if not ms:
        return None
    now = int(now or time.time()) // HOUR * HOUR
    lo = now - int(days * 86400)
    series, hs = [], {}
    for m in ms:
        is_var = m["venue"] == "variational"
        rates = _load(conn, m["venue"], m["symbol"], lo - 9 * HOUR)
        h = hourly(rates, is_var, lo, now)
        key, label, short, slot = describe(m["venue"], m["symbol"])
        hs[(m["venue"], m["symbol"])] = h
        st = _stats(h, now)
        if is_var:
            snap = conn.execute("SELECT interval_rate, interval_s, next_ts FROM var_snapshots WHERE symbol=? "
                                "ORDER BY captured_at DESC LIMIT 1", (m["symbol"],)).fetchone()
            if snap is not None and snap["interval_rate"] is not None and snap["interval_s"]:
                st["live"] = snap["interval_rate"] / (snap["interval_s"] / HOUR)
                st["next_ts"] = snap["next_ts"]
        series.append({"key": key, "venue": m["venue"], "symbol": m["symbol"], "label": label, "short": short,
                       "slot": slot, "interval_s": m["interval_s"], "max_lev": m["max_leverage"], "stats": st,
                       "points": [[t, h[t]] for t in sorted(h)]})
    series.sort(key=lambda s: (s["slot"], s["symbol"]))
    spreads = []
    for v in (s for s in series if s["venue"] == "variational"):
        for o in (s for s in series if s["venue"] != "variational"):
            sp = _spread(hs[(v["venue"], v["symbol"])], hs[(o["venue"], o["symbol"])])
            if not sp:
                continue
            spreads.append({"key": o["key"], "label": f"vs {o['label']}", "short": o["short"], "slot": o["slot"],
                            "var_symbol": v["symbol"], "symbol": o["symbol"], "stats": _stats(sp, now),
                            "points": [[t, sp[t]] for t in sorted(sp)]})
    pairs = [{"pair_id": r["pair_id"], "status": r["status"], "apy_1x": r["apy_1x"], "long": SHORT[r["long_venue"]],
              "short": SHORT[r["short_venue"]]}
             for r in conn.execute("SELECT pair_id, status, apy_1x, long_venue, short_venue FROM signals "
                                   "WHERE asset=? ORDER BY apy_1x IS NULL, apy_1x DESC", (asset,))]
    return {"asset": asset, "class": max((m["asset_class"] or "") for m in ms) or "unknown", "now": now,
            "days": days, "series": series, "spreads": spreads, "pairs": pairs,
            "apr_factor": APR_FACTOR, "apy_factor": APY_FACTOR, "min_apy": MIN_APY_1X}
