"""Compare Variational's OFFICIAL settled funding rates (from your account's funding-history CSV export) with
what the scanner recorded, and with alternative ways of reading the snapshots, to prove which estimator is right.

CSV columns used: created_at (settlement time, UTC), underlying (e.g. ERA), instrument_type
(perpetual_future / perpetual_rwa_future), transfer_type=funding, funding_rate (per settlement interval,
positive = longs pay; same convention as the scanner). Swap financing/dividend rows have no rate and are skipped.

Estimators compared for each settlement T (snapshots announcing next_ts == T):
  stored  - the settlement the scanner recorded (var_pre / var_pre_late / var_post)
  last    - last snapshot before T (what 'pre' records)
  twap    - time-weighted average of the displayed rate over the interval
  post    - first snapshot at or after T (the rate shown after settlement)"""
import csv
from datetime import datetime, timezone
from statistics import median

ESTIMATORS = ("stored", "last", "twap", "post")
REL_OK = 0.01  # within 1% of the official rate counts as a match


def parse_ts(s):
    return int(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def read_export(path):
    """[(symbol, ts, official_rate)] for perpetual funding rows that carry a rate."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("transfer_type") != "funding" or not (r.get("instrument_type") or "").startswith("perpetual"):
                continue
            if not (r.get("funding_rate") or "").strip():
                continue
            out.append((f"{r['underlying']}-PERP", parse_ts(r["created_at"]), float(r["funding_rate"])))
    return sorted(set(out), key=lambda x: (x[1], x[0]))


def estimates(conn, symbol, ts):
    """{estimator: rate or None} plus details for one settlement."""
    st = conn.execute("SELECT rate, source FROM settlements WHERE venue='variational' AND symbol=? AND ts=?",
                      (symbol, ts)).fetchone()
    snaps = conn.execute("SELECT captured_at, interval_s, interval_rate FROM var_snapshots WHERE symbol=? AND "
                         "next_ts=? AND captured_at < ? ORDER BY captured_at", (symbol, ts, ts)).fetchall()
    post = conn.execute("SELECT interval_rate FROM var_snapshots WHERE symbol=? AND captured_at >= ? "
                        "ORDER BY captured_at LIMIT 1", (symbol, ts)).fetchone()
    out = {"stored": st["rate"] if st else None, "source": st["source"] if st else None,
           "last": snaps[-1]["interval_rate"] if snaps else None, "post": post[0] if post else None,
           "twap": None, "last_age_s": ts - snaps[-1]["captured_at"] if snaps else None, "snapshots": len(snaps)}
    if len(snaps) >= 2:
        start = max(snaps[0]["captured_at"], ts - (snaps[-1]["interval_s"] or 0))
        total = weight = 0.0
        for a, b in zip(snaps, list(snaps[1:]) + [None]):
            t0, t1 = max(a["captured_at"], start), (b["captured_at"] if b else ts)
            if t1 > t0:
                total += a["interval_rate"] * (t1 - t0)
                weight += t1 - t0
        out["twap"] = total / weight if weight else None
    return out


def compare(conn, rows):
    """Per-settlement comparison rows and a per-estimator summary."""
    detail, errs = [], {e: [] for e in ESTIMATORS}
    for sym, ts, official in rows:
        est = estimates(conn, sym, ts)
        rec = {"symbol": sym, "ts": ts, "official": official, **est}
        for e in ESTIMATORS:
            v = est[e]
            if v is None:
                rec[f"{e}_err"] = None
                continue
            err = v - official
            rec[f"{e}_err"] = err
            errs[e].append((err, abs(err) <= REL_OK * abs(official) + 1e-12, (v > 0) == (official > 0) or v == official))
        detail.append(rec)
    summary = {}
    for e, xs in errs.items():
        if not xs:
            summary[e] = {"n": 0}
            continue
        abs_err = [abs(x[0]) for x in xs]
        summary[e] = {"n": len(xs), "match_share": sum(x[1] for x in xs) / len(xs),
                      "sign_agree": sum(x[2] for x in xs) / len(xs),
                      "median_abs_err": median(abs_err), "max_abs_err": max(abs_err)}
    return detail, summary
