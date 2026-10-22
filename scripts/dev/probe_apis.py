"""Milestone 1: verify each venue's public API and inspect funding formats."""
import json
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.venues import hyperliquid as hl, lighter as lt, variational as va

OUT = Path("data/probe")
OUT.mkdir(parents=True, exist_ok=True)
NOW = time.time()


def utc(ts_s):
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def spacing(ts_list_s):
    ts = sorted(ts_list_s)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    if not gaps:
        return "n/a"
    return f"median gap {statistics.median(gaps):.0f}s, min {min(gaps):.0f}s, max {max(gaps):.0f}s"


def save(name, obj):
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))


def probe_variational():
    print("\n=== VARIATIONAL ===")
    s = va.stats()
    listings = s["listings"]
    perps = [x for x in listings if x.get("contract_type") == "perpetual"]
    print(f"listings={len(listings)} types={dict(Counter(x.get('contract_type') for x in listings))}")
    intervals = Counter((x["funding"] or {}).get("interval_seconds") for x in perps)
    print(f"perp intervals (s): {dict(intervals)}  perps w/o funding: {sum(1 for x in perps if x['funding'] is None)}")
    btc = next(x for x in perps if x["ticker"] == "BTC-PERP")
    f = btc["funding"]
    ivl = f["interval_seconds"]
    print(f"BTC-PERP last={f['last']} next={f['next']} interval={ivl}s")
    print(f"BTC next_timestamp={utc(f['next_timestamp'])} UTC ({f['next_timestamp'] - NOW:.0f}s from now)")
    hourly = float(f["last"]["interval_rate"]) / (ivl / 3600)
    q = btc.get("quotes", {})
    print(f"BTC quotes updated_at={q.get('updated_at')} size_1k={q.get('size_1k')}")
    save("variational_stats", s)
    return {x["ticker"].rsplit("-", 1)[0] for x in perps}, hourly


def probe_hyperliquid():
    print("\n=== HYPERLIQUID ===")
    universe, ctxs = hl.meta_and_asset_ctxs()
    live = [u["name"] for u in universe if not u.get("isDelisted")]
    print(f"main-dex perps={len(universe)} live={len(live)}")
    dexs = hl.perp_dexs()
    print(f"HIP-3 builder dexs ({len(dexs)}): {[d.get('name') for d in dexs][:15]}")
    rows = hl.funding_history("BTC", (NOW - 48 * 3600) * 1000)
    print(f"BTC fundingHistory 48h rows={len(rows)} {spacing([r['time'] / 1000 for r in rows])}")
    for r in rows[-3:]:
        print(f"  {utc(r['time'] / 1000)} fundingRate={r['fundingRate']} premium={r['premium']}")
    ctx = ctxs[[u["name"] for u in universe].index("BTC")]
    print(f"BTC current ctx funding={ctx.get('funding')} (predicted, not settled)")
    save("hl_btc_funding", rows)
    save("hl_universe", universe)
    return set(live), (rows[-1]["fundingRate"] if rows else None)


def probe_lighter():
    print("\n=== LIGHTER ===")
    books = lt.order_books()
    print(f"order_books={len(books)} keys={sorted(books[0].keys())}")
    print(f"market_type counts: {dict(Counter(b.get('market_type', '?') for b in books))}")
    print(f"status counts: {dict(Counter(b.get('status', '?') for b in books))}")
    btc = next(b for b in books if b["symbol"] == "BTC")
    print(f"BTC market_id={btc['market_id']} taker_fee={btc.get('taker_fee')} maker_fee={btc.get('maker_fee')}")
    rows, used = [], None
    for used, mult in (("seconds", 1), ("milliseconds", 1000)):
        try:
            rows = lt.fundings(btc["market_id"], (NOW - 48 * 3600) * mult, NOW * mult)
        except Exception as e:
            print(f"  fundings with {used}: error {e}")
            continue
        print(f"  fundings with {used} timestamps: rows={len(rows)}")
        if rows:
            break
    if rows:
        ts = [float(r["timestamp"]) for r in rows]
        ts = [t / 1000 if t > 1e11 else t for t in ts]
        print(f"BTC fundings 48h rows={len(rows)} {spacing(ts)} (input unit: {used})")
        for r in rows[-3:]:
            print(f"  raw={r}")
    save("lighter_order_books", books)
    save("lighter_btc_fundings", rows)
    perps = {b["symbol"] for b in books if b.get("market_type", "perp") == "perp"}
    return perps, (rows[-1] if rows else None)


def main():
    res, ok = {}, True
    for name, fn in (("variational", probe_variational), ("hyperliquid", probe_hyperliquid),
                     ("lighter", probe_lighter)):
        try:
            res[name] = fn()
        except Exception as e:
            ok = False
            print(f"\n!!! {name} FAILED: {e!r}")
    if len(res) == 3:
        (va_set, va_h), (hl_set, hl_last), (lt_set, lt_last) = res["variational"], res["hyperliquid"], res["lighter"]
        print("\n=== BTC UNIT CROSS-CHECK (hourly-equivalent) ===")
        print(f"Variational current (estimate): {va_h:.8f}")
        print(f"Hyperliquid last settled:       {hl_last}")
        print(f"Lighter last settled raw:       {lt_last}")
        print("\n=== NAIVE SYMBOL OVERLAP (exact names, pre-normalization) ===")
        print(f"VAR&HL={len(va_set & hl_set)} VAR&LT={len(va_set & lt_set)} "
              f"HL&LT={len(hl_set & lt_set)} all3={len(va_set & hl_set & lt_set)}")
    print("\nPROBE OK" if ok else "\nPROBE HAD FAILURES")


if __name__ == "__main__":
    main()
