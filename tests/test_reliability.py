"""Offline tests for the reliability upgrade: bounded retries, shared rate limiters, deploy-aware restart counts,
the book refresher's schedule and the hourly cost history."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import src.http_client as http
import src.volume as volume
from src.dashboard import health, restart_counts, serve
from src.db import connect
from src.venues import lighter, variational

H = 3600


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _fake_http(monkeypatch, answers):
    clock, calls = _Clock(), []

    class Session:
        def request(self, method, url, params=None, json=None, timeout=None):
            calls.append(timeout)
            return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(http, "time", clock)
    monkeypatch.setattr(http, "_session", lambda: Session())
    return clock, calls


def test_busy_answers_are_retried_patiently(monkeypatch):
    busy = _Resp(429, {"error": "rate_limited", "retry_after_seconds": 1})
    clock, calls = _fake_http(monkeypatch, [busy, busy, busy, _Resp(200, {"ok": 1})])
    assert http.request_json("GET", "u", retries=8, max_wait=8) == {"ok": 1}
    assert len(calls) == 4 and clock.sleeps == [1, 2, 4]


def test_retries_stop_before_give_up_time(monkeypatch):
    busy = _Resp(429, {"retry_after_seconds": 1})
    clock, calls = _fake_http(monkeypatch, [busy])
    with pytest.raises(RuntimeError, match="failed after"):
        http.request_json("GET", "u", retries=8, give_up_at=1010, max_wait=8)
    assert clock.t < 1010 and len(calls) == 4                           # waits 1, 2, 4; an 8s wait would overrun
    assert all(t <= 10 for t in calls)                                  # request timeouts shrink to fit too


def test_variational_snapshots_retry_until_the_settlement(monkeypatch):
    seen = {}
    monkeypatch.setattr(variational, "request_json", lambda *a, **k: seen.update(k) or {"listings": []})
    variational.stats(give_up_at=5000)
    assert seen["give_up_at"] == 5000 and seen["retries"] == 8


def test_lighter_limiter_is_shared_between_threads(monkeypatch):
    stamps = []
    monkeypatch.setattr(lighter, "request_json", lambda *a, **k: stamps.append(time.time()) or {})
    monkeypatch.setattr(lighter, "_MIN_GAP_S", 0.05)
    threads = [threading.Thread(target=lambda: [lighter._get("x") for _ in range(3)]) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    assert len(stamps) == 6 and min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.045


def _run(conn, job, t, detail="{}"):
    conn.execute("INSERT INTO runs (job, started_at, finished_at, ok, detail) VALUES (?,?,?,1,?)", (job, t, t, detail))


def test_deploys_are_not_counted_as_crashes():
    conn = connect(":memory:")
    now = 100 * H
    _run(conn, "worker_start", now - 50 * H)                            # first start ever: not a restart
    _run(conn, "worker_stop", now - 5 * H)
    _run(conn, "worker_start", now - 5 * H)                             # deploy
    _run(conn, "worker_start", now - 2 * H)                             # crash: no clean stop before it
    _run(conn, "worker_stop", now - H)
    _run(conn, "worker_start", now - H)                                 # deploy
    assert restart_counts(conn, now) == (2, 1)
    h = health(conn, now, db_path=":memory:")
    assert ("INFO", "worker restarted unexpectedly 1 time(s) in 24h (deploys don't count)") in h["issues"]


def test_stale_book_refresher_is_flagged():
    conn = connect(":memory:")
    now = 100 * H
    _run(conn, "books", now - 2 * H, json.dumps({"markets": 400}))
    text = " ".join(t for _, t in health(conn, now, db_path=":memory:")["issues"])
    assert "order books not refreshed for 2h" in text


@pytest.fixture
def shared():
    conn = connect(":memory:")
    for v, s in (("variational", "AAA-PERP"), ("hyperliquid", "AAA"), ("lighter", "AAA")):
        conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched, "
                     "active, updated_at) VALUES (?,?,'AAA',?,3600,'crypto','',1,1,0)", (v, s, s))
    return conn


def _book(conn, venue, symbol, t, rt):
    for size in (1000, 100000):
        conn.execute("INSERT OR REPLACE INTO book_costs (venue, symbol, size_usd, rt_cost, mid, executable, flags, "
                     "captured_at, rt_cost_maker) VALUES (?,?,?,?,100,1,'',?,?)", (venue, symbol, size, rt * size / 1000,
                                                                                   t, rt))


def test_lighter_books_refresh_on_their_own_slower_schedule(shared):
    now = 100 * H
    for v, s in (("variational", "AAA-PERP"), ("hyperliquid", "AAA"), ("lighter", "AAA")):
        _book(shared, v, s, now - 900, 5e-4)                            # all refreshed 15 minutes ago
    todo = volume.sweep_markets(shared, now - 540, {"lighter": now - 1140})
    assert [x[0] for x in todo] == ["variational", "hyperliquid"]       # Lighter waits for its 20-minute turn


def test_cost_history_keeps_one_row_per_market_hour_and_prunes(shared, monkeypatch):
    now = 1000 * H + 1800
    _book(shared, "hyperliquid", "AAA", now - 60, 5e-4)
    _book(shared, "lighter", "AAA", now - 60, 3e-4)
    shared.execute("INSERT INTO cost_history VALUES ('lighter','AAA',?,'{}')", (now - 40 * 86400,))
    st = volume.record_history(shared, now)
    assert st == {"history_rows": 2, "history_pruned": 1}
    _book(shared, "lighter", "AAA", now, 4e-4)                          # a later refresh in the same hour wins
    volume.record_history(shared, now + 60)
    rows = shared.execute("SELECT hour, costs FROM cost_history WHERE venue='lighter'").fetchall()
    assert len(rows) == 1 and rows[0]["hour"] == 1000 * H and json.loads(rows[0]["costs"])["100000"] == 0.04
    d = volume.history(shared, "AAA", 1000, now=now + 60)
    lt = next(s for s in d["series"] if s["venue"] == "lighter")
    assert lt["points"] == [[1000 * H, 0.0004]] and lt["slot"] == 3 and volume.history(shared, "ZZZ", 1000) is None


def test_history_api(tmp_path):
    path = str(tmp_path / "h.db")
    conn = connect(path)
    for v, s in (("variational", "AAA-PERP"), ("hyperliquid", "AAA")):
        conn.execute("INSERT INTO markets (venue, symbol, asset, market_id, interval_s, asset_class, flags, matched, "
                     "active, updated_at) VALUES (?,?,'AAA',?,3600,'crypto','',1,1,0)", (v, s, s))
    hour = int(time.time()) // H * H
    conn.execute("INSERT INTO cost_history VALUES ('hyperliquid','AAA',?,?)", (hour, json.dumps({"5000": 0.0012})))
    conn.commit()
    stop = threading.Event()
    t = threading.Thread(target=serve, args=("127.0.0.1", 8795, stop, path), daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8795/api/volume/history?a=AAA&size=5000",
                                              timeout=5).read())
        hl = next(s for s in d["series"] if s["venue"] == "hyperliquid")
        assert d["size"] == 5000 and hl["points"] == [[hour, 0.0012]]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:8795/api/volume/history?a=NOPE&size=5000", timeout=5)
    finally:
        stop.set()
        t.join(timeout=5)
