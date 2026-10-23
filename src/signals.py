"""Signal engine.

For each matched asset and each (long venue, short venue) orientation with a leg on REQUIRED_VENUE (if set):
1. Build an HOURLY funding series. Each hour pairs the Hyperliquid/Lighter hourly settlement with the
   Variational leg's LAST KNOWN settlement at or before that hour, converted to a per-hour rate and held until
   the next settlement (never longer than its interval + 1h). Missing hours stay missing; nothing is zero-filled.
   hour net = short-leg hourly rate - long-leg hourly rate  (fraction of one leg's notional).
2. Exclude dividend-window hours (Variational tradfi interval switch) and extreme outliers.
3. Lifecycle: a run starts at the first positive hour. A streak of non-positive hours kills it when the streak
   is TERMINATE_AFTER+ hours long AND its combined loss exceeds KILL_LOSS_SHARE of what the run earned in the
   KILL_CUSHION_H hours before the streak, or when the streak reaches TERMINATE_HARD_AFTER hours whatever its
   size. Small dips inside a healthy run are tolerated. The next positive hour after a kill starts a new run
   from zero. Missing/excluded hours neither count nor reset.
4. Status: MONITORING -> EMERGING (run 8-24h old, passing the gates on the run so far) -> QUALIFIED (run 24h+,
   conservative funding = min(48h, 7d) average, plus consistency and coverage gates) -> SUSPENDED if a qualified
   run later fails a gate; TERMINATED when the run is killed. Gates judge the FUNDING signal only.
5. Execution economics are informational and never block a signal: round-trip cost (taker worst case and
   maker best case), payback ETA at the CURRENT rate (24h average), expected 24h net, expected net over
   HOLD_HORIZON_H at the conservative rate (used for ranking). Trend: last 12h vs the 24h before it.
6. Leverage range: 1x up to min(venue limits, MAX_LEVERAGE_CAP, 24h-stress-safe leverage).
"""
import json
import time
from bisect import bisect_right
from collections import Counter
from itertools import permutations
from statistics import median

from src.config import (ANOMALY_MAD_K, ANOMALY_MIN_HOURLY, APY_BASIS, COST_MAX_AGE_S, COST_SIZES, EMERGING_MIN_H,
                        HOLD_HORIZON_H, KILL_CUSHION_H, KILL_LOSS_SHARE, MAX_LEVERAGE_CAP, MIN_APY_1X,
                        MIN_COVERAGE, MIN_POSITIVE_SHARE,
                        PAYBACK_FAST_H, PAYBACK_OK_H, PRIMARY_SIZE, QUALIFY_MIN_H, REQUIRED_VENUE,
                        SIGNAL_LOOKBACK_DAYS, TERMINATE_AFTER, TERMINATE_HARD_AFTER, TREND_APY_DELTA,
                        TREND_PRIOR_H, TREND_RECENT_H)
from src.costs import stress_leverage

HOUR = 3600
BLOCKING_FLAGS = {"REDUCE_ONLY", "HIDDEN", "PREIPO", "NO_FUNDING"}
COHORTS = ((EMERGING_MIN_H, "FRESH"), (QUALIFY_MIN_H, "EMERGING"), (48, "ACTIONABLE"), (168, "ESTABLISHED"))
COST_CANDIDATE_FRACTION = 0.8  # fetch execution costs for pairs within 80% of the APY minimum
STALE_AFTER_H = 3  # no complete hour for this long -> stale


# ---------------- periods ----------------

def _period(end, hours, net, quality, tags=()):
    return {"end": end, "hours": hours, "net": net, "quality": quality, "tags": set(tags), "excluded": False}


def var_hourly(var_r, keys, t):
    """Variational leg for the hour ending t: (per-hour rate, settlement ts, interval_s) of the last known
    settlement at or before t, or None if there is none or it is older than its interval + 1h (a missed one)."""
    i = bisect_right(keys, t) - 1
    if i < 0:
        return None
    ts = keys[i]
    rate, ivl, _ = var_r[ts]
    ivl = ivl or HOUR
    if t - ts > ivl + HOUR:
        return None
    return rate / (ivl / HOUR), ts, ivl


