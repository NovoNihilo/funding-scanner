"""Cheapest delta-neutral volume: for every asset shared between Variational and another venue, what it costs to
open and close a hedged position, whether or not funding pays.

A round trip = open long on one venue + short on the other, then close both. Its cost is the two markets' own
round-trip costs (spread + slippage + fees, from book_costs). Entry basis is NOT counted: a quick round trip
closes at roughly the same cross-venue basis it opened at. Cost is quoted per $10k of Variational volume
(each round trip trades 2 x size on Variational) and per $10k of total volume across both venues (4 x size).
Two execution styles, each with its own cheapest hedge (the cheapest venue can differ: maker fees vary far more
than spreads): taker = both legs cross the spread (spread + slippage + taker fees); maker = the hedge leg rests limit
orders at the touch and pays only maker fees if filled. Variational is RFQ-only, so its leg is always taker.
Funding is shown alongside, for the better direction, as what holding the hedge for a day would earn or pay.
History: the last refresh of every hour is kept per market (cost_history, COST_HISTORY_KEEP_DAYS) for charts."""
import json
import time

from src.config import COST_HISTORY_KEEP_DAYS, REQUIRED_VENUE, VOLUME_MAX_AGE_S, VOLUME_SIZES
from src.markets import SHORT, describe

HOUR = 3600
PER = 10_000  # results per $10k of volume
SLOT_ORDER = {"variational": 0, "hyperliquid": 1, "lighter": 2, "risex": 3}


def _matched(conn):
    return conn.execute("SELECT venue, symbol, asset, asset_class, market_id, max_leverage FROM markets "
                        "WHERE matched=1 AND active=1 ORDER BY asset, venue, symbol").fetchall()


def _shared(conn):
    """{asset: [market rows]} for assets tradable as a cross-venue pair (with a REQUIRED_VENUE leg if set)."""
    by_asset = {}
    for m in _matched(conn):
        by_asset.setdefault(m["asset"], []).append(m)
    out = {}
    for asset, ms in by_asset.items():
        venues = {m["venue"] for m in ms}
        if len(venues) < 2 or (REQUIRED_VENUE and REQUIRED_VENUE not in venues):
            continue
        out[asset] = ms
    return out


def sweep_markets(conn, fresh_since, venue_fresh=None):
    """Markets of shared assets whose order-book costs were not refreshed since `fresh_since` (or, per venue,
    venue_fresh[venue]), oldest first (Variational first: its costs come from stored quotes, no request needed)."""
    venue_fresh = venue_fresh or {}
    last = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT venue, symbol, MIN(captured_at) FROM book_costs GROUP BY venue, symbol")}  # any stale size
    todo = [(m["venue"], m["symbol"], m["market_id"]) for ms in _shared(conn).values() for m in ms
            if (last.get((m["venue"], m["symbol"])) or 0) < venue_fresh.get(m["venue"], fresh_since)]
    return sorted(todo, key=lambda x: (x[0] != "variational", last.get((x[0], x[1])) or 0, x[0], x[1]))


def _costs(conn, now):
    out = {}
    for r in conn.execute("SELECT * FROM book_costs WHERE captured_at >= ?", (now - VOLUME_MAX_AGE_S,)):
        out[(r["venue"], r["symbol"], r["size_usd"])] = r
    return out


def _funding(conn):
    """{pair_id: current hourly net (24h average)} from the signal engine."""
    out = {}
    for r in conn.execute("SELECT pair_id, metrics FROM signals"):
        out[r["pair_id"]] = json.loads(r["metrics"] or "{}").get("current_hourly")
    return out


def _flags(row):
    return [f for f in (row["flags"] or "").split(",") if f]


