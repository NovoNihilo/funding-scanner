"""Read-only dashboard, rendered from the database on every request.
Served on localhost only (DASHBOARD_HOST); open it through the SSH tunnel: deploy/dashboard.ps1.
Routes:  /                 the page (tabs: Opportunities, Watchlist, Health, Activity)
         /api/pair?id=...  JSON for one pair's detail panel (hourly series, legs, costs, flags explained)"""
import html
import json
import os
import re
import shutil
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.config import (APY_BASIS, DB_PATH, EMERGING_MIN_H, HOLD_HORIZON_H, KILL_CUSHION_H, KILL_LOSS_SHARE,
                        MAX_LEVERAGE_CAP, MIN_APY_1X, PRIMARY_SIZE, REQUIRED_VENUE, SIGNAL_LOOKBACK_DAYS,
                        TERMINATE_AFTER, TERMINATE_HARD_AFTER)
from src.db import connect
from src.funding import completeness, upcoming_settlements
from src.signals import _load, _normal_interval, build_periods, lifecycle, mark_anomalies, var_hourly

HOUR = 3600
KILL_RULE = (f"A run is killed when {TERMINATE_AFTER} or more unprofitable hours in a row lose more than "
             f"{KILL_LOSS_SHARE:.0%} of what it earned in the {KILL_CUSHION_H} hours before them, or after "
             f"{TERMINATE_HARD_AFTER} in a row whatever their size.")
VENUE = {"hyperliquid": ("HL", "Hyperliquid"), "lighter": ("LT", "Lighter"), "variational": ("VAR", "Variational")}
STATUS = {"QUALIFIED": "Qualified", "EMERGING": "Emerging", "SUSPENDED": "Suspended", "MONITORING": "Monitoring",
          "TERMINATED": "Terminated"}
STATUS_ORDER = {"QUALIFIED": 0, "EMERGING": 1, "SUSPENDED": 2, "MONITORING": 3, "TERMINATED": 4}
HIDDEN_FLAGS = {"ESTIMATED_DATA", "TOS_RISK"}  # true for every Variational pair: explained once, not per row
SZ = str(int(PRIMARY_SIZE))
APY_FACTOR = 8760 / 2 if APY_BASIS == "capital" else 8760  # hourly net -> APY at 1x
SPARK_H = 48
WATCH_ROWS = 60

FLAG_TEXT = {
    "DIVIDEND_UNKNOWN": "A stock or commodity leg trades on Lighter or a Hyperliquid builder market. Neither "
                        "publishes how dividends are handled, so a dividend can move price or funding unexpectedly.",
    "DIV_IMMINENT": "Variational has shortened this market's funding interval, which it does around a dividend. "
                    "Expect a one-off price adjustment.",
    "EARLY_APY": "Under 24h of comparable history, so the APY is the average so far, not the conservative 48h/7d rate.",
    "STRESS_DEFAULT": "No price history yet, so the leverage cap assumes a 30% adverse daily move.",
    "NO_DEPTH": f"The order book is too thin to fill ${PRIMARY_SIZE:,.0f} per leg.",
    "Q_HIP3_FEE_EST": "Hyperliquid builder-market fees vary by deployer; costs assume twice the base fee.",
    "Q_STALE_QUOTES": "Variational's quote is over 10 minutes old, so its cost estimate is doubled.",
    "Q_NO_QUOTES": "Variational returned no quote for this size.",
}
FLAG_PREFIX = {
    "DIV_EXCLUDED=": "{n} hour(s) inside a Variational dividend window are left out of the averages.",
    "ANOMALY_EXCLUDED=": "{n} extreme one-off hour(s) are left out of the averages.",
}
BLOCK_TEXT = {"REDUCE_ONLY": "is reduce-only: new positions can't be opened",
              "HIDDEN": "is hidden by the venue", "PREIPO": "is a pre-IPO market with no regular funding",
              "NO_FUNDING": "has no funding"}
REASON_RULES = [
    (r"^APY 1x ([\d.]+%) < (\d+%)$", "APY {0} is under the {1} minimum"),
    (r"^run APY 1x ([\d.]+%) < (\d+%)$", "APY so far ({0}) is under the {1} minimum"),
    (r"^conservative funding <= 0$", "Not profitable over 48h / 7d"),
    (r"^run funding <= 0$", "Current run isn't profitable"),
    (r"^consistency: positive share < (\d+%) \(48h\)$", "Profitable in under {0} of hours (48h)"),
    (r"^consistency: positive share < (\d+%) \(run\)$", "Profitable in under {0} of hours so far"),
    (r"^median hourly net <= 0 \(48h\)$", "The typical hour isn't profitable (48h)"),
    (r"^coverage (\d+%) < (\d+%) \((24h|run)\)$", "Only {0} of hours have data"),
    (r"^stale data$", "No fresh data for 3h+"),
    (r"^(hyperliquid|lighter|variational) (\w+)$", "{0} market {1}"),
]


# ---------------- data ----------------

def var_coverage(conn, now, hours, grace_s=300):
    """Variational settlements announced by our snapshots in the window vs settlements actually recorded.
    A settlement counts as expected only if a snapshot saw it coming, so start-up never shows false misses."""
    lo, hi = now - hours * HOUR, now - grace_s
    expected = {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT symbol, next_ts FROM var_snapshots WHERE next_ts > ? AND next_ts <= ?", (lo, hi))}
    got = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT symbol, ts, source FROM settlements WHERE venue='variational' AND ts > ? AND ts <= ?", (lo, hi))}
    captured = expected & set(got)
    return {"hours": hours, "expected": len(expected), "captured": len(captured),
            "late": sum(1 for k in captured if got[k].endswith("_late")),
            "missing": sorted(expected - captured, key=lambda k: (-k[1], k[0]))}


def _last_run(conn, job):
    return conn.execute("SELECT * FROM runs WHERE job=? ORDER BY id DESC LIMIT 1", (job,)).fetchone()


def health(conn, now, db_path=None):
    """Overall status (OK / WARN / FAIL), the issues behind it, and the numbers the page shows."""
    issues = []
    snap = conn.execute("SELECT MAX(captured_at) FROM var_snapshots").fetchone()[0]
    snap_age = now - snap if snap else None
    if snap_age is None or snap_age > 600:
        issues.append(("FAIL", "no Variational snapshots yet" if snap_age is None
                       else f"Variational snapshots are stale (last one {age(snap_age)} ago)"))
    sync = _last_run(conn, "sync")
    sync_age = now - sync["finished_at"] if sync else None
    if sync is None or sync_age > 2 * HOUR:
        issues.append(("FAIL", "no hourly sync has completed yet" if sync is None
                       else f"hourly sync has not completed for {age(sync_age)}"))
    elif not sync["ok"]:
        issues.append(("WARN", f"last sync had errors: {(sync['detail'] or '')[:160]}"))
    cov24, cov72 = var_coverage(conn, now, 24), var_coverage(conn, now, 72)
    if cov24["missing"]:
        issues.append(("WARN", f"{len(cov24['missing'])} Variational settlement(s) missed in the last 24h"))
    if cov24["late"]:
        issues.append(("INFO", f"{cov24['late']} Variational settlement(s) in 24h used the late fallback snapshot"))
    matched = {(r[0], r[1]) for r in conn.execute("SELECT venue, symbol FROM markets WHERE matched=1 AND active=1")}
    venues = {}
    for venue in ("hyperliquid", "lighter"):
        rep = [r for r in completeness(conn, venue, now - 7 * 86400) if (venue, r[0]) in matched]
        last = {r[0]: r[1] for r in conn.execute(
            "SELECT symbol, MAX(ts) FROM settlements WHERE venue=? GROUP BY symbol", (venue,))}
        stale = [s for v, s in matched if v == venue and (last.get(s) or 0) < now - 3 * HOUR]
        venues[venue] = {"markets": sum(1 for v, _ in matched if v == venue),
                         "gaps": sum(1 for r in rep if r[3] > 0), "stale": len(stale)}
        if stale:
            issues.append(("WARN", f"{len(stale)} {venue} market(s) have no settlement for 3h+"))
    starts = [r[0] for r in conn.execute("SELECT started_at FROM runs WHERE job='worker_start' ORDER BY id DESC")]
    restarts_24h = sum(1 for t in starts if t > now - 86400)
    if restarts_24h > 5:
        issues.append(("WARN", f"worker restarted {restarts_24h} times in 24h"))
    path = db_path or DB_PATH
    db_bytes = sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))
    try:
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
    except OSError:
        free = None
    if free is not None and free < 1 << 30:
        issues.append(("WARN", f"low disk space: {free / 2**30:.1f} GB free"))
    levels = {lvl for lvl, _ in issues}
    status = "FAIL" if "FAIL" in levels else ("WARN" if "WARN" in levels else "OK")
    upcoming = upcoming_settlements(conn, now)
    return {"status": status, "issues": issues, "snap_age": snap_age, "sync": sync, "sync_age": sync_age,
            "cov24": cov24, "cov72": cov72, "venues": venues, "next_settlement": upcoming[0] if upcoming else None,
            "uptime": now - starts[0] if starts else None, "restarts_24h": restarts_24h,
            "db_mb": db_bytes / 2**20, "free_gb": free / 2**30 if free is not None else None}


