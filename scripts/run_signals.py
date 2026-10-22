"""Milestone 5: evaluate signals on the local DB and print a ranked review table.
Uses the latest Hyperliquid settlement hour as 'now' so a local DB that isn't freshly synced still evaluates."""
import json
import time
from collections import Counter

from src.config import APY_BASIS, MIN_APY_1X
from src.db import connect
from src.signals import evaluate_all


def pct(x):
    return f"{x * 100:6.1f}%" if x is not None else "    n/a"


def main():
    conn = connect()
    asof = conn.execute("SELECT MAX(ts) FROM settlements WHERE venue='hyperliquid'").fetchone()[0] or int(time.time())
    res = evaluate_all(conn, asof)
    print(f"as of {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(asof))}  basis={APY_BASIS}  min APY 1x={MIN_APY_1X:.0%}")
    print(f"pairs={res['pairs']} events={res['events']} status={res['status']}")

    rows = conn.execute("SELECT * FROM signals WHERE apy_1x IS NOT NULL ORDER BY apy_1x DESC").fetchall()
    head = (f"{'asset':<10}{'long>short':<11}{'status':<11}{'cohort':<12}{'age_h':>6}{'APY1x':>8}"
            f"  {'APY range':<17}{'$/day/1k':>9}{'pos48':>7}{'cov24':>7}  flags")
    for title, keep in (("QUALIFIED", lambda r: r["status"] == "QUALIFIED"),
                        ("TOP NON-QUALIFIED (by APY)", lambda r: r["status"] != "QUALIFIED")):
        sel = [r for r in rows if keep(r)][:20]
        print(f"\n=== {title} ({sum(1 for r in rows if keep(r))} total, showing {len(sel)}) ===")
        print(head)
        for r in sel:
            m = json.loads(r["metrics"])
            rng = f"{pct(r['apy_1x']).strip()}-{pct(r['apy_max']).strip()} ({int(r['max_lev'])}x)"
            daily = m["daily_net_per_1k"]
            flags = ",".join(f for f in (r["flags"] or "").split(",") if f and f != "ESTIMATED_DATA")
            print(f"{r['asset']:<10}{r['long_venue'][:3] + '>' + r['short_venue'][:3]:<11}{r['status']:<11}"
                  f"{(r['cohort'] or '-'):<12}{r['age_h']:>6.0f}{pct(r['apy_1x']):>8}  {rng:<17}"
                  f"{(f'{daily:.2f}' if daily is not None else 'n/a'):>9}{pct(m['pos_share_48h']):>7}"
                  f"{pct(m['coverage_24h']):>7}  {flags}")

    print("\n=== Why runs >=24h are not qualified (reason counts) ===")
    c = Counter()
    for r in conn.execute("SELECT reasons FROM signals WHERE status IN ('MONITORING','SUSPENDED') AND age_h >= 24"):
        for reason in json.loads(r["reasons"]):
            key = reason
            for prefix, label in (("APY 1x", "APY 1x below minimum"), ("coverage", "coverage below minimum"),
                                  ("consistency", "consistency below minimum")):
                if reason.startswith(prefix):
                    key = label
            c[key] += 1
    for k, n in c.most_common(10):
        print(f"  {n:>4}  {k}")
    print("\nSIGNALS OK")


if __name__ == "__main__":
    main()