def board(conn, size, now=None):
    """One row per shared asset at `size` USD per leg, cheapest round trip first. Rows without a complete
    hedge (missing or stale books on every other venue) sort last."""
    now = int(now or time.time())
    costs, funding = _costs(conn, now), _funding(conn)
    rows = []
    for asset, ms in _shared(conn).items():
        anchors = [m for m in ms if m["venue"] == (REQUIRED_VENUE or m["venue"])]
        venues = {}
        for m in ms:
            c = costs.get((m["venue"], m["symbol"], float(size)))
            key = describe(m["venue"], m["symbol"])[0]
            venues.setdefault(m["venue"], []).append({
                "symbol": m["symbol"], "key": key, "short": describe(m["venue"], m["symbol"])[2],
                "rt": c["rt_cost"] if c is not None else None,
                "maker": c["rt_cost_maker"] if c is not None else None,
                "age": now - c["captured_at"] if c is not None else None,
                "flags": _flags(c) if c is not None else ["NO_DATA"]})
        cands = []
        for a in anchors:
            ac = next(x for x in venues[a["venue"]] if x["symbol"] == a["symbol"])
            if ac["rt"] is None:
                continue
            for o in ms:
                if o["venue"] == a["venue"]:
                    continue
                oc = next(x for x in venues[o["venue"]] if x["symbol"] == o["symbol"])
                if oc["rt"] is None:
                    continue
                taker = ac["rt"] + oc["rt"]
                maker = ac["rt"] + (oc["maker"] if oc["maker"] is not None else oc["rt"])
                pa = f"{asset}|{a['venue']}:{a['symbol']}>{o['venue']}:{o['symbol']}"
                pb = f"{asset}|{o['venue']}:{o['symbol']}>{a['venue']}:{a['symbol']}"
                fa, fb = funding.get(pa), funding.get(pb)  # net per hour: long anchor / long other
                if fa is None and fb is not None:
                    fa = -fb
                if fb is None and fa is not None:
                    fb = -fa
                fdir = None
                if fa is not None:
                    long_a = fa >= fb  # hold the hedge in whichever direction funding favours
                    h = fa if long_a else fb
                    fdir = {"hourly": h, "daily_usd": h * 24 * size,
                            "long": SHORT.get(a["venue"] if long_a else o["venue"]),
                            "short": SHORT.get(o["venue"] if long_a else a["venue"]),
                            "pair_id": pa if long_a else pb}
                cands.append({"anchor": a["venue"], "anchor_symbol": a["symbol"], "venue": o["venue"],
                              "symbol": o["symbol"], "short": oc["short"], "taker": taker, "maker": maker,
                              "per10k_var": taker / 2 * PER, "per10k_var_maker": maker / 2 * PER,
                              "per10k_all": taker / 4 * PER, "per10k_all_maker": maker / 4 * PER,
                              "usd": taker * size, "usd_maker": maker * size, "funding": fdir,
                              "var_flags": ac["flags"], "hedge_flags": oc["flags"]})
        best = min(cands, key=lambda c: (c["taker"], c["maker"])) if cands else None
        best_maker = min(cands, key=lambda c: (c["maker"], c["taker"])) if cands else None
        lev = [m["max_leverage"] for m in ms if m["max_leverage"]]
        rows.append({"asset": asset, "class": max((m["asset_class"] or "") for m in ms) or "unknown",
                     "venues": venues, "best": best, "best_maker": best_maker, "max_lev": min(lev) if lev else None})
    rows.sort(key=lambda r: (r["best"] is None, r["best"]["taker"] if r["best"] else 0, r["asset"]))
    return rows


def _r(x):
    return None if x is None else float(f"{x:.6g}")


