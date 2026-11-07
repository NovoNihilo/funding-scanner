"""Variational Omni read-only stats API. 10 req/10s per IP. No funding history endpoint."""
from src.http_client import request_json

BASE = "https://omni-client-api.prod.ap-northeast-1.variational.io"


def stats(give_up_at=None):
    """All listings with funding and quotes. Retries patiently (the venue sometimes answers 429 'Service is busy'
    for everyone), but never past give_up_at: a snapshot taken after a settlement would carry the next rate."""
    return request_json("GET", f"{BASE}/metadata/v2/stats", retries=8, give_up_at=give_up_at, max_wait=8)
