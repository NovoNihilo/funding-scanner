"""RISEx public REST API (no auth; docs: developer.rise.trade). Limit 500 requests / 10 s per IP (paced here).
All timestamps are nanoseconds. Funding settles hourly: 'funding_rate' is a signed fraction of notional per hour,
positive = longs pay shorts (verified: funding_rate_8h = 8 x current_funding_rate; payment = rate x size)."""
import threading
import time

from src.http_client import request_json

BASE = "https://api.rise.trade/v1"
NS = 10 ** 9
_MIN_GAP_S = 0.05
_last_call = [0.0]
_PACE = threading.Lock()


def _get(path, params=None):
    with _PACE:  # one shared limiter for every thread
        wait = _last_call[0] + _MIN_GAP_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    return request_json("GET", f"{BASE}/{path}", params=params)


def _data(body, key):
    data = (body or {}).get("data")
    if not isinstance(data, dict) or key not in data:
        raise KeyError(f"unexpected RISEx response: data keys {list(data) if isinstance(data, dict) else data!r}")
    return data[key]


def markets():
    """All markets: market_id, config{name, max_leverage, unlocked, ...}, mark_price, category, active, ..."""
    return _data(_get("markets"), "markets")


def funding_history(market_id, start_s, end_s, limit=1000):
    """Settled hourly funding records in [start_s, end_s), every page. Each: funding_rate, start_time, end_time (ns)."""
    out, page = [], 1
    while True:
        body = _get(f"markets/id/{market_id}/funding-rate-history",
                    {"start_time": str(int(start_s) * NS), "end_time": str(int(end_s) * NS),
                     "limit": limit, "page": page})
        data = body.get("data") or {}
        out.extend(data.get("records") or [])
        if not data.get("has_next_page") or page >= 50:
            return out
        page += 1


def order_book(market_id, limit=250):
    """Return (bids, asks) as [(price, base_size)], best first."""
    body = _get("orderbook", {"market_id": str(market_id), "limit": limit})

    def side(rows, reverse):
        lv = [(float(o["price"]), float(o["quantity"])) for o in rows or []]
        return sorted((x for x in lv if x[1] > 0), key=lambda x: x[0], reverse=reverse)

    return side(_data(body, "bids"), True), side(_data(body, "asks"), False)