def _legs(conn, row):
    q = "SELECT * FROM markets WHERE venue=? AND symbol=?"
    return (conn.execute(q, (row["long_venue"], row["long_symbol"])).fetchone(),
            conn.execute(q, (row["short_venue"], row["short_symbol"])).fetchone())


def pair_series(conn, row, hours=168, cache=None):
    """Hourly series for one stored signal, built exactly like the signal engine (same lookback, held-forward
    Variational rate, dividend and anomaly exclusions). Returns (points, run_start, kills); each point is
    {t, net, ex, l, s} with net and per-leg rates per hour (None = missing)."""
    cache = {} if cache is None else cache
    now = row["updated_at"]
    since = int(now - SIGNAL_LOOKBACK_DAYS * 86400 - 8 * HOUR)
    lg, sh = _legs(conn, row)
    if lg is None or sh is None:
        return [], None, []

    def rates(m):
        key = (m["venue"], m["symbol"])
        if key not in cache:
            cache[key] = _load(conn, m["venue"], m["symbol"], since)
        return cache[key]

    lr, sr = rates(lg), rates(sh)
    var_leg = "long" if lg["venue"] == "variational" else ("short" if sh["venue"] == "variational" else None)
    normal = None
    if var_leg:
        vm, vr = (lg, lr) if var_leg == "long" else (sh, sr)
        normal = _normal_interval(vr) if vm["asset_class"] == "tradfi" else None
    periods = [p for p in build_periods(lr, sr, var_leg, normal) if p["end"] <= now]
    mark_anomalies(periods)
    run_start, _, kills, _, _ = lifecycle(periods)

    def leg_rate(r, is_var, t):
        if is_var:
            keys = sorted(r)
            v = var_hourly(r, keys, t) if keys else None
            return v[0] if v else None
        return r[t][0] if t in r else None

    lo = now - hours * HOUR
    pts = []
    for p in periods:
        if p["end"] <= lo:
            continue
        ex = "dividend" if "DIVIDEND" in p["tags"] else ("anomaly" if "ANOMALY" in p["tags"] else None)
        pts.append({"t": p["end"], "net": p["net"], "ex": ex,
                    "l": leg_rate(lr, var_leg == "long", p["end"]), "s": leg_rate(sr, var_leg == "short", p["end"])})
    return pts, run_start, [k for k in kills if k > lo]


def flag_notes(flags):
    out = []
    for f in flags:
        if not f or f in HIDDEN_FLAGS:
            continue
        text = FLAG_TEXT.get(f)
        for prefix, tmpl in FLAG_PREFIX.items():
            if f.startswith(prefix):
                text = tmpl.format(n=f[len(prefix):])
        m = re.match(r"^(HYP|LIG|VAR)_(\w+)$", f)
        if m and m.group(2) in BLOCK_TEXT:
            text = f"The {dict(HYP='Hyperliquid', LIG='Lighter', VAR='Variational')[m.group(1)]} market " \
                   f"{BLOCK_TEXT[m.group(2)]}."
        out.append({"code": f, "text": text or f})
    return out


def humanize(reason):
    for pat, tmpl in REASON_RULES:
        m = re.match(pat, reason)
        if m:
            g = list(m.groups())
            if tmpl.startswith("{0} market"):
                g = [VENUE.get(g[0], (g[0], g[0].title()))[1], BLOCK_TEXT.get(g[1], g[1].lower().replace("_", " "))]
            return tmpl.format(*g)
    return reason


def leverage_note(row, lg, sh, m):
    venue_caps = [v for v in ((lg["max_leverage"] if lg else None), (sh["max_leverage"] if sh else None)) if v]
    venue = min(venue_caps) if venue_caps else None
    lev = int(row["max_lev"] or 1)
    move = m.get("stress_move")
    if m.get("stress_lev") is not None and lev >= int(m["stress_lev"]) and (venue is None or m["stress_lev"] < venue) \
            and m["stress_lev"] < MAX_LEVERAGE_CAP:
        return (f"Capped at {lev}x by the stress test: neither leg may be liquidated by a {move:.0%} adverse move in "
                f"24h (the 99th-percentile daily move plus a 25% buffer), rebalancing once a day.")
    if venue is not None and venue <= MAX_LEVERAGE_CAP:
        return f"Capped at {lev}x by the venues' own maximum leverage."
    return f"Capped at {lev}x by the scanner's {MAX_LEVERAGE_CAP:.0f}x ceiling."


def pair_detail(conn, pair_id):
    row = conn.execute("SELECT * FROM signals WHERE pair_id=?", (pair_id,)).fetchone()
    if row is None:
        return None
    lg, sh = _legs(conn, row)
    m = json.loads(row["metrics"] or "{}")
    pts, run_start, kills = pair_series(conn, row)
    cls = (lg["asset_class"] if lg else None) or (sh["asset_class"] if sh else None)
    return {
        "pair_id": pair_id, "asset": row["asset"], "class": cls, "status": row["status"],
        "status_label": STATUS.get(row["status"], row["status"]), "cohort": row["cohort"], "age_h": row["age_h"],
        "long": {"venue": row["long_venue"], "badge": VENUE[row["long_venue"]][0],
                 "name": VENUE[row["long_venue"]][1], "symbol": row["long_symbol"]},
        "short": {"venue": row["short_venue"], "badge": VENUE[row["short_venue"]][0],
                  "name": VENUE[row["short_venue"]][1], "symbol": row["short_symbol"]},
        "apy_1x": row["apy_1x"], "apy_max": row["apy_max"], "max_lev": row["max_lev"], "metrics": m,
        "flags": flag_notes((row["flags"] or "").split(",")),
        "reasons": [humanize(r) for r in json.loads(row["reasons"] or "[]")],
        "leverage_note": leverage_note(row, lg, sh, m), "series": pts, "run_start": run_start, "kills": kills,
        "updated_at": row["updated_at"], "apy_factor": APY_FACTOR, "min_apy": MIN_APY_1X,
        "kill_rule": KILL_RULE, "hold_h": HOLD_HORIZON_H,
    }