def build_periods(long_r, short_r, var_leg=None, normal_interval=None):
    """Hourly periods. long_r/short_r: {ts: (rate per interval, interval_s, quality)}.
    var_leg: None (both legs hourly), 'long' or 'short' (that leg is Variational, held forward hour by hour).
    normal_interval: Variational tradfi interval; hours priced off a shorter (dividend-window) settlement are excluded."""
    if not long_r or not short_r:
        return []
    out = []
    if var_leg is None:
        start, end = max(min(long_r), min(short_r)), max(max(long_r), max(short_r))
        for t in range(start, end + 1, HOUR):
            if t in long_r and t in short_r:
                q = "ESTIMATED" if "ESTIMATED" in (long_r[t][2], short_r[t][2]) else "CONFIRMED"
                out.append(_period(t, 1.0, short_r[t][0] - long_r[t][0], q))
            else:
                out.append(_period(t, 1.0, None, None))
        return out
    var_r, fine = (long_r, short_r) if var_leg == "long" else (short_r, long_r)
    keys = sorted(var_r)
    for t in range(max(min(fine), keys[0]), max(fine) + 1, HOUR):
        v = var_hourly(var_r, keys, t)
        if t not in fine or v is None:
            out.append(_period(t, 1.0, None, None))
            continue
        vh, ts, ivl = v
        f = fine[t][0]
        p = _period(t, 1.0, (f - vh) if var_leg == "long" else (vh - f), "ESTIMATED")
        p["var_ts"] = ts
        if normal_interval and ivl < normal_interval:
            p["tags"].add("DIVIDEND")
            p["excluded"] = True
        out.append(p)
    return out


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
    """Return (run_start, neg_streak, terminations, longest_neg_hours, kill_reasons).
    A non-positive streak kills the run when it is TERMINATE_AFTER+ hours long and has lost more than
    KILL_LOSS_SHARE of the run's earnings in the KILL_CUSHION_H hours before the streak (the cushion), or when it
    reaches TERMINATE_HARD_AFTER hours. A run with no cushion is killed by any TERMINATE_AFTER-hour streak."""
    run_start, neg, neg_h, longest, terms, why = None, 0, 0.0, 0.0, [], []
    hist, loss, cushion = [], 0.0, 0.0  # hist: (end, net) of the current run's valid hours
    for p in periods:
        if p["net"] is None or p["excluded"]:
            continue
        if p["net"] > 0:
            neg, neg_h, loss = 0, 0.0, 0.0
            if run_start is None:
                run_start, hist = int(p["end"] - p["hours"] * HOUR), []
            hist.append((p["end"], p["net"]))
            continue
        neg_h += p["hours"]
        longest = max(longest, neg_h)
        if run_start is None:
            continue
        if neg == 0:
            lo = p["end"] - p["hours"] * HOUR - KILL_CUSHION_H * HOUR
            cushion = max(0.0, sum(n for e, n in hist if e > lo))
        neg += 1
        loss -= p["net"]
        hist.append((p["end"], p["net"]))
        reason = None
        if neg >= TERMINATE_HARD_AFTER:
            reason = f"{neg} unprofitable hours in a row"
        elif neg >= TERMINATE_AFTER and loss > KILL_LOSS_SHARE * cushion:
            reason = (f"{neg} unprofitable hours in a row lost {loss / cushion:.0%} of the previous "
                      f"{KILL_CUSHION_H}h's earnings" if cushion > 0 else
                      f"{neg} unprofitable hours in a row with no earnings before them")
        if reason:
            terms.append(p["end"])
            why.append(reason)
            run_start, neg, loss, hist = None, 0, 0.0, []
    return run_start, neg, terms, longest, why


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


