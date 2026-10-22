"""Read-only HTML dashboard, rendered from the database on every request.
Served on localhost only (DASHBOARD_HOST); open it through the SSH tunnel: deploy/dashboard.ps1."""
import html
import json
import os
import shutil
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.config import APY_BASIS, DB_PATH, HOLD_HORIZON_H, MIN_APY_1X, PRIMARY_SIZE, REQUIRED_VENUE
from src.db import connect
from src.funding import completeness

HOUR = 3600
LABEL = {"hyperliquid": "HL", "lighter": "LT", "variational": "VAR"}
HIDDEN_FLAGS = {"ESTIMATED_DATA", "TOS_RISK"}  # TOS_RISK applies to every Variational pair: shown once in the header
SZ = str(int(PRIMARY_SIZE))
STATUS_ORDER = {"QUALIFIED": 0, "SUSPENDED": 1, "MONITORING": 2, "TERMINATED": 3}


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
    sig = _last_run(conn, "signals")
    return {"status": status, "issues": issues, "snap_age": snap_age, "sync": sync, "sync_age": sync_age,
            "signals_run": sig, "cov24": cov24, "cov72": cov72, "venues": venues,
            "uptime": now - starts[0] if starts else None, "restarts_24h": restarts_24h,
            "db_mb": db_bytes / 2**20, "free_gb": free / 2**30 if free is not None else None}


# ---------------- formatting ----------------

def esc(x):
    return html.escape("" if x is None else str(x))


def age(s):
    if s is None:
        return "never"
    s = int(s)
    return f"{s}s" if s < 120 else (f"{s // 60}m" if s < 7200 else (f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"))


def pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def usd(x):
    return "n/a" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def hrs(h):
    return "never" if h is None else (f"{h:.0f}h" if h < 1000 else ">999h")


def ts_tag(t):
    if not t:
        return "n/a"
    utc = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))
    return f'<time data-ts="{int(t)}">{utc}</time>'


def leg(venue, symbol):
    return f'<span class="v v-{esc(venue[:3])}">{esc(LABEL.get(venue, venue))}</span> {esc(symbol)}'


def td(text, sort=None, cls=""):
    attr = f' data-v="{sort}"' if sort is not None else ""
    return f'<td class="{cls}"{attr}>{text}</td>'


def num(x, default=-1e18):
    return default if x is None else x


# ---------------- sections ----------------

def pair_table(rows, classes, with_reasons, empty):
    head = ("<tr><th>Asset</th><th>Class</th><th>Long</th><th>Short</th><th>Status</th><th>Cohort</th>"
            "<th>Age</th><th>APY 1x</th><th>APY range</th><th>Cost (taker / maker)</th><th>Payback t / m</th>"
            f"<th>Net 24h</th><th>Net {HOLD_HORIZON_H:.0f}h</th><th>Basis</th><th>Flags{' / reasons' if with_reasons else ''}</th></tr>")
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    out = []
    for r in rows:
        m = json.loads(r["metrics"] or "{}")
        c = (m.get("costs") or {}).get(SZ, {})
        ok = c.get("status") == "OK"
        cls = classes.get(r["asset"], "unknown")
        flags = [f for f in (r["flags"] or "").split(",") if f and f not in HIDDEN_FLAGS]
        notes = flags + (json.loads(r["reasons"] or "[]") if with_reasons else [])
        st = r["status"]
        lev = int(r["max_lev"] or 1)
        out.append(
            f'<tr data-class="{esc(cls)}" data-text="{esc((r["asset"] + " " + r["long_symbol"] + " " + r["short_symbol"]).lower())}">'
            + td(f"<b>{esc(r['asset'])}</b>", r["asset"])
            + td(esc("RWA" if cls == "tradfi" else cls), cls)
            + td(leg(r["long_venue"], r["long_symbol"]), r["long_venue"])
            + td(leg(r["short_venue"], r["short_symbol"]), r["short_venue"])
            + td(f'<span class="chip s-{esc(st.lower())}">{esc(st)}</span>', STATUS_ORDER.get(st, 9))
            + td(esc(r["cohort"] or "-"), r["age_h"] or 0)
            + td(f"{r['age_h'] or 0:.0f}h", r["age_h"] or 0, "n")
            + td(pct(r["apy_1x"]), num(r["apy_1x"]), "n")
            + td(f"{pct(r['apy_1x'])} &ndash; {pct(r['apy_max'])} @ {lev}x", num(r["apy_max"]), "n")
            + td(f"{usd(c.get('cost_usd'))} / {usd(c.get('cost_usd_maker'))}" if ok else esc(c.get("status", "n/a")),
                 num(c.get("cost_usd"), 1e18), "n")
            + td(f"{hrs(c.get('payback_h'))} / {hrs(c.get('payback_h_maker'))}" if ok else "n/a",
                 num(c.get("payback_h"), 1e18), "n")
            + td(usd(c.get("net24_usd")), num(c.get("net24_usd")), "n")
            + td(usd(c.get("net_hold_usd")), num(c.get("net_hold_usd")), "n")
            + td(f"{c['basis_bps']:+.1f}bp" if ok else "n/a", c.get("basis_bps", 0) if ok else 0, "n")
            + td('<span class="notes">' + esc("; ".join(notes)) + "</span>", None))
    return f'<div class="scroll"><table class="sortable">{head}{"".join(out)}</table></div>'


