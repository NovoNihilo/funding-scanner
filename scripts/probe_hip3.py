"""Probe Hyperliquid HIP-3 builder dexes: market names, funding history, order book and candles for one market."""
import time

from src.venues import hyperliquid as hl

NOW = time.time()
dexs = hl.perp_dexs()
print(f"HIP-3 dexs ({len(dexs)}); fields of first: {sorted(dexs[0].keys()) if dexs else []}")
sample = None
for d in dexs:
    try:
        uni, ctx = hl.meta_and_asset_ctxs(d["name"])
    except Exception as e:
        print(f"  {d['name']:<6} ERROR {e!r}"[:120])
        continue
    live = [u["name"] for u in uni if not u.get("isDelisted")]
    print(f"  {d['name']:<6} live={len(live):<4} e.g. {live[:6]}")
    if sample is None and live:
        sample = live[0]
print(f"\nsample market: {sample}")
rows = hl.funding_history(sample, (NOW - 6 * 3600) * 1000)
print(f"fundingHistory rows (6h): {len(rows)} last: {rows[-1] if rows else None}")
bids, asks = hl.l2_book(sample)
print(f"l2Book levels: bids={len(bids)} asks={len(asks)} best: {bids[:1]} {asks[:1]}")
closes = hl.hourly_closes(sample, NOW - 48 * 3600, NOW)
print(f"candles (48h): {len(closes)}")
print("HIP3 PROBE OK" if rows and bids and asks and closes else "HIP3 PROBE INCOMPLETE")
