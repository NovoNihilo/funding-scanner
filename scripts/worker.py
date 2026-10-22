"""Always-on worker. Thread 'var': Variational snapshot every 5 min (:00:30, :05:30...).
Thread 'sync': hourly at hh:04 -> refresh markets, HL + Lighter settled funding,
costs for candidate pairs, 24h-move stats, signals, timing verdict."""
import logging
import os
import signal
import threading
import time

from src.config import BACKFILL_DAYS, DB_PATH, VAR_RATE_SOURCE
from src.costs import refresh_costs, refresh_price_stats
from src.db import connect
from src.funding import capture_variational, derive_variational, ingest_hyperliquid, ingest_lighter, record_run
from src.matching import refresh_markets
from src.signals import evaluate_all, market_index, prioritized_assets
from src.var_timing import timing_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
log = logging.getLogger("worker")
STOP = threading.Event()


def wait_for_slot(offset_s, period_s):
    now = time.time()
    nxt = (now - offset_s) // period_s * period_s + period_s + offset_s
    STOP.wait(max(0.0, nxt - now))
    return nxt


def variational_loop():
    conn = connect()
    while not STOP.is_set():
        sched = wait_for_slot(30, 300)
        if STOP.is_set():
            break
        t0 = int(time.time())
        try:
            n = capture_variational(conn)
            d = derive_variational(conn)
            log.info("variational snapshot=%d derived=%d late=%ds", n, d, int(t0 - sched))
            record_run(conn, "variational", t0, True, {"snap": n, "derived": d, "late_s": int(t0 - sched)})
        except Exception as e:
            log.exception("variational failed")
            record_run(conn, "variational", t0, False, {"error": repr(e)[:300]})


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
            log.info("costs %s price_stats %s signals %s", cst, pst, sig)
            record_run(conn, "signals", t0, True, {"costs": cst, "price_stats": pst, "signals": sig})
            log.info("var timing: %s", timing_report(conn))
        except Exception as e:
            log.exception("sync failed")
            record_run(conn, "sync", t0, False, {"error": repr(e)[:300]})


def main():
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    threads = [threading.Thread(target=variational_loop, name="var", daemon=True),
               threading.Thread(target=sync_loop, name="sync", daemon=True)]
    for t in threads:
        t.start()
    log.info("worker started db=%s var_mode=%s", DB_PATH, VAR_RATE_SOURCE)
    try:
        while not STOP.is_set():
            STOP.wait(30)
            for t in threads:
                if not t.is_alive() and not STOP.is_set():
                    log.error("thread %s died; exiting so systemd restarts it", t.name)
                    os._exit(1)
    except KeyboardInterrupt:
        STOP.set()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