def warming_section(rows):
    if not rows:
        return ""
    buckets = Counter()
    best = 0
    for r in rows:
        h = json.loads(r["metrics"] or "{}").get("history_hours")
        if h is None:
            buckets["not yet evaluated"] += 1
            continue
        best = max(best, h)
        buckets["0h" if h <= 0 else ("1-7h" if h < 8 else ("8-15h" if h < 16 else "16-23h"))] += 1
    order = ["0h", "1-7h", "8-15h", "16-23h", "not yet evaluated"]
    cells = "".join(f'<div class="stat"><span>{esc(k)}</span><b>{buckets[k]}</b></div>' for k in order if buckets[k])
    return (f'<section><h2>Warming up <small>{len(rows)} pairs without an APY yet</small></h2>'
            f'<p class="hint">A pair gets an APY once it has 24h of comparable funding history. Variational adds '
            f'4h or 8h per settlement, so pairs fill in steps. Most advanced pair: {best:.0f}h of 24h.</p>'
            f'<div class="stats">{cells}</div></section>')


def health_section(h, now):
    rows = "".join(f'<li class="i-{lvl.lower()}"><b>{lvl}</b> {esc(text)}</li>' for lvl, text in h["issues"])
    if not rows:
        rows = '<li class="i-ok"><b>OK</b> All collectors are healthy.</li>'
    c24, c72 = h["cov24"], h["cov72"]
    sync = h["sync"]
    sync_detail = json.loads(sync["detail"] or "{}") if sync else {}
    took = (sync["finished_at"] - sync["started_at"]) if sync else None
    repairs = sum((sync_detail.get(k) or {}).get("gap_repairs", 0) for k in ("hl", "lt"))
    v = h["venues"]
    stats = [
        ("Variational snapshot", f"{age(h['snap_age'])} ago"),
        ("Var settlements 24h", f"{c24['captured']} / {c24['expected']}"),
        ("Var settlements 72h", f"{c72['captured']} / {c72['expected']}"),
        ("Late fallbacks 24h", str(c24["late"])),
        ("Last sync", f"{age(h['sync_age'])} ago, took {age(took)}"),
        ("HL markets / gaps", f"{v['hyperliquid']['markets']} / {v['hyperliquid']['gaps']}"),
        ("LT markets / gaps", f"{v['lighter']['markets']} / {v['lighter']['gaps']}"),
        ("Gap repairs (last sync)", str(repairs)),
        ("Worker uptime", age(h["uptime"])),
        ("Restarts 24h", str(h["restarts_24h"])),
        ("Database", f"{h['db_mb']:.0f} MB"),
        ("Disk free", "n/a" if h["free_gb"] is None else f"{h['free_gb']:.1f} GB"),
    ]
    cells = "".join(f'<div class="stat"><span>{esc(k)}</span><b>{esc(val)}</b></div>' for k, val in stats)
    missing = c72["missing"][:20]
    miss = ""
    if missing:
        items = "".join(f"<li>{esc(s)} at {ts_tag(t)}</li>" for s, t in missing)
        miss = f'<details><summary>Missed Variational settlements (72h): {len(c72["missing"])}</summary><ul>{items}</ul></details>'
    return (f'<section class="health h-{h["status"].lower()}"><h2>Health <span class="chip st-{h["status"].lower()}">'
            f'{h["status"]}</span></h2><ul class="issues">{rows}</ul><div class="stats">{cells}</div>{miss}</section>')


