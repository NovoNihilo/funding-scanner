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
