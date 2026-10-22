"""Funding ingestion. Stored rate = signed fraction of notional per settlement interval,
positive = longs pay shorts. ts = UTC settlement time in seconds. Gaps are never zero-filled."""
import json
import time

from src.config import (RUNS_KEEP_DAYS, VAR_LATE_WINDOW_S, VAR_PRE_CAPTURE_S, VAR_RATE_SOURCE, VAR_SNAPSHOT_KEEP_DAYS,
                        VAR_WINDOW_S)
from src.venues import hyperliquid as hl, lighter as lt, variational as va

HOUR = 3600
LT_RATE_QUANTUM = 1e-6   # Lighter 'rate' is percent with 4 dp
LT_VALUE_QUANTUM = 1e-8  # Lighter 'value' has 8 dp

_INSERT = """INSERT INTO settlements
 (venue, symbol, ts, interval_s, rate, rate_err, quality, source, raw, inserted_at)
 VALUES (?,?,?,?,?,?,?,?,?,?)
 ON CONFLICT(venue, symbol, ts) DO UPDATE SET
  interval_s=excluded.interval_s, rate=excluded.rate, rate_err=excluded.rate_err,
  quality=excluded.quality, source=excluded.source, raw=excluded.raw, inserted_at=excluded.inserted_at
 WHERE settlements.quality='ESTIMATED'
    OR COALESCE(excluded.rate_err, 1) < COALESCE(settlements.rate_err, 1)"""


def insert_settlements(conn, rows):
    """rows: (venue, symbol, ts, interval_s, rate, rate_err, quality, source, raw). Returns rows written."""
    now = int(time.time())
    before = conn.total_changes
    with conn:
        conn.executemany(_INSERT, [tuple(r) + (now,) for r in rows])
    return conn.total_changes - before


def matched_markets(conn, venue):
    return conn.execute("SELECT symbol, market_id FROM markets WHERE venue=? AND matched=1 AND active=1 "
                        "ORDER BY symbol", (venue,)).fetchall()


def first_gap(conn, venue, symbol, since, step=HOUR):
    """Settlement time just before the earliest hole in a market's hourly series since `since`, or None."""
    ts = [r[0] for r in conn.execute("SELECT ts FROM settlements WHERE venue=? AND symbol=? AND ts>=? ORDER BY ts",
                                     (venue, symbol, since))]
    return next((a for a, b in zip(ts, ts[1:]) if b - a > step), None)


def _since(conn, venue, symbol, now, backfill_s):
    """Fetch start: 3h before the last stored settlement (reconciles late/revised rows), or earlier when a hole
    exists inside the backfill window (gap repair). Holes the venue genuinely lacks are simply re-requested."""
    row = conn.execute("SELECT last_ts FROM sync_state WHERE venue=? AND symbol=?",
                       (venue, symbol)).fetchone()
    if row and row["last_ts"]:
        start = row["last_ts"] - 3 * HOUR
        gap = first_gap(conn, venue, symbol, now - backfill_s)
        return (gap, True) if gap is not None and gap < start else (start, False)
    return now - backfill_s, False


def _mark(conn, venue, symbol, now, last_ts=None, error=None):
    with conn:
        conn.execute("""INSERT INTO sync_state (venue, symbol, last_ts, last_attempt, last_error)
            VALUES (?,?,?,?,?) ON CONFLICT(venue, symbol) DO UPDATE SET
            last_ts=MAX(COALESCE(sync_state.last_ts, 0), COALESCE(excluded.last_ts, 0)),
            last_attempt=excluded.last_attempt, last_error=excluded.last_error""",
                     (venue, symbol, last_ts, now, error))


def record_run(conn, job, started_at, ok, detail):
    with conn:
        conn.execute("INSERT INTO runs (job, started_at, finished_at, ok, detail) VALUES (?,?,?,?,?)",
                     (job, started_at, int(time.time()), int(ok), json.dumps(detail)))


# ---------------- Hyperliquid ----------------