# ---------------- formatting ----------------

def esc(x):
    return html.escape("" if x is None else str(x))


def age(s):
    if s is None:
        return "never"
    s = int(s)
    return f"{s}s" if s < 120 else (f"{s // 60}m" if s < 7200 else (f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"))


def until(s):
    if s is None or s < 0:
        return "n/a"
    h, rem = divmod(int(s), 3600)
    return f"{h}h {rem // 60}m" if h else f"{rem // 60}m"


def pct(x, digits=1):
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def usd(x):
    return "n/a" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def hrs(h):
    return "never" if h is None else (f"{h:.0f}h" if h < 1000 else ">999h")


def ts_tag(t):
    if not t:
        return "n/a"
    return f'<time data-ts="{int(t)}">{time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))}</time>'


def num(x, default=-1e18):
    return default if x is None else x


def badge(venue):
    short, name = VENUE.get(venue, (venue, venue))
    return f'<abbr class="vb vb-{esc(short.lower())}" title="{esc(name)}">{esc(short)}</abbr>'


def sparkline(pts, hours=SPARK_H):
    """Inline SVG of the last `hours` hourly nets: bars up (earning) / down (paying), gaps where data is missing."""
    pts = pts[-hours:]
    vals = [abs(p["net"]) for p in pts if p["net"] is not None and not p["ex"]]
    top = max(vals) if vals else 0
    w, h, bw = hours * 3, 24, 2
    if not pts or not top:
        return f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true"><line x1="0" y1="12" x2="{w}" y2="12"/></svg>'
    x0 = w - len(pts) * 3
    bars, pos = [], 0
    for i, p in enumerate(pts):
        x = x0 + i * 3
        if p["net"] is None:
            bars.append(f'<rect class="sk-miss" x="{x}" y="11.5" width="{bw}" height="1"/>')
            continue
        v = max(-1.0, min(1.0, p["net"] / top))
        bh = max(1.0, abs(v) * 11)
        cls = "sk-ex" if p["ex"] else ("sk-pos" if v > 0 else "sk-neg")
        pos += v > 0
        y = 12 - bh if v > 0 else 12
        bars.append(f'<rect class="{cls}" x="{x}" y="{y:.1f}" width="{bw}" height="{bh:.1f}"/>')
    label = f"Last {len(pts)} hours: profitable in {pos}"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            f'<line class="sk-zero" x1="0" y1="12" x2="{w}" y2="12"/>{"".join(bars)}</svg>')


# ---------------- page sections ----------------

def trade_cell(r):
    return (f'<div class="trade"><span class="side">Long</span>{badge(r["long_venue"])}'
            f'<span class="sym">{esc(r["long_symbol"])}</span>'
            f'<span class="side">Short</span>{badge(r["short_venue"])}<span class="sym">{esc(r["short_symbol"])}</span></div>')


TREND = {"UP": ("&#9650;", "Rising", "t-up"), "DOWN": ("&#9660;", "Falling", "t-down"),
         "STABLE": ("&#9644;", "Stable", "t-flat")}


def trend_cell(m):
    t = TREND.get(m.get("trend"))
    if not t:
        return '<td data-v="-1e18" class="c-trend"><span class="muted">Not enough data</span></td>'
    d = m.get("trend_delta_apy") or 0
    return (f'<td data-v="{d}" class="c-trend"><span class="{t[2]}"><span aria-hidden="true">{t[0]}</span> {t[1]}</span>'
            f'<span class="sub">{d * 100:+.1f} pts</span></td>')


def pair_rows(rows, classes, series, with_reasons):
    out = []
    for r in rows:
        m = json.loads(r["metrics"] or "{}")
        c = (m.get("costs") or {}).get(SZ, {})
        ok = c.get("status") == "OK"
        cls = classes.get(r["asset"], "unknown")
        lev = int(r["max_lev"] or 1)
        text = " ".join([r["asset"], r["long_symbol"], r["short_symbol"], r["long_venue"], r["short_venue"]]).lower()
        payback = (f'<b>{hrs(c.get("payback_h_maker"))}</b> maker<span class="sub">{hrs(c.get("payback_h"))} taker</span>'
                   if ok else f'<span class="muted">{"Costs pending" if c.get("status") in (None, "MISSING") else "Thin book"}</span>')
        cells = [
            f'<td class="c-asset" data-v="{esc(r["asset"])}"><button class="open" data-pair="{esc(r["pair_id"])}">'
            f'{esc(r["asset"])}</button><span class="sub">{"RWA" if cls == "tradfi" else "Crypto"}</span></td>',
            f'<td class="c-trade" data-v="{esc(r["long_venue"])}">{trade_cell(r)}</td>',
            f'<td class="c-apy n" data-v="{num(r["apy_1x"])}"><b>{pct(r["apy_1x"])}</b>'
            f'<span class="sub">up to {pct(r["apy_max"])} at {lev}x</span></td>',
            trend_cell(m),
            f'<td class="c-pay n" data-v="{num(c.get("payback_h_maker"), 1e18) if ok else 1e18}">{payback}</td>',
            f'<td class="c-net n" data-v="{num(c.get("net_hold_usd"))}">{usd(c.get("net_hold_usd"))}</td>',
            f'<td class="c-age n" data-v="{r["age_h"] or 0}">{r["age_h"] or 0:.0f}h'
            f'<span class="sub">{esc((r["cohort"] or "").title() if r["status"] in ("QUALIFIED", "EMERGING", "SUSPENDED") else "")}</span></td>',
            f'<td class="c-spark">{sparkline(series.get(r["pair_id"], []))}</td>',
        ]
        if with_reasons:
            reasons = [humanize(x) for x in json.loads(r["reasons"] or "[]")]
            if not reasons and r["status"] == "MONITORING" and (r["age_h"] or 0) < EMERGING_MIN_H:
                reasons = [f"Run is {r['age_h'] or 0:.0f}h old; it can emerge at {EMERGING_MIN_H:.0f}h"]
            cells.append(f'<td class="c-why"><span class="chip s-{esc(r["status"].lower())}">'
                         f'{esc(STATUS.get(r["status"], r["status"]))}</span>'
                         f'<span class="why">{esc(reasons[0] if reasons else "")}</span></td>')
        out.append(f'<tr data-pair="{esc(r["pair_id"])}" data-class="{esc(cls)}" data-text="{esc(text)}">'
                   + "".join(cells) + "</tr>")
    return "".join(out)


def pair_table(rows, classes, series, empty, with_reasons=False):
    if not rows:
        return f'<p class="empty">{empty}</p>'
    heads = ["Asset", "Trade", "APY at 1x", "Trend (12h vs 24h)", "Payback of entry + exit", f"Net {HOLD_HORIZON_H / 24:.0f}d",
             "Run age", f"Last {SPARK_H}h"] + (["Why not yet"] if with_reasons else [])
    cls = ["c-asset", "c-trade", "c-apy n", "c-trend", "c-pay n", "c-net n", "c-age n", "c-spark"] + \
          (["c-why"] if with_reasons else [])
    th = "".join(f'<th class="{c}" scope="col"><button class="sort" data-col="{i}">{h}</button></th>'
                 if h != f"Last {SPARK_H}h" else f'<th class="{c}" scope="col">{h}</th>'
                 for i, (h, c) in enumerate(zip(heads, cls)))
    return (f'<div class="tablewrap"><table class="pairs"><thead><tr>{th}</tr></thead>'
            f'<tbody>{pair_rows(rows, classes, series, with_reasons)}</tbody></table></div>')


def health_tab(h, warming):
    icon = {"FAIL": "&#10007;", "WARN": "!", "INFO": "i"}
    items = "".join(f'<li class="i-{lvl.lower()}"><span class="ic" aria-hidden="true">{icon[lvl]}</span>'
                    f'<b>{dict(FAIL="Failing", WARN="Warning", INFO="Note")[lvl]}</b> {esc(text)}</li>'
                    for lvl, text in h["issues"])
    if not items:
        items = '<li class="i-ok"><span class="ic" aria-hidden="true">&#10003;</span><b>Healthy</b> All collectors are running and up to date.</li>'
    c24, c72, v = h["cov24"], h["cov72"], h["venues"]
    sync = h["sync"]
    detail = json.loads(sync["detail"] or "{}") if sync else {}
    took = (sync["finished_at"] - sync["started_at"]) if sync else None
    repairs = sum((detail.get(k) or {}).get("gap_repairs", 0) for k in ("hl", "lt"))
    nxt = h["next_settlement"]
    groups = [
        ("Variational", [("Last snapshot", f"{age(h['snap_age'])} ago"),
                         ("Next settlement", f"in {until(nxt - int(time.time()))}" if nxt else "n/a"),
                         ("Settlements captured, 24h", f"{c24['captured']} of {c24['expected']}"),
                         ("Settlements captured, 72h", f"{c72['captured']} of {c72['expected']}"),
                         ("Late fallbacks, 24h", str(c24["late"]))]),
        ("Hyperliquid and Lighter", [("Last sync", f"{age(h['sync_age'])} ago, took {age(took)}"),
                                     ("Hyperliquid markets", f"{v['hyperliquid']['markets']}, {v['hyperliquid']['gaps']} with gaps"),
                                     ("Lighter markets", f"{v['lighter']['markets']}, {v['lighter']['gaps']} with gaps"),
                                     ("Gaps repaired, last sync", str(repairs))]),
        ("Server", [("Worker uptime", age(h["uptime"])), ("Restarts, 24h", str(h["restarts_24h"])),
                    ("Database", f"{h['db_mb']:.0f} MB"),
                    ("Disk free", "n/a" if h["free_gb"] is None else f"{h['free_gb']:.1f} GB")]),
    ]
    dl = "".join(f'<div class="group"><h3>{g}</h3><dl>' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(val)}</dd>"
                                                                  for k, val in rows) + "</dl></div>"
                 for g, rows in groups)
    miss = ""
    if c72["missing"]:
        li = "".join(f"<li>{esc(s)} at {ts_tag(t)}</li>" for s, t in c72["missing"][:30])
        miss = (f'<details class="misses"><summary>{len(c72["missing"])} missed Variational settlement(s) in 72h'
                f'</summary><ul>{li}</ul></details>')
    warm = ""
    if warming:
        b, best = Counter(), 0
        for r in warming:
            hh = json.loads(r["metrics"] or "{}").get("history_hours")
            if hh is None:
                b["Not evaluated yet"] += 1
                continue
            best = max(best, hh)
            b["No history" if hh <= 0 else ("1-7 hours" if hh < 8 else "8-23 hours")] += 1
        rows = "".join(f"<dt>{k}</dt><dd>{b[k]} pairs</dd>" for k in ("No history", "1-7 hours", "8-23 hours",
                                                                       "Not evaluated yet") if b[k])
        warm = (f'<div class="group"><h3>Pairs still building history</h3><p class="note">A pair shows an early APY '
                f'after 8 hours of comparable history and its conservative APY after 24. The furthest along has '
                f'{best:.0f} hours.</p><dl>{rows}</dl></div>')
    return (f'<ul class="issues">{items}</ul><div class="groups">{dl}{warm}</div>{miss}')