def payback_tier(hours):
    if hours is None:
        return "NEVER"
    return "FAST" if hours <= PAYBACK_FAST_H else ("OK" if hours <= PAYBACK_OK_H else "SLOW")


def pair_costs(lg, sh, lg_costs, sh_costs, rate_now, rate_hold=None):
    """Per size: round-trip cost (USD) taker/maker, payback ETA (hours) taker/maker and expected 24h net at the
    CURRENT hourly rate, expected net over HOLD_HORIZON_H at the conservative rate (taker, USD), adverse basis (bps).
    lg_costs/sh_costs: {size: book_costs row}.
    Adverse basis = long venue priced above short venue at entry (assumed to converge by exit);
    a favourable basis is not counted as profit."""
    out, flags = {}, set()
    rate_hold = rate_now if rate_hold is None else rate_hold

    def payback(frac):
        return frac / rate_now if rate_now and rate_now > 0 else None

    for size in COST_SIZES:
        cl, cs = lg_costs.get(size), sh_costs.get(size)
        if cl is None or cs is None:
            out[size] = {"status": "MISSING"}
            continue
        if cl["rt_cost"] is None or cs["rt_cost"] is None:
            out[size] = {"status": "NO_DEPTH"}
            continue
        for row in (cl, cs):
            flags.update(f for f in (row["flags"] or "").split(",") if f)
        basis = 0.0
        if cl["mid"] and cs["mid"]:
            ul, us = cl["mid"] / lg["multiplier"], cs["mid"] / sh["multiplier"]
            basis = (ul - us) / ((ul + us) / 2)
        adverse = max(0.0, basis)
        frac = cl["rt_cost"] + cs["rt_cost"] + adverse
        mk_l = cl["rt_cost_maker"] if cl["rt_cost_maker"] is not None else cl["rt_cost"]
        mk_s = cs["rt_cost_maker"] if cs["rt_cost_maker"] is not None else cs["rt_cost"]
        frac_mk = mk_l + mk_s + adverse
        out[size] = {"status": "OK", "cost_usd": frac * size, "cost_usd_maker": frac_mk * size,
                     "cost_bps": frac * 1e4, "basis_bps": basis * 1e4,
                     "payback_h": payback(frac), "payback_h_maker": payback(frac_mk),
                     "payback_tier": payback_tier(payback(frac)),
                     "net24_usd": (rate_now * 24 - frac) * size if rate_now is not None else None,
                     "net_hold_usd": (rate_hold * HOLD_HORIZON_H - frac) * size if rate_hold is not None else None}
    return out, sorted(flags)


def _normal_interval(rates):
    c = Counter(v[1] for v in rates.values())
    return c.most_common(1)[0][0] if c else None


def trend_of(periods, now):
    """UP / DOWN / STABLE: APY at 1x over the last TREND_RECENT_H vs the TREND_PRIOR_H before it."""
    rec = window_stats(periods, now, TREND_RECENT_H)
    pri = window_stats(periods, now - TREND_RECENT_H * HOUR, TREND_PRIOR_H)
    if rec["valid_hours"] < TREND_RECENT_H / 2 or pri["valid_hours"] < TREND_PRIOR_H / 3:
        return None, None
    delta = apy_numbers(rec["avg_hourly"], 1)[1] - apy_numbers(pri["avg_hourly"], 1)[1]
    return ("UP" if delta > TREND_APY_DELTA else "DOWN" if delta < -TREND_APY_DELTA else "STABLE"), delta


