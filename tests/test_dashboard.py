"""Offline tests for the dashboard: settlement coverage, health, page rendering, pair detail API and the server."""
import html
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import src.signals as signals
from src.dashboard import build_page, flag_notes, health, humanize, pair_detail, serve, sparkline, var_coverage
from src.db import connect
from src.signals import evaluate_all
from tests.test_signals import _seed, _var_pair

H = 3600
VPAIR = "AAA|hyperliquid:AAA>variational:AAA-PERP"


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


def test_reasons_and_flags_in_plain_language():
    assert humanize("APY 1x 4.1% < 5%") == "APY 4.1% is under the 5% minimum"
    assert humanize("lighter REDUCE_ONLY") == "Lighter market is reduce-only: new positions can't be opened"
    assert humanize("something new") == "something new"
    notes = {n["code"]: n["text"] for n in flag_notes(["TOS_RISK", "DIVIDEND_UNKNOWN", "ANOMALY_EXCLUDED=2",
                                                       "LIG_REDUCE_ONLY", "NEW_FLAG"])}
    assert "TOS_RISK" not in notes and "dividend" in notes["DIVIDEND_UNKNOWN"]
    assert notes["ANOMALY_EXCLUDED=2"].startswith("2 extreme") and "reduce-only" in notes["LIG_REDUCE_ONLY"]
    assert notes["NEW_FLAG"] == "NEW_FLAG"


def test_sparkline_marks_missing_and_polarity():
    pts = [{"net": 1e-5, "ex": None}, {"net": None, "ex": None}, {"net": -5e-6, "ex": None}]
    svg = sparkline(pts)
    assert 'class="sk-pos"' in svg and 'class="sk-neg"' in svg and 'class="sk-miss"' in svg
    assert "profitable in 1" in svg


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setattr(signals, "REQUIRED_VENUE", "variational")
    conn = connect(":memory:")
    now = 1000 * H
    _var_pair(conn, now, {now - k * 4 * H: 1.2e-4 for k in range(15)}, {now - k * H: 0.0 for k in range(60)})
    evaluate_all(conn, now)
    _snap(conn, "AAA-PERP", now - 60, now + H)
    conn.execute("INSERT INTO runs (job, started_at, finished_at, ok, detail) VALUES ('sync', ?, ?, 1, ?)",
                 (now - 600, now - 300, '{"hl": {"gap_repairs": 2}, "lt": {"gap_repairs": 1}}'))
    conn.execute("INSERT INTO runs (job, started_at, finished_at, ok) VALUES ('worker_start', ?, ?, 1)",
                 (now - 5 * H, now - 5 * H))
    return conn, now


def test_page_renders_tabs_signals_health_and_activity(seeded):
    conn, now = seeded
    conn.execute("INSERT INTO signal_events (pair_id, ts, event, from_status, to_status, detail) VALUES "
                 "(?, ?, 'STATUS', 'EMERGING', 'QUALIFIED', '')", (VPAIR, now))
    conn.execute("INSERT INTO signal_events (pair_id, ts, event, from_status, to_status, detail) VALUES "
                 "('ZZZ|hyperliquid:ZZZ>lighter:ZZZ', ?, 'STATUS', 'MONITORING', 'QUALIFIED', '')", (now,))
    page = build_page(conn, now=now, db_path=":memory:")
    assert page.startswith("<!doctype html>") and "Funding scanner" in page
    for tab in ("opportunities", "watchlist", "health", "activity"):
        assert f'id="tab-{tab}"' in page
    assert f'data-pair="{html.escape(VPAIR)}"' in page and "1 qualified and 0 emerging" in page
    assert "Emerging to" in page and "ZZZ" not in page                      # real change shown; dropped pair hidden
    assert "hp-ok" in page and "Gaps repaired, last sync</dt><dd>3" in page  # healthy; gap repairs summed
    assert page.count("TOS_RISK") == 0                                    # explained once in plain words instead


def test_pair_detail_has_series_legs_and_explanations(seeded):
    conn, _ = seeded
    d = pair_detail(conn, VPAIR)
    assert d["status"] == "QUALIFIED" and d["long"]["badge"] == "HL" and d["short"]["badge"] == "VAR"
    assert len(d["series"]) == 57 and all(abs(p["net"] - 3e-5) < 1e-12 for p in d["series"])
    p = d["series"][-1]
    assert p["l"] == 0.0 and abs(p["s"] - 3e-5) < 1e-12                   # Variational 1.2e-4 per 4h -> 3e-5/h
    assert d["run_start"] is not None and d["kills"] == [] and "Capped at" in d["leverage_note"]
    assert pair_detail(conn, "nope") is None


def test_server_serves_page_api_and_404(seeded, tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = connect(db)
    now = 1000 * H
    _var_pair(conn, now, {now - k * 4 * H: 1.2e-4 for k in range(15)}, {now - k * H: 0.0 for k in range(60)})
    evaluate_all(conn, now)
    conn.commit()
    stop = threading.Event()
    t = threading.Thread(target=serve, args=("127.0.0.1", 8799, stop, db), daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        body = urllib.request.urlopen("http://127.0.0.1:8799/", timeout=5).read().decode()
        assert "Funding scanner" in body
        api = json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/pair?id=" + urllib.parse.quote(VPAIR),
                                                timeout=5).read())
        assert api["asset"] == "AAA" and api["series"]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:8799/api/pair?id=missing", timeout=5)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:8799/nope", timeout=5)
    finally:
        stop.set()
        t.join(timeout=5)