def activity_tab(conn, live):
    rows = [e for e in conn.execute("SELECT * FROM signal_events WHERE from_status IS NOT NULL ORDER BY id DESC "
                                    "LIMIT 600") if e["pair_id"] in live][:60]
    if not rows:
        return '<p class="empty">No status changes yet. They appear here as pairs emerge, qualify or get killed.</p>'
    out = []
    for e in rows:
        asset, _, legs = e["pair_id"].partition("|")
        lg, _, sh = legs.partition(">")
        lv, _, ls = lg.partition(":")
        sv, _, ss = sh.partition(":")
        to = e["to_status"]
        detail = "; ".join(humanize(x) for x in (e["detail"] or "").split("; ") if x)
        if to == "TERMINATED":
            why = (e["detail"] or "").removeprefix("killed: ")
            detail = "Killed: " + (f"{TERMINATE_AFTER} unprofitable hours in a row"
                                   if why == "3 consecutive non-positive hours" else why)
        out.append(f'<tr data-pair="{esc(e["pair_id"])}"><td class="when">{ts_tag(e["ts"])}</td>'
                   f'<td><button class="open" data-pair="{esc(e["pair_id"])}">{esc(asset)}</button></td>'
                   f'<td><div class="trade"><span class="side">Long</span>{badge(lv)}<span class="sym">{esc(ls)}</span>'
                   f'<span class="side">Short</span>{badge(sv)}<span class="sym">{esc(ss)}</span></div></td>'
                   f'<td>{esc(STATUS.get(e["from_status"], e["from_status"]))} to '
                   f'<span class="chip s-{esc(to.lower())}">{esc(STATUS.get(to, to))}</span></td>'
                   f'<td class="muted">{esc(detail)}</td></tr>')
    return ('<div class="tablewrap"><table class="events"><thead><tr><th scope="col">When</th><th scope="col">Asset</th>'
            '<th scope="col">Trade</th><th scope="col">Change</th><th scope="col">Detail</th></tr></thead>'
            f'<tbody>{"".join(out)}</tbody></table></div>')


