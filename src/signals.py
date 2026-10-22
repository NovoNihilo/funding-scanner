"""Signal engine.

For each matched asset and each (long venue, short venue) orientation:
1. Build comparable funding periods. The grid is the coarser leg's settlement schedule:
   hourly for Hyperliquid/Lighter pairs, Variational's own settlement times (1h/4h/8h) otherwise.
   A period is complete only when every settlement both legs owe in it exists; nothing is zero-filled.
   period net = sum(short-leg rates) - sum(long-leg rates)  (fraction of one leg's notional).
2. Exclude dividend-window periods (Variational tradfi interval switch) and extreme outliers.
3. Lifecycle: a run starts at the first positive period and ends after TERMINATE_AFTER consecutive
   non-positive periods. Missing and excluded periods neither count nor reset.
4. Gates (after 24h of run age): conservative funding > 0, capital APY at 1x >= MIN_APY_1X,
   consistency, data coverage, tradable markets. Execution-cost gate is added in M6.
"""
import json
import time
from collections import Counter
from itertools import permutations
from statistics import median

from src.config import (ANOMALY_MAD_K, ANOMALY_MIN_HOURLY, APY_BASIS, MAX_LEVERAGE_CAP, MIN_APY_1X,
                        MIN_COVERAGE, MIN_POSITIVE_SHARE, SIGNAL_LOOKBACK_DAYS, TERMINATE_AFTER)

HOUR = 3600
BLOCKING_FLAGS = {"REDUCE_ONLY", "HIDDEN", "PREIPO", "NO_FUNDING"}
COHORTS = ((24, "FRESH"), (48, "ACTIONABLE"), (168, "ESTABLISHED"))


# ---------------- periods ----------------

def _period(end, hours, net, quality, tags=()):
    return {"end": end, "hours": hours, "net": net, "quality": quality, "tags": set(tags), "excluded": False}


def build_periods(long_r, short_r, grid):
    """long_r/short_r: {ts: (rate, interval_s, quality)}. grid: None (both hourly), 'long' or 'short'
    (that leg is the coarse Variational leg). Returns periods sorted by end time."""
    if not long_r or not short_r:
        return []
    out = []
    if grid is None:
        start, end = max(min(long_r), min(short_r)), max(max(long_r), max(short_r))
        for t in range(start, end + 1, HOUR):
            if t in long_r and t in short_r:
                q = "ESTIMATED" if "ESTIMATED" in (long_r[t][2], short_r[t][2]) else "CONFIRMED"
                out.append(_period(t, 1.0, short_r[t][0] - long_r[t][0], q))
            else:
                out.append(_period(t, 1.0, None, None))
        return out
    coarse, fine = (long_r, short_r) if grid == "long" else (short_r, long_r)
    for t in sorted(coarse):
        rate_c, ivl, q_c = coarse[t]
        hours = max(1, int(round(ivl / HOUR)))
        needed = [t - k * HOUR for k in range(hours)]
        if all(x in fine for x in needed):
            s_fine = sum(fine[x][0] for x in needed)
            net = (rate_c - s_fine) if grid == "short" else (s_fine - rate_c)
            q = "ESTIMATED" if "ESTIMATED" in [q_c] + [fine[x][2] for x in needed] else "CONFIRMED"
            out.append(_period(t, float(hours), net, q))
        else:
            out.append(_period(t, float(hours), None, None))
    return out


def mark_dividend_windows(periods, coarse_r, normal_interval):
    """Variational tradfi: settlements on a shortened interval are the dividend window -> exclude."""
    if not normal_interval:
        return 0
    n = 0
    for p in periods:
        row = coarse_r.get(p["end"])
        if row and row[1] < normal_interval:
            p["tags"].add("DIVIDEND")
            p["excluded"] = True
            n += 1
    return n


def mark_anomalies(periods):
    """Exclude isolated extreme hourly nets (data errors / one-off adjustments), not ordinary reversals."""
    vals = [p["net"] / p["hours"] for p in periods if p["net"] is not None and not p["excluded"]]
    if len(vals) < 12:
        return 0
    med = median(vals)
    mad = max(median(abs(v - med) for v in vals), 1e-6)
    n = 0
    for p in periods:
        if p["net"] is None or p["excluded"]:
            continue
        h = p["net"] / p["hours"]
        if abs(h) >= ANOMALY_MIN_HOURLY and abs(h - med) > ANOMALY_MAD_K * mad:
            p["tags"].add("ANOMALY")
            p["excluded"] = True
            n += 1
    return n


# ---------------- metrics ----------------

