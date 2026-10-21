"""Variational Omni read-only stats API. 10 req/10s per IP. No funding history endpoint."""
from src.http_client import request_json

BASE = "https://omni-client-api.prod.ap-northeast-1.variational.io"


def stats():
    return request_json("GET", f"{BASE}/metadata/v2/stats")