def build_page(conn, now=None, db_path=None):
    now = int(now or time.time())
    sigs = conn.execute("SELECT * FROM signals").fetchall()
    classes = {r[0]: r[1] for r in conn.execute(
        "SELECT asset, MAX(asset_class) FROM markets WHERE matched=1 AND active=1 GROUP BY asset")}
    asof = max((r["updated_at"] for r in sigs), default=None)
    qualified = sorted((r for r in sigs if r["status"] == "QUALIFIED"),
                       key=lambda r: -num(json.loads(r["metrics"] or "{}").get("rank_net_hold_usd")))
    emerging = sorted((r for r in sigs if r["status"] == "EMERGING"), key=lambda r: -num(r["apy_1x"]))
    watch = sorted((r for r in sigs if r["status"] not in ("QUALIFIED", "EMERGING") and (r["apy_1x"] or 0) > 0),
                   key=lambda r: -r["apy_1x"])[:WATCH_ROWS]
    warming = [r for r in sigs if r["apy_1x"] is None and r["status"] != "TERMINATED"]
    cache, series = {}, {}
    for r in qualified + emerging + watch:
        series[r["pair_id"]] = pair_series(conn, r, SPARK_H, cache)[0]
    h = health(conn, now, db_path)
    hl = {"OK": ("ok", "&#10003;", "Healthy"), "WARN": ("warn", "!", "Needs attention"),
          "FAIL": ("fail", "&#10007;", "Failing")}[h["status"]]
    best = qualified[0] if qualified else None
    if best:
        lead = (f"{len(qualified)} qualified and {len(emerging)} emerging. The top qualified pair is "
                f"<b>{esc(best['asset'])}</b> at {pct(best['apy_1x'])} APY at 1x.")
    elif emerging:
        lead = f"No qualified pairs yet, {len(emerging)} emerging. Pairs qualify after 24 profitable hours."
    else:
        lead = "Nothing qualifies right now. Pairs appear here after 8 profitable hours and qualify after 24."
    tos = ("Every pair has one leg on Variational, whose terms of use (section 4.A.xv(6)) prohibit arbitrage between "
           "Variational and other platforms. Variational rates are estimated from live snapshots. "
           "This page is read-only and isn't trading advice." if REQUIRED_VENUE == "variational" else
           "This page is read-only and isn't trading advice.")
    opp = (f'<p class="lead">{lead}</p>'
           f'<section aria-labelledby="h-q"><div class="sechead"><h2 id="h-q">Qualified</h2><p>Profitable for 24 hours or '
           f'more and passing every check. Ranked by expected net over {HOLD_HORIZON_H / 24:.0f} days at '
           f'${PRIMARY_SIZE:,.0f} per leg after taker costs.</p></div>'
           + pair_table(qualified, classes, series, "No qualified pairs right now.") + '</section>'
           f'<section aria-labelledby="h-e"><div class="sechead"><h2 id="h-e">Emerging</h2><p>Profitable for 8 to 24 '
           f'hours and passing the same checks on the run so far. Early numbers move more; consider a smaller size.'
           f'</p></div>' + pair_table(emerging, classes, series, "No emerging pairs right now.") + '</section>'
           f'<p class="fine">{tos}</p>')
    wl = (f'<div class="sechead"><h2>Watchlist</h2><p>The {len(watch)} highest-APY profitable-direction pairs that '
          f'aren\'t qualified or emerging yet, with what is holding each one back.</p></div>'
          + pair_table(watch, classes, series, "No other pairs have an APY yet.", with_reasons=True))
    tabs = [("opportunities", "Opportunities", len(qualified) + len(emerging)), ("watchlist", "Watchlist", len(watch)),
            ("health", "Health", None), ("activity", "Activity", None)]
    tablist = "".join(f'<button role="tab" id="t-{k}" aria-controls="tab-{k}" data-tab="{k}" aria-selected="false">'
                      f'{label}{f" <span class=count>{n}</span>" if n is not None else ""}</button>'
                      for k, label, n in tabs)
    body = f"""
<header class="top">
  <div class="brand"><h1>Funding scanner</h1>
    <p>Signals for the hour ending {ts_tag(asof)} &#8211; updated hourly</p></div>
  <button class="health-pill hp-{hl[0]}" data-goto="health"><span class="ic" aria-hidden="true">{hl[1]}</span>{hl[2]}</button>
</header>
<nav class="bar">
  <div role="tablist" aria-label="Sections">{tablist}</div>
  <div class="filters"><label class="visually-hidden" for="q">Filter</label>
    <input id="q" type="search" placeholder="Filter by asset or market" autocomplete="off">
    <label class="visually-hidden" for="cls">Asset class</label>
    <select id="cls"><option value="">All assets</option><option value="crypto">Crypto</option><option value="tradfi">RWA</option></select>
  </div>
</nav>
<main>
  <div role="tabpanel" id="tab-opportunities" aria-labelledby="t-opportunities">{opp}</div>
  <div role="tabpanel" id="tab-watchlist" aria-labelledby="t-watchlist" hidden>{wl}</div>
  <div role="tabpanel" id="tab-health" aria-labelledby="t-health" hidden><div class="sechead"><h2>Health</h2>
    <p>Whether every collector is running and the data is complete.</p></div>{health_tab(h, warming)}</div>
  <div role="tabpanel" id="tab-activity" aria-labelledby="t-activity" hidden><div class="sechead"><h2>Activity</h2>
    <p>Status changes on tracked pairs, newest first.</p></div>{activity_tab(conn, {r["pair_id"] for r in sigs})}</div>
</main>
<div class="scrim" hidden></div>
<aside class="drawer" aria-label="Pair detail" aria-hidden="true" tabindex="-1">
  <button class="close" aria-label="Close detail">&#10005;</button><div class="drawer-body"></div>
</aside>"""
    return PAGE.replace("{{BODY}}", body)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Funding Scanner</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--paper:#f3f5f8;--surface:#fff;--ink:#161a21;--ink2:#4b5563;--muted:#6b7380;--rule:#e3e7ec;--rule2:#eef1f4;
--pos:#2a78d6;--neg:#e34948;--ex:#b8bfc9;--good:#0e8a0e;--warn:#a86a00;--fail:#c8322f;--focus:#2a78d6;
--goodbg:#e6f4e6;--warnbg:#fbf1dc;--failbg:#fbe7e6;--chip:#eef1f4}
@media (prefers-color-scheme:dark){:root{--paper:#141920;--surface:#1b212b;--ink:#e9edf2;--ink2:#b4bcc8;--muted:#8a93a1;
--rule:#2a313d;--rule2:#232a35;--pos:#3987e5;--neg:#e66767;--ex:#4a5260;--good:#4cc76a;--warn:#e7b04a;--fail:#ff7a6b;
--focus:#3987e5;--goodbg:#173022;--warnbg:#2f2614;--failbg:#351b1c;--chip:#262d39}}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
 font-variant-numeric:tabular-nums}
button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.top,.bar,main{max-width:1320px;margin:0 auto;padding-left:24px;padding-right:24px}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-top:22px;padding-bottom:6px}
.brand h1{margin:0;font:600 22px/1.2 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;letter-spacing:-.01em}
.brand p{margin:2px 0 0;color:var(--muted);font-size:13px}
.health-pill{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--rule);background:var(--surface);
 border-radius:999px;padding:6px 14px 6px 8px;cursor:pointer;font-size:14px;font-weight:500}
.health-pill .ic{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;font-size:12px;font-weight:700}
.hp-ok .ic{background:var(--goodbg);color:var(--good)}.hp-warn .ic{background:var(--warnbg);color:var(--warn)}
.hp-fail .ic{background:var(--failbg);color:var(--fail)}.hp-fail{border-color:var(--fail)}
.bar{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;border-bottom:1px solid var(--rule);
 position:sticky;top:0;background:var(--paper);z-index:5;padding-top:10px}
[role=tablist]{display:flex;gap:4px;overflow-x:auto}
[role=tab]{background:none;border:0;border-bottom:2px solid transparent;padding:10px 12px 11px;cursor:pointer;
 color:var(--ink2);font-weight:500;white-space:nowrap}
[role=tab][aria-selected=true]{color:var(--ink);border-bottom-color:var(--ink)}
.count{display:inline-block;min-width:22px;padding:0 6px;margin-left:4px;border-radius:999px;background:var(--chip);
 font-size:12px;text-align:center;color:var(--ink2)}
.filters{display:flex;gap:8px;padding-bottom:8px}
input,select{font:inherit;font-size:14px;padding:7px 11px;border:1px solid var(--rule);border-radius:8px;background:var(--surface);color:var(--ink)}
input{width:240px}
main{padding-top:20px;padding-bottom:60px}
.lead{font-size:17px;margin:0 0 22px;max-width:72ch}
section{margin-bottom:34px}
.sechead{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 16px;margin-bottom:10px}
.sechead h2{margin:0;font:600 18px/1.3 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif}
.sechead p{margin:0;color:var(--muted);font-size:14px;max-width:80ch}
.tablewrap{background:var(--surface);border:1px solid var(--rule);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%}
th{text-align:left;font:500 13px/1.3 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;color:var(--muted);
 padding:10px 14px;border-bottom:1px solid var(--rule);white-space:nowrap;vertical-align:bottom}
