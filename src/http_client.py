"""Shared HTTP helper with retries and backoff."""
import time
import requests

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "funding-scanner/0.1 (read-only)"})
RETRY_STATUS = {405, 429, 500, 502, 503, 504}  # Lighter may return 405 when rate-limited


def request_json(method, url, params=None, json_body=None, retries=4, timeout=20):
    last_err = None
    for attempt in range(retries):
        try:
            r = _SESSION.request(method, url, params=params, json=json_body, timeout=timeout)
            if r.status_code in RETRY_STATUS:
                wait = 2 ** attempt
                try:
                    wait = max(wait, float(r.json().get("retry_after_seconds", 0)))
                except Exception:
                    pass
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = repr(e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{method} {url} failed after {retries} attempts: {last_err}")
