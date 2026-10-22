"""Offline tests for the dashboard: settlement coverage, health status, page rendering and the local server."""
import threading
import time
import urllib.request

import pytest

import src.signals as signals
from src.dashboard import build_page, health, serve, var_coverage
from src.db import connect
from src.signals import evaluate_all
from tests.test_signals import _seed

H = 3600


def _snap(conn, sym, captured, next_ts):
    conn.execute("INSERT INTO var_snapshots (symbol, captured_at, next_ts, interval_s, interval_rate) "
                 "VALUES (?, ?, ?, 14400, 1e-5)", (sym, captured, next_ts))


def _var(conn, sym, ts, source="var_pre"):
    conn.execute("INSERT INTO settlements VALUES ('variational', ?, ?, 14400, 1e-5, NULL, 'ESTIMATED', ?, NULL, 0)",
                 (sym, ts, source))


def test_var_coverage_counts_captured_late_and_missing():
    conn = connect(":memory:")
    now = 100 * H
    for sym in ("A-PERP", "B-PERP"):
        _snap(conn, sym, now - 10 * H, now - 8 * H)   # announced settlement at now-8h
        _snap(conn, sym, now - 6 * H, now - 4 * H)    # announced settlement at now-4h
    _snap(conn, "A-PERP", now - 60, now + 4 * H)      # future settlement: not expected yet
    _var(conn, "A-PERP", now - 8 * H)
    _var(conn, "A-PERP", now - 4 * H, "var_pre_late")
    _var(conn, "B-PERP", now - 8 * H)
    cov = var_coverage(conn, now, 24)
    assert (cov["expected"], cov["captured"], cov["late"]) == (4, 3, 1)
    assert cov["missing"] == [("B-PERP", now - 4 * H)]


def test_health_flags_stale_snapshots_and_missed_settlements():
    conn = connect(":memory:")
    now = 100 * H
    _snap(conn, "A-PERP", now - 2 * H, now - H)  # last snapshot 2h old, its settlement never recorded
    conn.execute("INSERT INTO runs (job, started_at, finished_at, ok, detail) VALUES ('sync', ?, ?, 1, '{}')",
                 (now - 600, now - 300))
    h = health(conn, now, db_path=":memory:")
    assert h["status"] == "FAIL"
    text = " ".join(t for _, t in h["issues"])
    assert "snapshots are stale" in text and "missed" in text


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "")
    conn = connect(":memory:")
    now = 1000 * H
    _seed(conn, now, short_rate=3e-5, long_rate=0.0)
    evaluate_all(conn, now)
    _snap(conn, "AAA-PERP", now - 60, now + H)
    conn.execute("INSERT INTO runs (job, started_at, finished_at, ok, detail) VALUES ('sync', ?, ?, 1, ?)",
                 (now - 600, now - 300, '{"hl": {"gap_repairs": 2}, "lt": {"gap_repairs": 1}}'))
    conn.execute("INSERT INTO runs (job, started_at, finished_at, ok) VALUES ('worker_start', ?, ?, 1)",
                 (now - 5 * H, now - 5 * H))
    return conn, now


def test_page_renders_signals_health_and_events(seeded):
    conn, now = seeded
    conn.execute("INSERT INTO signal_events (pair_id, ts, event, from_status, to_status, detail) VALUES "
                 "('AAA|hyperliquid:AAA>lighter:AAA', ?, 'STATUS', 'EMERGING', 'QUALIFIED', '')", (now,))
    conn.execute("INSERT INTO signal_events (pair_id, ts, event, from_status, to_status, detail) VALUES "
                 "('ZZZ|hyperliquid:ZZZ>lighter:ZZZ', ?, 'STATUS', 'MONITORING', 'QUALIFIED', '')", (now,))
    page = build_page(conn, now=now, db_path=":memory:")
    assert "EMERGING &rarr;" in page and "ZZZ" not in page  # real change shown; dropped pair hidden
    assert page.startswith("<!doctype html>") and "Funding scanner" in page
    assert "<b>AAA</b>" in page and "QUALIFIED" in page and "Recent signal changes" in page
    assert 'class="chip st-ok">OK' in page  # fresh snapshot, recent sync, nothing missed
    assert "Gap repairs (last sync)</span><b>3</b>" in page
    assert "TOS_RISK" not in page.split("<section")[2]  # not repeated on rows


def test_server_serves_page_and_404(seeded, tmp_path):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    _seed(conn, 1000 * H, short_rate=3e-5, long_rate=0.0)
    conn.commit()
    stop = threading.Event()
    t = threading.Thread(target=serve, args=("127.0.0.1", 8799, stop, db), daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        body = urllib.request.urlopen("http://127.0.0.1:8799/", timeout=5).read().decode()
        assert "Funding scanner" in body
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:8799/nope", timeout=5)
    finally:
        stop.set()
        t.join(timeout=5)
