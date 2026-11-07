"""Shared HTTP helper with retries and backoff.
Each thread gets its own requests.Session (sessions are not guaranteed thread-safe). `give_up_at` (epoch seconds)
stops retrying before a hard deadline, e.g. just before a Variational settlement, when a late answer is useless."""
import threading
import time

import requests

RETRY_STATUS = {405, 429, 500, 502, 503, 504}  # Lighter may return 405 when rate-limited
_LOCAL = threading.local()


def _session():
    s = getattr(_LOCAL, "session", None)
    if s is None:
        s = _LOCAL.session = requests.Session()
        s.headers.update({"User-Agent": "funding-scanner/0.1 (read-only)"})
    return s


def request_json(method, url, params=None, json_body=None, retries=4, timeout=20, give_up_at=None, max_wait=30):
    last_err = None
    for attempt in range(retries):
        t = timeout
        if give_up_at is not None:
            t = min(timeout, give_up_at - time.time())
            if t <= 0.5:
                break
        wait = min(2 ** attempt, max_wait)
        try:
            r = _session().request(method, url, params=params, json=json_body, timeout=t)
            if r.status_code in RETRY_STATUS:
                try:
                    wait = min(max(wait, float(r.json().get("retry_after_seconds", 0))), max_wait)
                except Exception:
                    pass
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            else:
                r.raise_for_status()
                return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = repr(e)
        if attempt == retries - 1 or (give_up_at is not None and time.time() + wait >= give_up_at - 0.5):
            break
        time.sleep(wait)
    raise RuntimeError(f"{method} {url} failed after {attempt + 1} attempts: {last_err}")
