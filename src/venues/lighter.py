"""Lighter public REST API (no auth). Unauthenticated limit: 60 requests/min."""
from src.http_client import request_json

BASE = "https://mainnet.zklighter.elliot.ai/api/v1"


def order_books():
    data = request_json("GET", f"{BASE}/orderBooks")
    if "order_books" not in data:
        raise KeyError(f"unexpected orderBooks keys: {list(data)}")
    return data["order_books"]


def fundings(market_id, start_ts, end_ts, count_back=0, resolution="1h"):
    params = {"market_id": market_id, "resolution": resolution,
              "start_timestamp": int(start_ts), "end_timestamp": int(end_ts),
              "count_back": count_back}
    return request_json("GET", f"{BASE}/fundings", params=params).get("fundings", [])