th .sort{background:none;border:0;padding:0;cursor:pointer;color:inherit;font:inherit}
th .sort[data-dir]::after{content:" \\25BE"}th .sort[data-dir=asc]::after{content:" \\25B4"}
td{padding:11px 14px;border-bottom:1px solid var(--rule2);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr[data-pair]{cursor:pointer}tbody tr[data-pair]:hover{background:var(--rule2)}
.n{text-align:right}th.n{text-align:right}
.sub{display:block;color:var(--muted);font-size:12.5px;font-weight:400}
.open{background:none;border:0;padding:0;cursor:pointer;font:600 16px/1.2 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif}
.trade{display:grid;grid-template-columns:auto auto 1fr;gap:2px 8px;align-items:center;font-size:14px;white-space:nowrap}
.side{color:var(--muted);font-size:12.5px}
.vb{text-decoration:none;font:600 11px/1 "IBM Plex Sans Condensed",sans-serif;padding:3px 5px;border-radius:4px;
 background:var(--chip);color:var(--ink2);text-align:center;min-width:30px}
.vb-var{background:var(--ink);color:var(--surface)}
.c-apy{white-space:nowrap}.c-apy b{font-size:16px;font-weight:600}
.t-up{color:var(--pos)}.t-down{color:var(--neg)}.t-flat,.muted{color:var(--muted)}
.c-trend{white-space:nowrap}
.spark{width:144px;height:24px;display:block}
.spark line{stroke:var(--rule);stroke-width:1}.sk-zero{stroke:var(--muted)!important;stroke-opacity:.5}
.sk-pos{fill:var(--pos)}.sk-neg{fill:var(--neg)}.sk-ex{fill:var(--ex)}.sk-miss{fill:var(--ex)}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:500;background:var(--chip);color:var(--ink2)}
.s-qualified{background:var(--goodbg);color:var(--good)}.s-suspended{background:var(--warnbg);color:var(--warn)}
.s-terminated{background:var(--failbg);color:var(--fail)}
.why{display:block;color:var(--ink2);font-size:13px;margin-top:3px;min-width:220px}
.empty{background:var(--surface);border:1px dashed var(--rule);border-radius:10px;padding:18px;color:var(--muted);margin:0}
.fine{color:var(--muted);font-size:13px;max-width:90ch}
.issues{list-style:none;margin:0 0 22px;padding:0;background:var(--surface);border:1px solid var(--rule);border-radius:10px}
.issues li{padding:12px 16px;border-bottom:1px solid var(--rule2);display:flex;gap:10px;align-items:baseline}
.issues li:last-child{border-bottom:0}
.issues .ic{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;font-size:12px;font-weight:700;flex:none}
.i-ok .ic{background:var(--goodbg);color:var(--good)}.i-warn .ic{background:var(--warnbg);color:var(--warn)}
.i-fail .ic{background:var(--failbg);color:var(--fail)}.i-info .ic{background:var(--chip);color:var(--ink2)}
.groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.group{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:14px 16px}
.group h3{margin:0 0 8px;font:600 15px/1.3 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif}
.group dl{display:grid;grid-template-columns:1fr auto;gap:6px 12px;margin:0;font-size:14px}
.group dt{color:var(--ink2)}.group dd{margin:0;text-align:right;font-weight:500}
.note{color:var(--muted);font-size:13px;margin:0 0 8px}
.misses{margin-top:16px;background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:12px 16px}
.misses summary{cursor:pointer;font-weight:500}
.events td.when{white-space:nowrap;color:var(--ink2)}
.scrim{position:fixed;inset:0;background:rgba(20,25,32,.35);z-index:20}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(680px,100%);background:var(--surface);z-index:21;
 box-shadow:-12px 0 32px rgba(20,25,32,.18);transform:translateX(102%);transition:transform .2s ease;overflow-y:auto}
.drawer.is-open{transform:none}
.drawer .close{position:absolute;top:14px;right:14px;width:36px;height:36px;border:1px solid var(--rule);border-radius:50%;
 background:var(--surface);cursor:pointer}
.drawer-body{padding:24px 26px 40px}
.d-head h2{margin:0 0 4px;font:600 26px/1.1 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif}
.d-head .trade{font-size:15px;margin:10px 0 4px}
.d-figs{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--rule);border:1px solid var(--rule);
 border-radius:10px;overflow:hidden;margin:18px 0}
.d-figs div{background:var(--surface);padding:10px 12px}.d-figs span{display:block;color:var(--muted);font-size:12.5px}
.d-figs b{font-size:17px;font-weight:600}
.d-sec{margin-top:24px}.d-sec p{margin:0;color:var(--ink2);font-size:14px}.d-sec h3{margin:0 0 8px;font:600 15px/1.3 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}
.chart .axis{fill:var(--muted);font:11px "IBM Plex Sans",sans-serif}
.chart .zero{stroke:var(--muted);stroke-opacity:.6}.chart .grid{stroke:var(--rule2)}
.chart .min{stroke:var(--ink2);stroke-dasharray:4 4;stroke-opacity:.7}
.chart .pos{fill:var(--pos)}.chart .neg{fill:var(--neg)}.chart .ex{fill:var(--ex)}.chart .miss{fill:var(--ex);opacity:.6}
.chart .run{stroke:var(--ink);stroke-width:1.5}.chart .kill{fill:var(--fail)}
.chart .cross{stroke:var(--ink2);stroke-width:1}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--ink2);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.tip{position:absolute;pointer-events:none;background:var(--ink);color:var(--surface);font-size:12.5px;padding:8px 10px;
 border-radius:8px;white-space:nowrap;transform:translate(-50%,-100%);margin-top:-8px;z-index:2}
.kv{display:grid;grid-template-columns:1fr auto auto;gap:6px 16px;font-size:14px}
.kv .h{color:var(--muted);font-size:12.5px}.kv .r{text-align:right}
.flags{list-style:none;margin:0;padding:0}.flags li{padding:8px 0;border-bottom:1px solid var(--rule2);font-size:14px}
.flags code{font:500 12px "IBM Plex Sans Condensed",sans-serif;color:var(--muted);display:block}
details.tbl summary{cursor:pointer;color:var(--ink2);font-size:13px;margin-top:8px}
details.tbl table{font-size:13px}details.tbl td,details.tbl th{padding:5px 8px}
@media (max-width:820px){.top,.bar,main{padding-left:16px;padding-right:16px}.filters{width:100%}input{flex:1;width:auto}
 .c-trend,.c-net,.c-age,.c-spark{display:none}.d-figs{grid-template-columns:repeat(2,1fr)}.drawer-body{padding:20px 16px 40px}}
