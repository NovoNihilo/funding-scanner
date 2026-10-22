"""Lighter public REST API (no auth). Unauthenticated limit: 60 requests/min (throttled here).
Funding rows: rate = percent/hour rounded to 4dp; value = USD per contract unit; direction 'long' = longs pay."""
import time

from src.http_client import request_json

BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
_MIN_GAP_S = 1.05
_last_call = [0.0]
_candle_ms = [None]  # learned: does /candles take ms timestamps?


def _get(path, params=None):
    wait = _last_call[0] + _MIN_GAP_S - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    return request_json("GET", f"{BASE}/{path}", params=params)


def order_books():
    data = _get("orderBooks")
    if "order_books" not in data:
        raise KeyError(f"unexpected orderBooks keys: {list(data)}")
    return data["order_books"]


def order_book_details(filter_="perp"):
    data = _get("orderBookDetails", {"filter": filter_})
    if "order_book_details" not in data:
        raise KeyError(f"unexpected orderBookDetails keys: {list(data)}")
    return data["order_book_details"]


def fundings(market_id, start_ts, end_ts, count_back=0, resolution="1h"):
    params = {"market_id": market_id, "resolution": resolution,
              "start_timestamp": int(start_ts), "end_timestamp": int(end_ts),
              "count_back": count_back}
    return _get("fundings", params).get("fundings", [])


def order_book_orders(market_id, limit=250):
    """Return (bids, asks) as [(price, base_size)], best first."""
    data = _get("orderBookOrders", {"market_id": market_id, "limit": limit})

    def side(rows, reverse):
        lv = [(float(o["price"]), float(o["remaining_base_amount"])) for o in rows or []]
        return sorted((x for x in lv if x[1] > 0), key=lambda x: x[0], reverse=reverse)

    return side(data.get("bids"), True), side(data.get("asks"), False)


def candles(market_id, start_ts, end_ts, resolution="1h"):
    count = min(500, int((end_ts - start_ts) / 3600) + 2)

    def call(mult):
        params = {"market_id": market_id, "resolution": resolution,
                  "start_timestamp": int(start_ts * mult), "end_timestamp": int(end_ts * mult),
                  "count_back": count, "merge_mark_price": "true"}
        return _get("candles", params).get("c", []) or []

    if _candle_ms[0] is None:
        rows = call(1)
        if rows:
            _candle_ms[0] = False
            return rows
        rows = call(1000)
        if rows:
            _candle_ms[0] = True
        return rows
    return call(1000 if _candle_ms[0] else 1)


def hourly_closes(market_id, start_ts, end_ts):
    """Map hour-end timestamp (s) -> close of the 1h candle ending at that time."""
    out = {}
    for c in candles(market_id, start_ts, end_ts):
        t = int(c["t"])
        t = t // 1000 if t > 1e11 else t
        if c.get("c"):
            out[t + 3600] = float(c["c"])
    return out