def window_stats(periods, now, hours):
    lo = now - hours * HOUR
    ps = [p for p in periods if lo < p["end"] <= now]
    valid = [p for p in ps if p["net"] is not None and not p["excluded"]]
    present_h = sum(p["hours"] for p in ps if p["net"] is not None)
    vh = sum(p["hours"] for p in valid)
    if not vh:
        return {"avg_hourly": None, "coverage": present_h / hours, "pos_share": None, "median_hourly": None,
                "valid_hours": 0}
    return {"avg_hourly": sum(p["net"] for p in valid) / vh,
            "coverage": min(1.0, present_h / hours),
            "pos_share": sum(p["hours"] for p in valid if p["net"] > 0) / vh,
            "median_hourly": median(p["net"] / p["hours"] for p in valid),
            "valid_hours": vh}


def lifecycle(periods):
    """Return (run_start, neg_streak, terminations, longest_neg_hours)."""
    run_start, neg, neg_h, longest, terms = None, 0, 0.0, 0.0, []
    for p in periods:
        if p["net"] is None or p["excluded"]:
            continue
        if p["net"] > 0:
            neg, neg_h = 0, 0.0
            if run_start is None:
                run_start = int(p["end"] - p["hours"] * HOUR)
        else:
            neg_h += p["hours"]
            longest = max(longest, neg_h)
            if run_start is not None:
                neg += 1
                if neg >= TERMINATE_AFTER:
                    terms.append(p["end"])
                    run_start, neg = None, 0
    return run_start, neg, terms, longest


def cohort_for(age_h):
    for limit, name in COHORTS:
        if age_h < limit:
            return name
    return "DURABLE"


def apy_numbers(conservative_hourly, max_lev):
    spread_apr = conservative_hourly * 8760
    apy_1x = spread_apr / 2 if APY_BASIS == "capital" else spread_apr
    return spread_apr, apy_1x, apy_1x * max_lev


def leverage_limit(lev_long, lev_short):
    vals = [MAX_LEVERAGE_CAP] + [v for v in (lev_long, lev_short) if v]
    return max(1.0, float(int(min(vals))))


# ---------------- evaluation ----------------

def _load(conn, venue, symbol, since):
    return {r["ts"]: (r["rate"], r["interval_s"], r["quality"]) for r in conn.execute(
        "SELECT ts, rate, interval_s, quality FROM settlements WHERE venue=? AND symbol=? AND ts>=? ORDER BY ts",
        (venue, symbol, since))}


def _normal_interval(rates):
    c = Counter(v[1] for v in rates.values())
    return c.most_common(1)[0][0] if c else None


def evaluate_pair(lg, sh, long_r, short_r, now):
    """lg/sh: market rows (dict-like) for long and short legs. Returns signal dict (without stored state)."""
    grid = "long" if lg["venue"] == "variational" else ("short" if sh["venue"] == "variational" else None)
    periods = build_periods(long_r, short_r, grid)
    flags, reasons = set(), []
    div_n = 0
    if grid:
        var_leg, var_r = (lg, long_r) if grid == "long" else (sh, short_r)
        if var_leg["asset_class"] == "tradfi":
            normal = _normal_interval(var_r)
            div_n = mark_dividend_windows(periods, var_r, normal)
            if normal and var_leg["interval_s"] and var_leg["interval_s"] < normal:
                flags.add("DIV_IMMINENT")
        flags.update({"TOS_RISK", "ESTIMATED_DATA"})
    anom_n = mark_anomalies(periods)
    if div_n:
        flags.add(f"DIV_EXCLUDED={div_n}")
    if anom_n:
        flags.add(f"ANOMALY_EXCLUDED={anom_n}")
    if "tradfi" in (lg["asset_class"], sh["asset_class"]) and "lighter" in (lg["venue"], sh["venue"]):
        flags.add("DIVIDEND_UNKNOWN")
    for m in (lg, sh):
        for f in (m["flags"] or "").split(","):
            if f in BLOCKING_FLAGS:
                flags.add(f"{m['venue'][:3].upper()}_{f}")
                reasons.append(f"{m['venue']} {f}")

    w24, w48, w7 = (window_stats(periods, now, h) for h in (24, 48, 168))
    candidates = [w["avg_hourly"] for w in (w48, w7) if w["avg_hourly"] is not None and w["valid_hours"] >= 24]
    conservative = min(candidates) if candidates else None
    max_lev = leverage_limit(lg["max_leverage"], sh["max_leverage"])
    spread_apr, apy_1x, apy_max = apy_numbers(conservative, max_lev) if conservative is not None else (None,) * 3

    run_start, neg, terms, longest_neg = lifecycle(periods)
    complete = [p for p in periods if p["net"] is not None]
    last_end = complete[-1]["end"] if complete else None
    grid_h = max((p["hours"] for p in periods), default=1.0)
    age_h = (now - run_start) / HOUR if run_start is not None else 0.0

    if conservative is None or conservative <= 0:
        reasons.append("conservative funding <= 0")
    elif apy_1x < MIN_APY_1X:
        reasons.append(f"APY 1x {apy_1x:.1%} < {MIN_APY_1X:.0%}")
    if w48["pos_share"] is None or w48["pos_share"] < MIN_POSITIVE_SHARE:
        reasons.append(f"consistency: positive share < {MIN_POSITIVE_SHARE:.0%} (48h)")
    if w48["median_hourly"] is None or w48["median_hourly"] <= 0:
        reasons.append("median period net <= 0 (48h)")
    if w24["coverage"] < MIN_COVERAGE:
        reasons.append(f"coverage {w24['coverage']:.0%} < {MIN_COVERAGE:.0%} (24h)")
    if last_end is None or now - last_end > max(2 * grid_h, 3) * HOUR:
        reasons.append("stale data")

    metrics = {
        "avg_hourly_24h": w24["avg_hourly"], "avg_hourly_48h": w48["avg_hourly"], "avg_hourly_7d": w7["avg_hourly"],
        "conservative_hourly": conservative, "spread_apr": spread_apr,
        "daily_net_per_1k": conservative * 24 * 1000 if conservative is not None else None,
        "pos_share_48h": w48["pos_share"], "pos_share_7d": w7["pos_share"],
        "coverage_24h": w24["coverage"], "coverage_7d": w7["coverage"],
        "longest_neg_hours": longest_neg, "terminations": len(terms),
        "last_termination": terms[-1] if terms else None, "last_period_end": last_end, "grid_hours": grid_h,
    }
    return {"run_start": run_start, "neg_streak": neg, "age_h": age_h, "terminated_recently": bool(terms) and
            run_start is None, "apy_1x": apy_1x, "apy_max": apy_max, "max_lev": max_lev,
            "metrics": metrics, "flags": sorted(flags), "reasons": reasons}