def events_section(conn):
    rows = conn.execute("SELECT * FROM signal_events ORDER BY id DESC LIMIT 40").fetchall()
    if not rows:
        return ""
    out = []
    for e in rows:
        asset, _, legs = e["pair_id"].partition("|")
        lg, _, sh = legs.partition(">")
        lv, _, ls = lg.partition(":")
        sv, _, ss = sh.partition(":")
        out.append(f"<tr><td>{ts_tag(e['ts'])}</td><td><b>{esc(asset)}</b></td><td>{leg(lv, ls)}</td>"
                   f"<td>{leg(sv, ss)}</td><td>{esc(e['from_status'] or 'new')} &rarr; <b>{esc(e['to_status'])}</b></td>"
                   f'<td><span class="notes">{esc(e["detail"])}</span></td></tr>')
    return ('<section><h2>Recent signal changes</h2><div class="scroll"><table><tr><th>When</th><th>Asset</th>'
            f'<th>Long</th><th>Short</th><th>Change</th><th>Detail</th></tr>{"".join(out)}</table></div></section>')


def build_page(conn, now=None, db_path=None):
    now = int(now or time.time())
    sigs = conn.execute("SELECT * FROM signals").fetchall()
    classes = {r[0]: r[1] for r in conn.execute(
        "SELECT asset, MAX(asset_class) FROM markets WHERE matched=1 AND active=1 GROUP BY asset")}
    asof = max((r["updated_at"] for r in sigs), default=None)
    qualified = sorted((r for r in sigs if r["status"] == "QUALIFIED"),
                       key=lambda r: -num(json.loads(r["metrics"] or "{}").get("rank_net_hold_usd")))
    watch = sorted((r for r in sigs if r["status"] != "QUALIFIED" and r["apy_1x"] is not None),
                   key=lambda r: -r["apy_1x"])[:40]
    warming = [r for r in sigs if r["apy_1x"] is None and r["status"] != "TERMINATED"]
    counts = Counter(r["status"] for r in sigs)
    h = health(conn, now, db_path)
    tos = ('<p class="tos">Every pair has a Variational leg. Variational&#39;s Terms of Use (&sect;4.A.xv(6)) prohibit '
           'arbitrage between Variational and other platforms (TOS_RISK). Read-only scanner; not trading advice.</p>'
           if REQUIRED_VENUE == "variational" else "")
    summary = " &middot; ".join(esc(f"{k.lower()} {counts[k]}") for k in STATUS_ORDER if counts[k])
    body = f"""
<header>
  <div><h1>Funding scanner</h1>
  <p class="sub">Signals as of {ts_tag(asof)} &middot; page built {ts_tag(now)} &middot; {len(sigs)} pairs: {summary}</p>
  <p class="sub">APY on {esc(APY_BASIS)} &middot; minimum {MIN_APY_1X:.0%} at 1x &middot; costs at ${PRIMARY_SIZE:,.0f} per leg &middot;
  ranking = expected net over {HOLD_HORIZON_H:.0f}h after taker costs</p></div>
  <div class="controls"><input id="q" type="search" placeholder="Filter asset or market">
  <select id="cls"><option value="">All classes</option><option value="crypto">Crypto</option>
  <option value="tradfi">RWA</option></select></div>
</header>
{tos}
{health_section(h, now)}
<section><h2>Qualified <small>{len(qualified)}</small></h2>
{pair_table(qualified, classes, False, "No qualified pairs right now.")}</section>
<section><h2>Watchlist <small>top {len(watch)} non-qualified by APY</small></h2>
{pair_table(watch, classes, True, "No pairs with an APY yet.")}</section>
{warming_section(warming)}
{events_section(conn)}
"""
    return PAGE.replace("{{BODY}}", body)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Funding Scanner</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--muted:#5f6673;--line:#e3e6eb;--ok:#137a3a;--warn:#9a6200;--fail:#b42318;
