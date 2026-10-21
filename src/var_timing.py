"""Decide how to read Variational's 'last' funding rate from live snapshots.
Rate frozen within an interval and changing at settlement -> API shows last APPLIED rate -> USE_POST.
Rate drifting within an interval -> running estimate -> USE_PRE."""
import time


def timing_report(conn, lookback_s=86400, now=None):
    since = int(now or time.time()) - lookback_s
    rows = conn.execute("SELECT symbol, captured_at, next_ts, interval_rate FROM var_snapshots "
                        "WHERE captured_at >= ? ORDER BY symbol, captured_at", (since,)).fetchall()
    intra = intra_chg = cross = cross_chg = 0
    prev = None
    for r in rows:
        if prev is not None and prev["symbol"] == r["symbol"] and r["captured_at"] - prev["captured_at"] <= 600:
            changed = prev["interval_rate"] != r["interval_rate"]
            if prev["next_ts"] == r["next_ts"]:
                intra += 1
                intra_chg += changed
            else:
                cross += 1
                cross_chg += changed
        prev = r
    a = intra_chg / intra if intra else None
    b = cross_chg / cross if cross else None
    if a is None or b is None or cross < 50:
        verdict = "INSUFFICIENT_DATA"
    elif a < 0.02 and b > 0.2:
        verdict = "USE_POST"
    elif a > 0.2:
        verdict = "USE_PRE"
    else:
        verdict = "UNCLEAR"
    return {"intra_pairs": intra, "intra_changed": None if a is None else round(a, 4),
            "boundary_pairs": cross, "boundary_changed": None if b is None else round(b, 4),
            "verdict": verdict}