def decide_status(sig, prev):
    """prev: stored signals row or None. Returns (status, cohort, qualified_at)."""
    if sig["run_start"] is None:
        return ("TERMINATED" if sig["terminated_recently"] else "MONITORING"), None, None
    cohort = cohort_for(sig["age_h"])
    same_run = prev is not None and prev["run_start"] == sig["run_start"]
    qualified_at = prev["qualified_at"] if same_run else None
    if sig["age_h"] < 24:
        return "MONITORING", cohort, qualified_at
    if not sig["reasons"]:
        return "QUALIFIED", cohort, qualified_at
    return ("SUSPENDED" if qualified_at else "MONITORING"), cohort, qualified_at


def _markets(conn):
    return conn.execute("SELECT venue, symbol, asset, asset_class, flags, max_leverage, interval_s FROM markets "
                        "WHERE matched=1 AND active=1").fetchall()


def evaluate_all(conn, now=None):
    """Recompute every orientation, persist signals, record status/cohort transitions as events."""
    now = int((now or time.time()) // HOUR * HOUR)
    since = int(now - SIGNAL_LOOKBACK_DAYS * 86400 - 8 * HOUR)
    by_asset = {}
    for m in _markets(conn):
        by_asset.setdefault(m["asset"], []).append(m)
    cache = {}

    def rates(m):
        key = (m["venue"], m["symbol"])
        if key not in cache:
            cache[key] = _load(conn, m["venue"], m["symbol"], since)
        return cache[key]

    prev_rows = {r["pair_id"]: r for r in conn.execute("SELECT * FROM signals")}
    rows, events = [], []
    for asset, ms in by_asset.items():
        for lg, sh in permutations(ms, 2):
            if lg["venue"] == sh["venue"]:
                continue
            pair_id = f"{asset}|{lg['venue']}>{sh['venue']}"
            sig = evaluate_pair(lg, sh, rates(lg), rates(sh), now)
            prev = prev_rows.get(pair_id)
            status, cohort, qualified_at = decide_status(sig, prev)
            if status == "QUALIFIED" and not qualified_at:
                qualified_at = now
            old_status = prev["status"] if prev else None
            if status != old_status:
                detail = "; ".join(sig["reasons"]) if status in ("SUSPENDED", "MONITORING") else ""
                if status == "TERMINATED":
                    detail = f"{TERMINATE_AFTER} consecutive non-positive periods"
                events.append((pair_id, now, "STATUS", old_status, status, detail))
            elif status == "QUALIFIED" and prev and cohort != prev["cohort"]:
                events.append((pair_id, now, "COHORT", prev["cohort"], cohort, ""))
            rows.append((pair_id, asset, lg["venue"], lg["symbol"], sh["venue"], sh["symbol"], status, cohort,
                         sig["run_start"], qualified_at, round(sig["age_h"], 2), sig["neg_streak"], sig["apy_1x"],
                         sig["apy_max"], sig["max_lev"], json.dumps(sig["metrics"]), ",".join(sig["flags"]),
                         json.dumps(sig["reasons"]), now))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO signal_events (pair_id, ts, event, from_status, to_status, detail) "
                         "VALUES (?,?,?,?,?,?)", events)
    return {"pairs": len(rows), "events": len(events),
            "status": dict(Counter(r[6] for r in rows))}