def evaluate_pair(lg, sh, long_r, short_r, now, lg_costs=None, sh_costs=None, p99=None):
    """lg/sh: market rows (dict-like) for long and short legs. Returns signal dict (without stored state)."""
    var_leg = "long" if lg["venue"] == "variational" else ("short" if sh["venue"] == "variational" else None)
    flags, hard = set(), []
    normal = None
    if var_leg:
        vm, vr = (lg, long_r) if var_leg == "long" else (sh, short_r)
        if vm["asset_class"] == "tradfi":
            normal = _normal_interval(vr)
            if normal and vm["interval_s"] and vm["interval_s"] < normal:
                flags.add("DIV_IMMINENT")
        flags.update({"TOS_RISK", "ESTIMATED_DATA"})
    periods = [p for p in build_periods(long_r, short_r, var_leg, normal) if p["end"] <= now]
    div_n = sum(1 for p in periods if "DIVIDEND" in p["tags"])
    anom_n = mark_anomalies(periods)
    if div_n:
        flags.add(f"DIV_EXCLUDED={div_n}")
    if anom_n:
        flags.add(f"ANOMALY_EXCLUDED={anom_n}")
    undocumented = [m for m in (lg, sh) if m["venue"] == "lighter" or "HIP3" in (m["flags"] or "")]
    if "tradfi" in (lg["asset_class"], sh["asset_class"]) and undocumented:
        flags.add("DIVIDEND_UNKNOWN")  # Lighter and HIP-3 deployers publish no dividend procedure
    for m in (lg, sh):
        for f in (m["flags"] or "").split(","):
            if f in BLOCKING_FLAGS:
                flags.add(f"{m['venue'][:3].upper()}_{f}")
                hard.append(f"{m['venue']} {f}")

    w24, w48, w7 = (window_stats(periods, now, h) for h in (24, 48, 168))
    candidates = [w["avg_hourly"] for w in (w48, w7) if w["avg_hourly"] is not None and w["valid_hours"] >= 24]
    conservative = min(candidates) if candidates else None
    current = w24["avg_hourly"] if w24["valid_hours"] >= EMERGING_MIN_H else None
    shown = conservative if conservative is not None else current  # APY shown: conservative once 24h exist
    if conservative is None and current is not None:
        flags.add("EARLY_APY")
    stress_lev, stress_move = stress_leverage(p99, [(lg["venue"], lg["max_leverage"]),
                                                    (sh["venue"], sh["max_leverage"])])
    max_lev = min(leverage_limit(lg["max_leverage"], sh["max_leverage"]), stress_lev)
    if p99 is None:
        flags.add("STRESS_DEFAULT")
    spread_apr, apy_1x, apy_max = apy_numbers(shown, max_lev) if shown is not None else (None,) * 3

    run_start, neg, terms, longest_neg, kill_why = lifecycle(periods)
    complete = [p for p in periods if p["net"] is not None]
    last_end = complete[-1]["end"] if complete else None
    age_h = (now - run_start) / HOUR if run_start is not None else 0.0
    if last_end is None or now - last_end > STALE_AFTER_H * HOUR:
        hard.append("stale data")

    # QUALIFIED gates (run 24h+): conservative rate over 48h / 7d
    qualify = list(hard)
    if conservative is None or conservative <= 0:
        qualify.append("conservative funding <= 0")
    elif apy_numbers(conservative, 1)[1] < MIN_APY_1X:
        qualify.append(f"APY 1x {apy_numbers(conservative, 1)[1]:.1%} < {MIN_APY_1X:.0%}")
    if w48["pos_share"] is None or w48["pos_share"] < MIN_POSITIVE_SHARE:
        qualify.append(f"consistency: positive share < {MIN_POSITIVE_SHARE:.0%} (48h)")
    if w48["median_hourly"] is None or w48["median_hourly"] <= 0:
        qualify.append("median hourly net <= 0 (48h)")
    if w24["coverage"] < MIN_COVERAGE:
        qualify.append(f"coverage {w24['coverage']:.0%} < {MIN_COVERAGE:.0%} (24h)")

    # EMERGING gates (run 8-24h): the same tests on the run so far
    emerging = list(hard)
    run = window_stats(periods, now, max(1, int(age_h))) if run_start is not None else None
    if run is None or run["avg_hourly"] is None or run["avg_hourly"] <= 0:
        emerging.append("run funding <= 0")
    else:
        if apy_numbers(run["avg_hourly"], 1)[1] < MIN_APY_1X:
            emerging.append(f"run APY 1x {apy_numbers(run['avg_hourly'], 1)[1]:.1%} < {MIN_APY_1X:.0%}")
        if run["pos_share"] < MIN_POSITIVE_SHARE:
            emerging.append(f"consistency: positive share < {MIN_POSITIVE_SHARE:.0%} (run)")
        if run["coverage"] < MIN_COVERAGE:
            emerging.append(f"coverage {run['coverage']:.0%} < {MIN_COVERAGE:.0%} (run)")
    reasons = qualify if age_h >= QUALIFY_MIN_H else emerging

    trend, trend_delta = trend_of(periods, now)
    rate_now = current if current is not None else conservative
    costs, cost_flags = pair_costs(lg, sh, lg_costs or {}, sh_costs or {}, rate_now,
                                   conservative if conservative is not None else current)
    flags.update(f"Q_{f}" for f in cost_flags if f != "INDICATIVE")
    primary = costs.get(PRIMARY_SIZE, {"status": "MISSING"})
    if primary["status"] == "NO_DEPTH":
        flags.add("NO_DEPTH")

    metrics = {
        "avg_hourly_24h": w24["avg_hourly"], "avg_hourly_48h": w48["avg_hourly"], "avg_hourly_7d": w7["avg_hourly"],
        "conservative_hourly": conservative, "current_hourly": current, "spread_apr": spread_apr,
        "apy_now_1x": apy_numbers(current, 1)[1] if current is not None else None,
        "run_apy_1x": apy_numbers(run["avg_hourly"], 1)[1] if run and run["avg_hourly"] is not None else None,
        "trend": trend, "trend_delta_apy": trend_delta,
        "daily_net_per_1k": shown * 24 * 1000 if shown is not None else None,
        "pos_share_48h": w48["pos_share"], "pos_share_7d": w7["pos_share"],
        "coverage_24h": w24["coverage"], "coverage_7d": w7["coverage"],
        "history_hours": w7["valid_hours"], "history_needed_hours": 24,
        "longest_neg_hours": longest_neg, "terminations": len(terms),
        "last_termination": terms[-1] if terms else None,
        "last_kill_reason": kill_why[-1] if kill_why else None, "last_period_end": last_end,
        "stress_move": stress_move, "stress_lev": stress_lev,
        "costs": {str(int(k)): v for k, v in costs.items()},
        "rank_net_hold_usd": primary.get("net_hold_usd"),
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
    if sig["age_h"] < EMERGING_MIN_H:
        return "MONITORING", cohort, qualified_at
    if sig["age_h"] < QUALIFY_MIN_H:
        return ("EMERGING" if not sig["reasons"] else "MONITORING"), cohort, qualified_at
    if not sig["reasons"]:
        return "QUALIFIED", cohort, qualified_at
    return ("SUSPENDED" if qualified_at else "MONITORING"), cohort, qualified_at


def _markets(conn):
    return conn.execute("SELECT venue, symbol, asset, asset_class, flags, max_leverage, interval_s, multiplier, "
                        "market_id FROM markets WHERE matched=1 AND active=1").fetchall()


def _cost_rows(conn, now):
    out = {}
    for r in conn.execute("SELECT * FROM book_costs WHERE captured_at >= ?", (now - COST_MAX_AGE_S,)):
        out.setdefault((r["venue"], r["symbol"]), {})[r["size_usd"]] = r
    return out


def market_index(conn):
    """{asset: [(venue, symbol, market_id)]} for matched, active markets."""
    out = {}
    for m in _markets(conn):
        out.setdefault(m["asset"], []).append((m["venue"], m["symbol"], m["market_id"]))
    return out


def prioritized_assets(index, candidates):
    """Reorder {asset: legs} so assets of cost candidates come first (for the stress-stats refresh)."""
    owner = {(v, s): a for a, legs in index.items() for v, s, _ in legs}
    first = {owner[(v, s)] for v, s, _ in candidates if (v, s) in owner}
    return {a: index[a] for a in sorted(index, key=lambda a: (a not in first, a))}


def evaluate_all(conn, now=None, persist=True):
    """Recompute every orientation. persist=True stores signals and status/cohort transition events.
    Always returns a summary including 'candidates': markets whose execution costs should be refreshed."""
    now = int((now or time.time()) // HOUR * HOUR)
    since = int(now - SIGNAL_LOOKBACK_DAYS * 86400 - 8 * HOUR)
    by_asset = {}
    for m in _markets(conn):
        by_asset.setdefault(m["asset"], []).append(m)
    cache = {}
    costs = _cost_rows(conn, now)
    p99s = {r["asset"]: r["p99_24h"] for r in conn.execute("SELECT asset, p99_24h FROM price_stats")}
    candidates = set()

    def rates(m):
        key = (m["venue"], m["symbol"])
        if key not in cache:
            cache[key] = _load(conn, m["venue"], m["symbol"], since)
        return cache[key]

    prev_rows = {r["pair_id"]: r for r in conn.execute("SELECT * FROM signals")}
    rows, events = [], []
    for asset, ms in by_asset.items():
        for lg, sh in permutations(ms, 2):
            if lg["venue"] == sh["venue"] or (REQUIRED_VENUE and REQUIRED_VENUE not in (lg["venue"], sh["venue"])):
                continue
            pair_id = f"{asset}|{lg['venue']}:{lg['symbol']}>{sh['venue']}:{sh['symbol']}"
            sig = evaluate_pair(lg, sh, rates(lg), rates(sh), now, costs.get((lg["venue"], lg["symbol"])),
                                costs.get((sh["venue"], sh["symbol"])), p99s.get(asset))
            prev = prev_rows.get(pair_id)
            if (sig["apy_1x"] is not None and sig["apy_1x"] >= COST_CANDIDATE_FRACTION * MIN_APY_1X) or \
                    (prev is not None and prev["status"] == "QUALIFIED"):
                candidates.update({(lg["venue"], lg["symbol"], lg["market_id"]),
                                   (sh["venue"], sh["symbol"], sh["market_id"])})
            status, cohort, qualified_at = decide_status(sig, prev)
            if status == "QUALIFIED" and not qualified_at:
                qualified_at = now
            old_status = prev["status"] if prev else None
            if status != old_status:
                detail = "; ".join(sig["reasons"]) if status in ("SUSPENDED", "MONITORING") else ""
                if status == "TERMINATED":
                    detail = f"killed: {sig['metrics']['last_kill_reason'] or 'unprofitable hours'}"
                events.append((pair_id, now, "STATUS", old_status, status, detail))
            elif status == "QUALIFIED" and prev and cohort != prev["cohort"]:
                events.append((pair_id, now, "COHORT", prev["cohort"], cohort, ""))
            rows.append((pair_id, asset, lg["venue"], lg["symbol"], sh["venue"], sh["symbol"], status, cohort,
                         sig["run_start"], qualified_at, round(sig["age_h"], 2), sig["neg_streak"], sig["apy_1x"],
                         sig["apy_max"], sig["max_lev"], json.dumps(sig["metrics"]), ",".join(sig["flags"]),
                         json.dumps(sig["reasons"]), now))
    summary = {"pairs": len(rows), "events": len(events), "status": dict(Counter(r[6] for r in rows)),
               "removed": len(set(prev_rows) - {r[0] for r in rows}),
               "candidates": sorted(candidates)}
    if not persist:
        return summary
    stale = [(pid,) for pid in set(prev_rows) - {r[0] for r in rows}]  # delisted / unmatched / renamed pairs
    with conn:
        conn.executemany("DELETE FROM signals WHERE pair_id=?", stale)
        conn.executemany("INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO signal_events (pair_id, ts, event, from_status, to_status, detail) "
                         "VALUES (?,?,?,?,?,?)", events)
    return summary