def payload(conn, now=None):
    """Everything the Volume tab shows, for every VOLUME_SIZES entry, merged per asset (sorted by the smallest
    size's cheapest round trip): venues [{venue, symbol, short, rt: {size: cost}, maker, flags, age}],
    best {size: cheapest hedge, taker}, best_maker {size: cheapest hedge with a maker hedge leg},
    plus the last sweep's stats."""
    now = int(now or time.time())
    sizes = [int(s) for s in VOLUME_SIZES]
    assets, order = {}, []
    for size in sizes:
        for row in board(conn, size, now):
            a = assets.get(row["asset"])
            if a is None:
                a = assets[row["asset"]] = {"asset": row["asset"], "class": row["class"], "max_lev": row["max_lev"],
                                            "venues": {}, "best": {}, "best_maker": {}}
                order.append(row["asset"])
            for venue, lst in row["venues"].items():
                for x in lst:
                    v = a["venues"].setdefault((venue, x["symbol"]), {
                        "venue": venue, "symbol": x["symbol"], "short": x["short"], "rt": {}, "maker": {},
                        "flags": {}, "age": x["age"]})
                    v["rt"][size], v["maker"][size], v["flags"][size] = _r(x["rt"]), _r(x["maker"]), x["flags"]
            for style in ("best", "best_maker"):
                b = row[style]
                if b is not None:
                    a[style][size] = {k: (_r(v) if isinstance(v, float) else v) for k, v in b.items()}
                    if b["funding"]:
                        a[style][size]["funding"] = {k: (_r(v) if isinstance(v, float) else v)
                                                     for k, v in b["funding"].items()}
    out = []
    for name in order:
        a = assets[name]
        a["venues"] = sorted(a["venues"].values(), key=lambda v: (SLOT_ORDER.get(v["venue"], 9), v["symbol"]))
        out.append(a)
    sweep = conn.execute("SELECT started_at, finished_at, detail FROM runs WHERE job='books' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    return {"now": now, "sizes": sizes, "assets": out,
            "sweep": None if sweep is None else {"finished_at": sweep["finished_at"],
                                                 **json.loads(sweep["detail"] or "{}")}}


def record_history(conn, now=None):
    """Copy every shared market's current costs into this hour's cost_history row (the hour's last refresh wins),
    then prune rows older than COST_HISTORY_KEEP_DAYS. About 660 small rows per hour (~1 MB a day)."""
    now = int(now or time.time())
    hour = now // HOUR * HOUR
    shared = {(m["venue"], m["symbol"]) for ms in _shared(conn).values() for m in ms}
    rows = {}
    for r in conn.execute("SELECT venue, symbol, size_usd, rt_cost FROM book_costs WHERE captured_at >= ?",
                          (now - VOLUME_MAX_AGE_S,)):
        if (r[0], r[1]) in shared and r[3] is not None:
            rows.setdefault((r[0], r[1]), {})[str(int(r[2]))] = float(f"{r[3]:.4g}")
    with conn:
        conn.executemany("INSERT OR REPLACE INTO cost_history VALUES (?,?,?,?)",
                         [(v, s, hour, json.dumps(c, separators=(",", ":"))) for (v, s), c in rows.items()])
        pruned = conn.execute("DELETE FROM cost_history WHERE hour < ?",
                              (now - int(COST_HISTORY_KEEP_DAYS * 86400),)).rowcount
    return {"history_rows": len(rows), "history_pruned": pruned}


def history(conn, asset, size, days=14, now=None):
    """Hourly round-trip cost of each of an asset's shared markets at `size`, for the Volume tab chart."""
    now = int(now or time.time())
    ms = _shared(conn).get(asset)
    if not ms:
        return None
    key, series = str(int(size)), []
    for m in sorted(ms, key=lambda m: (SLOT_ORDER.get(m["venue"], 9), m["symbol"])):
        k, label, short, slot = describe(m["venue"], m["symbol"])
        pts = []
        for hour, costs in conn.execute("SELECT hour, costs FROM cost_history WHERE venue=? AND symbol=? AND hour>? "
                                        "ORDER BY hour", (m["venue"], m["symbol"], now - days * 86400)):
            v = json.loads(costs).get(key)
            if v is not None:
                pts.append([hour, v])
        series.append({"key": k, "venue": m["venue"], "symbol": m["symbol"], "label": label, "short": short,
                       "slot": slot, "points": pts})
    return {"asset": asset, "size": int(size), "now": now // HOUR * HOUR, "days": days, "series": series}
