"""Milestone 3: refresh markets, backfill/sync settled funding, snapshot Variational, report quality."""
import time

from src.config import BACKFILL_DAYS
from src.db import connect
from src.funding import (capture_variational, completeness, derive_variational, ingest_hyperliquid,
                         ingest_lighter, record_run)
from src.matching import refresh_markets


def main():
    conn = connect()
    now = int(time.time())
    backfill_s = int(BACKFILL_DAYS * 86400)
    t0 = time.time()
    refresh_markets(conn)
    print(f"markets refreshed ({time.time() - t0:.0f}s)")
    hls = ingest_hyperliquid(conn, now, backfill_s)
    print(f"hyperliquid: {hls} ({time.time() - t0:.0f}s)")
    lts = ingest_lighter(conn, now, backfill_s, progress=True)
    print(f"lighter: {lts} ({time.time() - t0:.0f}s)")
    n = capture_variational(conn, now)
    d = derive_variational(conn, now)
    print(f"variational: snapshot rows={n} derived settlements={d} (0 expected until snapshots span settlements)")
    record_run(conn, "sync_funding", now, hls["errors"] + lts["errors"] == 0, {"hl": hls, "lt": lts})

    print("\n=== COMPLETENESS (last backfill window) ===")
    for venue in ("hyperliquid", "lighter"):
        rep = completeness(conn, venue, now - backfill_s)
        gaps = [r for r in rep if r[3] > 0]
        stale = conn.execute("SELECT COUNT(*) FROM (SELECT symbol, MAX(ts) m FROM settlements WHERE venue=? "
                             "GROUP BY symbol) WHERE m < ?", (venue, now - 2 * 3600)).fetchone()[0]
        print(f"{venue}: markets={len(rep)} rows={sum(r[1] for r in rep)} with_gaps={len(gaps)} stale(>2h)={stale}")
        for g in gaps[:8]:
            print(f"  gap {g[0]}: rows={g[1]} expected={g[2]} missing={g[3]}")
    errs = conn.execute("SELECT venue, symbol, last_error FROM sync_state WHERE last_error IS NOT NULL "
                        "LIMIT 8").fetchall()
    print(f"sync errors: {[tuple(e) for e in errs]}")

    print("\n=== SOURCES ===")
    for r in conn.execute("SELECT venue, source, quality, COUNT(*) n, AVG(rate_err) e FROM settlements "
                          "GROUP BY venue, source, quality"):
        e = f"{r['e']:.1e}" if r["e"] is not None else "n/a"
        print(f"  {r['venue']:<12} {r['source']:<11} {r['quality']:<10} n={r['n']:<6} avg_err={e}")

    print("\n=== BTC last 4 settlements (rate per interval) ===")
    for venue, sym in (("hyperliquid", "BTC"), ("lighter", "BTC")):
        rows = conn.execute("SELECT ts, rate, source FROM settlements WHERE venue=? AND symbol=? "
                            "ORDER BY ts DESC LIMIT 4", (venue, sym)).fetchall()
        print(f"  {venue}: " + "  ".join(f"{time.strftime('%H:%M', time.gmtime(r['ts']))}={r['rate']:+.3e}"
                                         for r in rows))
    print("\nSYNC OK")


if __name__ == "__main__":
    main()