--okbg:#e7f6ec;--warnbg:#fff4de;--failbg:#fde8e6;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--fg:#e8eaee;--muted:#9aa3b2;--line:#272b33;
--ok:#4cc77a;--warn:#e7b04a;--fail:#ff7a6b;--okbg:#13291c;--warnbg:#2e2412;--failbg:#33191a;--accent:#7a9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px;max-width:1500px;margin:auto}
header{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:flex-end}
h1{margin:0;font-size:22px}h2{font-size:16px;margin:0 0 10px}h2 small{color:var(--muted);font-weight:400}
.sub{margin:2px 0;color:var(--muted)}.controls{display:flex;gap:8px}
input,select{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}
.tos{margin:12px 0 0;padding:8px 12px;border-radius:8px;background:var(--warnbg);color:var(--warn)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--card);color:var(--muted);font-weight:600;cursor:pointer;user-select:none}
td.n{text-align:right;font-variant-numeric:tabular-nums}.notes{color:var(--muted);white-space:normal;display:inline-block;min-width:360px;max-width:560px}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;background:var(--line)}
.s-qualified,.st-ok{background:var(--okbg);color:var(--ok)}.s-suspended,.st-warn{background:var(--warnbg);color:var(--warn)}
.s-terminated,.st-fail{background:var(--failbg);color:var(--fail)}
.v{font-size:11px;font-weight:700;padding:0 5px;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.v-var{color:var(--accent);border-color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:10px}
.stat{border:1px solid var(--line);border-radius:8px;padding:8px}.stat span{display:block;color:var(--muted);font-size:12px}
.issues{list-style:none;padding:0;margin:0}.issues li{padding:4px 0}.i-fail b{color:var(--fail)}.i-warn b{color:var(--warn)}
.i-ok b{color:var(--ok)}.i-info b{color:var(--muted)}.h-fail{border-color:var(--fail)}.h-warn{border-color:var(--warn)}
.empty,.hint{color:var(--muted);margin:4px 0}details{margin-top:10px}summary{cursor:pointer}
@media (max-width:640px){body{padding:16px}.controls{width:100%}.controls input{flex:1}}
</style></head><body>{{BODY}}
<script>
document.querySelectorAll('time[data-ts]').forEach(function(t){var d=new Date(+t.dataset.ts*1000);
 t.title=t.textContent;t.textContent=d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});});
var q=document.getElementById('q'),cls=document.getElementById('cls');
try{q.value=sessionStorage.getItem('q')||'';cls.value=sessionStorage.getItem('cls')||'';}catch(e){}
function apply(){var t=q.value.toLowerCase(),c=cls.value;try{sessionStorage.setItem('q',q.value);sessionStorage.setItem('cls',c);}catch(e){}
 document.querySelectorAll('tr[data-class]').forEach(function(r){r.style.display=((!c||r.dataset.class===c)&&r.dataset.text.indexOf(t)>=0)?'':'none';});}
q.oninput=apply;cls.onchange=apply;apply();
document.querySelectorAll('table.sortable').forEach(function(tb){tb.querySelectorAll('th').forEach(function(th,i){
 th.onclick=function(){var rows=[].slice.call(tb.querySelectorAll('tr')).slice(1),dir=th.dataset.dir==='asc'?-1:1;th.dataset.dir=dir>0?'asc':'desc';
  rows.sort(function(a,b){var x=a.children[i].dataset.v,y=b.children[i].dataset.v;var nx=parseFloat(x),ny=parseFloat(y);
   if(!isNaN(nx)&&!isNaN(ny))return (nx-ny)*dir;return String(x||'').localeCompare(String(y||''))*dir;});
  rows.forEach(function(r){tb.appendChild(r);});};});});
setTimeout(function(){location.reload();},300000);
</script></body></html>"""


# ---------------- server ----------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            body, code = build_page(self.server.conn, db_path=self.server.db_path).encode(), 200
        except Exception as e:  # show the error instead of a blank page; the worker keeps running
            body, code = f"<pre>dashboard error: {html.escape(repr(e))}</pre>".encode(), 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(host, port, stop=None, db_path=None):
    """Single-threaded server: requests are handled one at a time on this thread (its own DB connection)."""
    srv = HTTPServer((host, port), _Handler)
    srv.db_path = db_path or DB_PATH
    srv.conn = connect(srv.db_path)
    srv.timeout = 1
    while not (stop is not None and stop.is_set()):
        srv.handle_request()
    srv.server_close()
