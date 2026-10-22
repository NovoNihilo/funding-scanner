"""Hyperliquid public info API (no auth). Funding settles hourly."""
from src.http_client import request_json

INFO_URL = "https://api.hyperliquid.xyz/info"


def _post(body):
    return request_json("POST", INFO_URL, json_body=body)


def meta_and_asset_ctxs(dex=""):
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    meta, ctxs = _post(body)
    return meta["universe"], ctxs


def perp_dexs():
    return [d for d in _post({"type": "perpDexs"}) if d]


def funding_history(coin, start_ms, end_ms=None):
    body = {"type": "fundingHistory", "coin": coin, "startTime": int(start_ms)}
    if end_ms is not None:
        body["endTime"] = int(end_ms)
    return _post(body)


def l2_book(coin):
    """Return (bids, asks) as [(price, size_in_contract_units)], best first."""
    data = _post({"type": "l2Book", "coin": coin})
    bids, asks = data["levels"]
    return ([(float(x["px"]), float(x["sz"])) for x in bids], [(float(x["px"]), float(x["sz"])) for x in asks])


def hourly_closes(coin, start_s, end_s):
    """Map hour-end timestamp (s) -> close of the 1h candle ending then."""
    rows = _post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                     "startTime": int(start_s * 1000), "endTime": int(end_s * 1000)}})
    return {int(r["t"]) // 1000 + 3600: float(r["c"]) for r in rows}