def ingest_hyperliquid(conn, now, backfill_s):
    stats = {"markets": 0, "written": 0, "errors": 0, "gap_repairs": 0}
    for m in matched_markets(conn, "hyperliquid"):
        sym = m["symbol"]
        stats["markets"] += 1
        try:
            start, repair = _since(conn, "hyperliquid", sym, now, backfill_s)
            stats["gap_repairs"] += repair
            start_ms = start * 1000
            last = None
            while True:
                data = hl.funding_history(sym, start_ms, now * 1000)
                rows = [("hyperliquid", sym, int(round(r["time"] / 1000 / HOUR)) * HOUR, HOUR,
                         float(r["fundingRate"]), 0.0, "CONFIRMED", "hl_history",
                         json.dumps({"premium": r.get("premium")})) for r in data]
                stats["written"] += insert_settlements(conn, rows)
                if rows:
                    last = max(last or 0, max(r[2] for r in rows))
                if len(data) < 500:
                    break
                start_ms = max(int(r["time"]) for r in data) + 1
            _mark(conn, "hyperliquid", sym, now, last)
        except Exception as e:
            stats["errors"] += 1
            _mark(conn, "hyperliquid", sym, now, None, repr(e)[:300])
        time.sleep(0.25)  # pace Hyperliquid info requests (shared per-IP weight budget)
    return stats


# ---------------- Lighter ----------------

def price_at(closes, ts):
    if ts in closes:
        return closes[ts]
    near = [k for k in closes if abs(k - ts) <= 3 * HOUR]
    return closes[min(near, key=lambda k: abs(k - ts))] if near else None


def lighter_rate(row, price):
    """Return (signed_rate, abs_error, source). Prefer value/price when it is more precise and consistent."""
    sign = 1.0 if row["direction"] == "long" else -1.0
    rate_field = float(row["rate"]) / 100.0
    best = (sign * rate_field + 0.0, LT_RATE_QUANTUM / 2, "lt_rate")
    if price and price > 0:
        r_v = float(row["value"]) / price
        err_v = LT_VALUE_QUANTUM / 2 / price
        consistent = abs(r_v - rate_field) <= LT_RATE_QUANTUM / 2 + err_v + 0.05 * rate_field
        if err_v < best[1] and consistent:
            best = (sign * r_v + 0.0, err_v, "lt_value")
    return best


def ingest_lighter(conn, now, backfill_s, progress=False):
    stats = {"markets": 0, "written": 0, "errors": 0, "gap_repairs": 0, "lt_value": 0, "lt_rate": 0}
    markets = matched_markets(conn, "lighter")
    for i, m in enumerate(markets, 1):
        sym, mid = m["symbol"], int(m["market_id"])
        stats["markets"] += 1
        try:
            start, repair = _since(conn, "lighter", sym, now, backfill_s)
            stats["gap_repairs"] += repair
            last = None
            while start < now:
                end = min(now, start + 400 * HOUR)
                data = lt.fundings(mid, start, end)
                if data:
                    closes = lt.hourly_closes(mid, start - 2 * HOUR, end)
                    rows = []
                    for r in data:
                        ts = int(r["timestamp"])
                        ts = ts // 1000 if ts > 1e11 else ts
                        ts = int(round(ts / HOUR)) * HOUR
                        rate, err, src = lighter_rate(r, price_at(closes, ts))
                        stats[src] += 1
                        rows.append(("lighter", sym, ts, HOUR, rate, err, "CONFIRMED", src, json.dumps(r)))
                    stats["written"] += insert_settlements(conn, rows)
                    last = max(last or 0, max(x[2] for x in rows))
                start = end
            _mark(conn, "lighter", sym, now, last)
        except Exception as e:
            stats["errors"] += 1
            _mark(conn, "lighter", sym, now, None, repr(e)[:300])
        if progress and i % 25 == 0:
            print(f"  lighter {i}/{len(markets)}", flush=True)
    return stats


# ---------------- Variational (ESTIMATED) ----------------

def upcoming_settlements(conn, now, recent_s=900):
    """Settlement times announced by recent snapshots that are still in the future."""
    return sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT next_ts FROM var_snapshots WHERE captured_at >= ? AND next_ts > ?", (now - recent_s, now)))


def next_capture_time(now, upcoming, period=300, offset=30, pre_offsets=None):
    """Next snapshot time: the regular grid (every `period` s at `offset`) or an extra capture `pre_offsets`
    seconds before any upcoming settlement, whichever comes first."""
    pre_offsets = VAR_PRE_CAPTURE_S if pre_offsets is None else pre_offsets
    regular = (now - offset) // period * period + period + offset
    extra = [t - o for t in upcoming for o in pre_offsets if t - o > now]
    return min([regular] + extra)


