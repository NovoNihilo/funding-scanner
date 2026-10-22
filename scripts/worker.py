"""Always-on worker.
Thread 'var':  Variational snapshot every 5 min (:00:30, :05:30, ...) plus extra captures VAR_PRE_CAPTURE_S seconds
               before every announced settlement; settlements are derived right after each capture.
Thread 'sync': at start-up, then hourly at hh:04 -> refresh markets, HL + Lighter settled funding (with gap repair),
               costs for candidate pairs, 24h-move stats, signals, housekeeping, timing verdict.
Thread 'dash': read-only dashboard on DASHBOARD_HOST:DASHBOARD_PORT (localhost; open it through the SSH tunnel).
Watchdog: the process exits (systemd restarts it within seconds) if a thread dies, the Variational loop stalls for
VAR_STALL_S, or one sync runs longer than SYNC_STALL_S."""
import logging
import os
import signal
import threading
import time

from src.config import BACKFILL_DAYS, DASHBOARD_HOST, DASHBOARD_PORT, DB_PATH, VAR_RATE_SOURCE
from src.costs import refresh_costs, refresh_price_stats
from src.dashboard import serve
from src.db import connect
from src.funding import (capture_variational, derive_variational, housekeeping, ingest_hyperliquid, ingest_lighter,
                         next_capture_time, record_run, upcoming_settlements)
from src.matching import refresh_markets
from src.signals import evaluate_all, market_index, prioritized_assets
from src.var_timing import timing_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
log = logging.getLogger("worker")
STOP = threading.Event()
VAR_STALL_S = 600   # no completed Variational cycle for this long -> restart
SYNC_STALL_S = 3600  # a single sync running this long -> restart
BEATS = {"var": time.time(), "sync_busy_since": None}


def wait_for_slot(offset_s, period_s):
    now = time.time()
    nxt = (now - offset_s) // period_s * period_s + period_s + offset_s
    STOP.wait(max(0.0, nxt - now))
    return nxt


def variational_loop():
    conn = connect()
    due = time.time()  # capture immediately at start-up
    while not STOP.is_set():
        STOP.wait(max(0.0, due - time.time()))
        if STOP.is_set():
            break
        t0 = int(time.time())
        try:
            n = capture_variational(conn)
            d = derive_variational(conn, lookback_s=12 * 3600)  # recent settlements; older ones are final
            log.info("variational snapshot=%d derived=%d late=%ds", n, d, int(t0 - due))
            record_run(conn, "variational", t0, True, {"snap": n, "derived": d, "late_s": int(t0 - due)})
        except Exception as e:
            log.exception("variational failed")
            record_run(conn, "variational", t0, False, {"error": repr(e)[:300]})
        BEATS["var"] = time.time()
        now = time.time()
        try:
            upcoming = upcoming_settlements(conn, now)
        except Exception:
            upcoming = []
        due = next_capture_time(now, upcoming)


def sync_loop():
    conn = connect()
    backfill_s = int(BACKFILL_DAYS * 86400)
    first = True
    while not STOP.is_set():
        if not first:
            wait_for_slot(240, 3600)
            if STOP.is_set():
                break
        first = False
        t0 = int(time.time())
        BEATS["sync_busy_since"] = time.time()
        try:
            refresh_markets(conn)
            hls = ingest_hyperliquid(conn, t0, backfill_s)
            lts = ingest_lighter(conn, t0, backfill_s)
            log.info("sync hl=%s lt=%s took=%ds", hls, lts, int(time.time()) - t0)
            record_run(conn, "sync", t0, hls["errors"] + lts["errors"] == 0, {"hl": hls, "lt": lts})
            pre = evaluate_all(conn, persist=False)
            cst = refresh_costs(conn, pre["candidates"])
            pst = refresh_price_stats(conn, prioritized_assets(market_index(conn), pre["candidates"]), max_n=40)
            sig = evaluate_all(conn)
            sig.pop("candidates")
            hk = housekeeping(conn)
            log.info("costs %s price_stats %s signals %s housekeeping %s", cst, pst, sig, hk)
            record_run(conn, "signals", t0, True, {"costs": cst, "price_stats": pst, "signals": sig, "housekeeping": hk})
            log.info("var timing: %s", timing_report(conn))
        except Exception as e:
            log.exception("sync failed")
            record_run(conn, "sync", t0, False, {"error": repr(e)[:300]})
        BEATS["sync_busy_since"] = None


def dashboard_loop():
    try:
        serve(DASHBOARD_HOST, DASHBOARD_PORT, STOP)
    except Exception:
        log.exception("dashboard failed (collection continues)")


def main():
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    conn = connect()
    record_run(conn, "worker_start", int(time.time()), True, {"var_mode": VAR_RATE_SOURCE})
    conn.close()
    threads = [threading.Thread(target=variational_loop, name="var", daemon=True),
               threading.Thread(target=sync_loop, name="sync", daemon=True)]
    for t in threads:
        t.start()
    threading.Thread(target=dashboard_loop, name="dash", daemon=True).start()  # optional: never restarts the worker
    log.info("worker started db=%s var_mode=%s dashboard=%s:%d", DB_PATH, VAR_RATE_SOURCE, DASHBOARD_HOST,
             DASHBOARD_PORT)
    try:
        while not STOP.is_set():
            STOP.wait(30)
            if STOP.is_set():
                break
            for t in threads:
                if not t.is_alive():
                    log.error("thread %s died; exiting so systemd restarts it", t.name)
                    os._exit(1)
            if time.time() - BEATS["var"] > VAR_STALL_S:
                log.error("variational loop stalled %ds; exiting so systemd restarts it", time.time() - BEATS["var"])
                os._exit(1)
            busy = BEATS["sync_busy_since"]
            if busy and time.time() - busy > SYNC_STALL_S:
                log.error("sync stalled %ds; exiting so systemd restarts it", time.time() - busy)
                os._exit(1)
    except KeyboardInterrupt:
        STOP.set()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