@media (prefers-reduced-motion:reduce){.drawer{transition:none}}
</style></head><body>{{BODY}}
<script>
(function(){
var $=function(s,el){return (el||document).querySelector(s)},$$=function(s,el){return [].slice.call((el||document).querySelectorAll(s))};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function local(ts,opt){return new Date(ts*1000).toLocaleString([],opt||{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
$$('time[data-ts]').forEach(function(t){t.title=t.textContent;t.textContent=local(+t.dataset.ts)});
function store(k,v){try{if(v===undefined)return sessionStorage.getItem(k);sessionStorage.setItem(k,v)}catch(e){return null}}
// tabs
function show(id){if(!$('#tab-'+id))id='opportunities';$$('[role=tab]').forEach(function(t){t.setAttribute('aria-selected',t.dataset.tab===id)});
 $$('[role=tabpanel]').forEach(function(p){p.hidden=p.id!=='tab-'+id});if(location.hash!=='#'+id)history.replaceState(null,'','#'+id)}
$$('[role=tab]').forEach(function(t){t.onclick=function(){show(t.dataset.tab)}});
$$('[data-goto]').forEach(function(b){b.onclick=function(){show(b.dataset.goto)}});
$('[role=tablist]').addEventListener('keydown',function(e){var tabs=$$('[role=tab]'),i=tabs.indexOf(document.activeElement);
 if(i<0)return;if(e.key==='ArrowRight'||e.key==='ArrowLeft'){var j=(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[j].focus();show(tabs[j].dataset.tab);e.preventDefault()}});
show(location.hash.slice(1));window.addEventListener('hashchange',function(){show(location.hash.slice(1))});
// filters
var q=$('#q'),cls=$('#cls');q.value=store('q')||'';cls.value=store('cls')||'';
function apply(){var t=q.value.toLowerCase().trim(),c=cls.value;store('q',q.value);store('cls',c);
 $$('tbody tr[data-class]').forEach(function(r){r.hidden=!((!c||r.dataset.class===c)&&r.dataset.text.indexOf(t)>=0)})}
q.oninput=apply;cls.onchange=apply;apply();
// sorting
$$('th .sort').forEach(function(b){b.onclick=function(e){e.stopPropagation();var tb=b.closest('table'),i=+b.dataset.col,
 dir=b.dataset.dir==='desc'?'asc':'desc';$$('th .sort',tb).forEach(function(x){delete x.dataset.dir});b.dataset.dir=dir;
 var body=tb.tBodies[0],rows=[].slice.call(body.rows),k=dir==='asc'?1:-1;
 rows.sort(function(a,c){var x=a.cells[i].dataset.v,y=c.cells[i].dataset.v,nx=parseFloat(x),ny=parseFloat(y);
  if(!isNaN(nx)&&!isNaN(ny))return (nx-ny)*k;return String(x||'').localeCompare(String(y||''))*k});
 rows.forEach(function(r){body.appendChild(r)})}});
// drawer
var drawer=$('.drawer'),scrim=$('.scrim'),dbody=$('.drawer-body'),lastFocus=null;
function closeDrawer(){drawer.classList.remove('is-open');drawer.setAttribute('aria-hidden','true');scrim.hidden=true;if(lastFocus)lastFocus.focus()}
$('.close',drawer).onclick=closeDrawer;scrim.onclick=closeDrawer;
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&drawer.classList.contains('is-open'))closeDrawer()});
document.addEventListener('click',function(e){var el=e.target.closest('[data-pair]');if(!el||e.target.closest('th'))return;
 lastFocus=e.target.closest('button')||el;openPair(el.dataset.pair)});
function openPair(id){dbody.innerHTML='<p class="muted">Loading...</p>';drawer.classList.add('is-open');drawer.setAttribute('aria-hidden','false');
 scrim.hidden=false;drawer.focus();fetch('/api/pair?id='+encodeURIComponent(id)).then(function(r){return r.json()}).then(render)
 .catch(function(){dbody.innerHTML='<p>Could not load this pair. Refresh the page and try again.</p>'})}
var STATUSCLS={QUALIFIED:'s-qualified',SUSPENDED:'s-suspended',TERMINATED:'s-terminated'};
function pct(x,d){return x==null?'n/a':(x*100).toFixed(d==null?1:d)+'%'}
function usd(x){return x==null?'n/a':(x<0?'-':'')+'$'+Math.abs(x).toFixed(2)}
function hrs(h){return h==null?'never':(h<1000?Math.round(h)+'h':'>999h')}
function legHtml(side,l){return '<span class="side">'+side+'</span><abbr class="vb vb-'+l.badge.toLowerCase()+'" title="'+esc(l.name)+'">'+esc(l.badge)+'</abbr><span class="sym">'+esc(l.symbol)+'</span>'}
function render(d){if(d.error){dbody.innerHTML='<p>'+esc(d.error)+'</p>';return}var m=d.metrics||{},c=(m.costs||{})['__SZ__']||{};
 var trend={UP:'Rising',DOWN:'Falling',STABLE:'Stable'}[m.trend]||'Not enough data';
 var h='<div class="d-head"><h2>'+esc(d.asset)+'</h2><span class="chip '+(STATUSCLS[d.status]||'')+'">'+esc(d.status_label)+'</span> '
  +'<span class="muted">'+(d.class==='tradfi'?'RWA':'Crypto')+(d.cohort?', '+esc(d.cohort.toLowerCase())+' run of '+Math.round(d.age_h)+'h':'')+'</span>'
  +'<div class="trade">'+legHtml('Long',d.long)+legHtml('Short',d.short)+'</div></div>';
 h+='<div class="d-figs"><div><span>APY at 1x</span><b>'+pct(d.apy_1x)+'</b></div><div><span>At '+(d.max_lev||1)+'x leverage</span><b>'+pct(d.apy_max)+'</b></div>'
  +'<div><span>Trend</span><b>'+trend+(m.trend_delta_apy!=null?' <small class="muted">'+(m.trend_delta_apy>=0?'+':'')+(m.trend_delta_apy*100).toFixed(1)+' pts</small>':'')+'</b></div>'
  +'<div><span>Last 24h, APY at 1x</span><b>'+pct(m.apy_now_1x)+'</b></div><div><span>Conservative (48h/7d)</span><b>'+pct(m.conservative_hourly==null?null:m.conservative_hourly*d.apy_factor)+'</b></div>'
  +'<div><span>Profitable hours, 48h</span><b>'+pct(m.pos_share_48h,0)+'</b></div></div>';
 h+='<div class="d-sec"><h3>Hourly net funding, last 7 days</h3><div class="chart" id="chart"></div>'
  +'<div class="legend"><span><i style="background:var(--pos)"></i>Earning</span><span><i style="background:var(--neg)"></i>Paying</span>'
  +'<span><i style="background:var(--ex)"></i>Left out (dividend window or outlier)</span><span><i style="background:var(--ink);width:2px"></i>Current run started</span>'
  +'<span><i style="background:var(--fail);border-radius:50%"></i>Run killed</span><span>Dashed line: '+pct(d.min_apy,0)+' minimum</span></div>'+tableView(d)+'</div>';
 h+='<div class="d-sec"><h3>Costs to enter and exit</h3>'+costs(m)+'</div>';
 h+='<div class="d-sec"><h3>Leverage</h3><p>'+esc(d.leverage_note)+'</p></div>';
 if(d.reasons.length&&d.status!=='QUALIFIED'&&d.status!=='EMERGING')h+='<div class="d-sec"><h3>Why it isn\\'t qualified</h3><ul class="flags">'+d.reasons.map(function(r){return '<li>'+esc(r)+'</li>'}).join('')+'</ul></div>';
 if(d.flags.length)h+='<div class="d-sec"><h3>Things to know</h3><ul class="flags">'+d.flags.map(function(f){return '<li>'+esc(f.text)+'<code>'+esc(f.code)+'</code></li>'}).join('')+'</ul></div>';
 h+='<p class="fine" style="margin-top:24px">Each hour pairs the Hyperliquid or Lighter hourly rate with the last recorded Variational settlement, held until the next one. '
  +esc(d.kill_rule)+' Updated '+local(d.updated_at)+'.</p>';
 dbody.innerHTML=h;drawChart($('#chart'),d)}
function costs(m){var rows='';['1000','5000'].forEach(function(s){var c=(m.costs||{})[s]||{};if(c.status!=='OK'){rows+='<span>$'+(+s).toLocaleString()+' per leg</span><span class="r muted">'+(c.status==='NO_DEPTH'?'book too thin':'not fetched yet')+'</span><span></span>';return}
 rows+='<span>$'+(+s).toLocaleString()+' per leg</span><span class="r">'+usd(c.cost_usd_maker)+' maker, '+hrs(c.payback_h_maker)+'</span><span class="r">'+usd(c.cost_usd)+' taker, '+hrs(c.payback_h)+'</span>'});
 var c1=(m.costs||{})['1000']||{};
 return '<div class="kv"><span class="h">Size</span><span class="h r">Maker cost, payback</span><span class="h r">Taker cost, payback</span>'+rows+'</div>'
 +'<p class="note" style="margin-top:8px">Payback uses the current 24h rate. '+(c1.status==='OK'?'Entry basis '+(c1.basis_bps>=0?'+':'')+c1.basis_bps.toFixed(1)+' bp; expected net over 7 days '+usd(c1.net_hold_usd)+' per $1,000 leg after taker costs.':'')+'</p>'}
function tableView(d){var rows=d.series.slice(-24).reverse().map(function(p){return '<tr><td>'+local(p.t)+'</td><td class="n">'+(p.net==null?'missing':pct(p.net*d.apy_factor))+(p.ex?' ('+p.ex+')':'')+'</td><td class="n">'+(p.l==null?'-':(p.l*1e4).toFixed(3)+' bp')+'</td><td class="n">'+(p.s==null?'-':(p.s*1e4).toFixed(3)+' bp')+'</td></tr>'}).join('');
 return '<details class="tbl"><summary>Show the last 24 hours as a table</summary><div class="tablewrap"><table><thead><tr><th>Hour</th><th class="n">Net, APY at 1x</th><th class="n">Long leg per hour</th><th class="n">Short leg per hour</th></tr></thead><tbody>'+rows+'</tbody></table></div></details>'}
function drawChart(el,d){var s=d.series;if(!s.length){el.innerHTML='<p class="muted">No comparable hours yet.</p>';return}
 var W=640,H=230,L=44,R=8,T=12,B=26,n=s.length,bw=(W-L-R)/n,f=d.apy_factor;
 var ok=s.filter(function(p){return p.net!=null&&!p.ex}).map(function(p){return p.net*f});
 function q98(a){a=a.slice().sort(function(x,y){return x-y});return a.length?a[Math.floor(.98*(a.length-1))]:0}
 var up=Math.max(q98(ok.filter(function(v){return v>0}))*1.15,d.min_apy*2,.05);
 var dn=Math.max(q98(ok.filter(function(v){return v<0}).map(function(v){return -v}))*1.15,up*.12);
 var y=function(v){v=Math.max(-dn,Math.min(up,v));return T+(H-T-B)*(up-v)/(up+dn)},z=y(0),top=Math.max(up,dn);
 var step=top>2?1:top>1?.5:top>.4?.2:top>.2?.1:.05,g='';
 for(var v=-Math.floor(dn/step)*step;v<=up+1e-9;v+=step){var yy=y(v);g+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+yy+'" y2="'+yy+'"/><text class="axis" x="'+(L-6)+'" y="'+(yy+4)+'" text-anchor="end">'+Math.round(v*100)+'%</text>'}
 var bars='';s.forEach(function(p,i){var x=L+i*bw+.5,w=Math.max(1,bw-1);if(p.net==null){bars+='<rect class="miss" x="'+x+'" y="'+(z-1)+'" width="'+w+'" height="2"/>';return}
  var v=p.net*f,yv=y(v),cls=p.ex?'ex':(v>0?'pos':'neg');bars+='<rect class="'+cls+'" x="'+x+'" y="'+Math.min(yv,z)+'" width="'+w+'" height="'+Math.max(1,Math.abs(z-yv))+'"/>'});
 var marks='',idx=function(t){for(var i=0;i<n;i++)if(s[i].t>t)return i;return n};
 if(d.run_start){var ri=idx(d.run_start);if(ri<n){var rx=L+ri*bw;marks+='<line class="run" x1="'+rx+'" x2="'+rx+'" y1="'+T+'" y2="'+(H-B)+'"/>'}}
 (d.kills||[]).forEach(function(k){var ki=idx(k-1);if(ki<n)marks+='<circle class="kill" cx="'+(L+ki*bw+bw/2)+'" cy="'+(H-B-6)+'" r="4"/>'});
 var ticks='';for(var i=0;i<n;i++){var t=new Date(s[i].t*1000);if(t.getHours()===0){ticks+='<text class="axis" x="'+(L+i*bw)+'" y="'+(H-8)+'">'+t.toLocaleDateString([],{weekday:'short'})+'</text>'}}
 var my=y(d.min_apy);el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Hourly net funding for the last '+n+' hours, as APY at 1x">'+g
  +'<line class="min" x1="'+L+'" x2="'+(W-R)+'" y1="'+my+'" y2="'+my+'"/>'+bars+'<line class="zero" x1="'+L+'" x2="'+(W-R)+'" y1="'+z+'" y2="'+z+'"/>'+marks+ticks
  +'<line class="cross" x1="0" x2="0" y1="'+T+'" y2="'+(H-B)+'" visibility="hidden"/><rect x="'+L+'" y="0" width="'+(W-L-R)+'" height="'+H+'" fill="transparent" class="hit"/></svg><div class="tip" hidden></div>';
 var svg=$('svg',el),tip=$('.tip',el),cross=$('.cross',el);
 function move(e){var r=svg.getBoundingClientRect(),sx=(e.clientX-r.left)*W/r.width,i=Math.floor((sx-L)/bw);if(i<0||i>=n){leave();return}
  var p=s[i],cx=L+i*bw+bw/2;cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('visibility','visible');
  tip.innerHTML='<b>'+local(p.t)+'</b><br>'+(p.net==null?'No data for this hour':'Net '+pct(p.net*f)+' APY at 1x'+(p.ex?' (left out: '+p.ex+')':''))
   +(p.l!=null?'<br>Long leg '+(p.l*1e4).toFixed(3)+' bp/h':'')+(p.s!=null?'<br>Short leg '+(p.s*1e4).toFixed(3)+' bp/h':'');
  tip.hidden=false;tip.style.left=(cx*r.width/W)+'px';tip.style.top=(Math.min(y(p.net==null?0:p.net*f),z)*r.height/H)+'px'}
 function leave(){tip.hidden=true;cross.setAttribute('visibility','hidden')}
 svg.addEventListener('mousemove',move);svg.addEventListener('mouseleave',leave)}
// refresh every 5 minutes unless the detail panel is open or the filter has focus
setInterval(function(){if(!drawer.classList.contains('is-open')&&document.activeElement!==q)location.reload()},300000);
})();
</script></body></html>""".replace("__SZ__", SZ)


# ---------------- server ----------------

class _Handler(BaseHTTPRequestHandler):
    timeout = 15  # drop idle connections (browsers open some in advance) so they can never hold the server

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        conn = None
        try:
            conn = connect(self.server.db_path)  # one connection per request: requests run on their own threads
            if url.path in ("/", "/index.html"):
                self._send(200, build_page(conn, db_path=self.server.db_path).encode(), "text/html; charset=utf-8")
            elif url.path == "/api/pair":
                pid = (parse_qs(url.query).get("id") or [""])[0]
                d = pair_detail(conn, pid)
                body = json.dumps(d if d is not None else {"error": "This pair is no longer tracked."})
                self._send(200 if d is not None else 404, body.encode(), "application/json")
            else:
                self.send_error(404)
        except Exception as e:  # show the error instead of a blank page; the worker keeps running
            self._send(500, f"<pre>dashboard error: {html.escape(repr(e))}</pre>".encode(), "text/html; charset=utf-8")
        finally:
            if conn is not None:
                conn.close()

    def log_message(self, *args):
        pass


def serve(host, port, stop=None, db_path=None):
    """Threaded server: each request runs on its own thread with its own read connection, and idle connections
    time out, so a slow or half-open browser connection can never block the page."""
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.daemon_threads = True
    srv.db_path = db_path or DB_PATH
    srv.timeout = 1
    while not (stop is not None and stop.is_set()):
        srv.handle_request()
    srv.server_close()