def capture_variational(conn, now=None):
    """Store one snapshot of funding + quotes for matched Variational perps."""
    now = int(now or time.time())
    matched = {r["symbol"] for r in matched_markets(conn, "variational")}
    rows = []
    for x in va.stats()["listings"]:
        f = x.get("funding")
        if x["ticker"] not in matched or x.get("contract_type") != "perpetual" or not f or not f.get("last"):
            continue
        q = x.get("quotes") or {}

        def qf(size, side):
            v = (q.get(size) or {}).get(side)
            return float(v) if v else None

        rows.append((x["ticker"], now, f.get("next_timestamp"), f.get("interval_seconds"),
                     float(f["last"]["interval_rate"]), float(f["last"]["annualized_rate"]),
                     float(x.get("mark_price") or 0) or None,
                     qf("size_1k", "bid"), qf("size_1k", "ask"), qf("size_100k", "bid"), qf("size_100k", "ask"),
                     q.get("updated_at")))
    with conn:
        conn.executemany("INSERT OR IGNORE INTO var_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def derive_variational(conn, now=None, mode=None, window=None, lookback_s=3 * 86400, late_window=None):
    """Turn snapshots around each settlement time T into ESTIMATED settlements.
    pre  = last snapshot in [T-window, T) for that interval, recorded as soon as T has passed;
           if none, the newest snapshot in [T-late_window, T-window) is used (source var_pre_late).
    post = first snapshot in [T, T+window], recorded once that window has closed.
    Settlements without a usable snapshot are skipped, never zero-filled. Re-deriving always keeps the best value."""
    now = int(now or time.time())
    mode = mode or VAR_RATE_SOURCE
    window = window or VAR_WINDOW_S
    late_window = max(late_window or VAR_LATE_WINDOW_S, window)
    ready = now if mode == "pre" else now - window
    written = 0
    syms = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM var_snapshots WHERE captured_at >= ?",
                                       (now - lookback_s,))]
    for sym in syms:
        snaps = conn.execute("""SELECT captured_at, next_ts, interval_s, interval_rate FROM var_snapshots
            WHERE symbol=? AND captured_at >= ? ORDER BY captured_at""",
                             (sym, now - lookback_s - late_window)).fetchall()
        rows = []
        for T in sorted({s["next_ts"] for s in snaps if s["next_ts"] and s["next_ts"] <= ready}):
            pre = [s for s in snaps if T - window <= s["captured_at"] < T and s["next_ts"] == T]
            late = [s for s in snaps if T - late_window <= s["captured_at"] < T - window and s["next_ts"] == T]
            post = [s for s in snaps if T <= s["captured_at"] <= T + window]
            p = pre[-1] if pre else (late[-1] if late else None)
            q = post[0] if post else None
            chosen = p if mode == "pre" else q
            if chosen is None or chosen["interval_rate"] is None:
                continue
            source = f"var_{mode}" + ("_late" if mode == "pre" and not pre else "")
            raw = {"pre": p["interval_rate"] if p else None, "post": q["interval_rate"] if q else None,
                   "pre_age_s": T - p["captured_at"] if p else None,
                   "post_lag_s": q["captured_at"] - T if q else None}
            rows.append(("variational", sym, T, (p or q)["interval_s"], chosen["interval_rate"], None,
                         "ESTIMATED", source, json.dumps(raw)))
        written += insert_settlements(conn, rows)
    return written


def housekeeping(conn, now=None):
    """Prune Variational snapshots and run logs beyond their retention (settlements are kept forever)."""
    now = int(now or time.time())
    with conn:
        snaps = conn.execute("DELETE FROM var_snapshots WHERE captured_at < ?",
                             (now - int(VAR_SNAPSHOT_KEEP_DAYS * 86400),)).rowcount
        runs = conn.execute("DELETE FROM runs WHERE started_at < ?", (now - int(RUNS_KEEP_DAYS * 86400),)).rowcount
    return {"snapshots_pruned": snaps, "runs_pruned": runs}


# ---------------- Data quality ----------------

def completeness(conn, venue, since):
    """(symbol, rows, expected, missing) using each market's smallest interval."""
    out = []
    for r in conn.execute("""SELECT symbol, COUNT(*) n, MIN(ts) a, MAX(ts) b, MIN(interval_s) i
            FROM settlements WHERE venue=? AND ts>=? GROUP BY symbol ORDER BY symbol""", (venue, since)):
        expected = (r["b"] - r["a"]) // r["i"] + 1
        out.append((r["symbol"], r["n"], expected, expected - r["n"]))
    return out
