"""One-off probe of the RISEx public API (developer.rise.trade): prints real response shapes so the venue client
is built on actual field names and units. Read-only. Usage: python -m scripts.dev.probe_risex"""
import json
import time

import requests

BASE = "https://api.rise.trade"
S = requests.Session()
S.headers.update({"User-Agent": "funding-scanner/0.1 (read-only)"})


def get(path, params=None):
    r = S.get(BASE + path, params=params, timeout=20)
    hdr = {k: v for k, v in r.headers.items() if "limit" in k.lower() or "retry" in k.lower()}
    print(f"\n===== GET {path} {params or ''} -> HTTP {r.status_code} {hdr or ''}")
    try:
        return r.json()
    except ValueError:
        print(r.text[:400])
        return None


def dump(obj, n=3000):
    print(json.dumps(obj, indent=1)[:n])


def rows_of(body):
    """Find the first list of dicts in the response, and say where it was."""
    if isinstance(body, list):
        return body, "(top level)"
    if isinstance(body, dict):
        for k, v in body.items():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v, k
            if isinstance(v, dict):
                inner, where = rows_of(v)
                if inner is not None:
                    return inner, f"{k}.{where}"
    return None, None


def skeleton(body):
    """The response with every list cut to its first element, so wrappers and paging fields are visible."""
    if isinstance(body, list):
        return [skeleton(body[0])] if body else []
    if isinstance(body, dict):
        return {k: skeleton(v) for k, v in body.items()}
    return body


markets = get("/v1/markets")
rows, where = rows_of(markets)
print("markets list at:", where, " count:", len(rows) if rows else None)
if rows:
    cats = {}
    for m in rows:
        cats.setdefault(str(m.get("category")), []).append(str(m.get("symbol") or m.get("name") or m.get("market_id")))
    print("categories:", {k: len(v) for k, v in cats.items()})
    print("crypto:", cats.get("crypto"))
    btc = next((m for m in rows if "BTC" in json.dumps(m)[:300]), rows[0])
    print("\n--- BTC market, complete ---")
    dump(btc, 6000)
    print("\n--- wrapper (lists cut to 1 element) ---")
    dump({k: v for k, v in skeleton(markets).items()} if isinstance(markets, dict) else "list", 800)

now_ns = int(time.time() * 1e9)
hist = get("/v1/markets/id/1/funding-rate-history", {"start_time": str(now_ns - 6 * 3600 * 10**9),
                                                     "end_time": str(now_ns), "limit": 3, "page": 1})
print("--- funding history, 6h window, limit 3 (lists cut to 1 element) ---")
dump(skeleton(hist), 2500)
hrows, hwhere = rows_of(hist)
if hrows:
    print("history rows at:", hwhere, " returned:", len(hrows))
    for r in hrows:
        dump(r, 800)
week = get("/v1/markets/id/1/funding-rate-history", {"limit": 1000, "page": 1})
wrows, _ = rows_of(week)
print("default window (7d), limit 1000 -> rows:", len(wrows) if wrows else None)
if wrows:
    print("first:", json.dumps(wrows[0])[:400])
    print("last: ", json.dumps(wrows[-1])[:400])

book = get("/v1/orderbook", {"market_id": "1", "limit": 3})
print("--- order book BTC, 3 levels ---")
dump(book, 2500)
