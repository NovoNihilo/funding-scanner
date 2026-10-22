"""Evaluate signals on the local DB (with live execution costs) and print a ranked review table.
Uses the latest Hyperliquid settlement hour as 'now' so a local DB that isn't freshly synced still evaluates.
QUALIFIED pairs are ranked by expected net over HOLD_HORIZON_H at PRIMARY_SIZE (taker costs included).
Usage: python -m scripts.run_signals [--offline | --view]
  --offline  skip fetching order books and price history
  --view     read-only: print the signals the worker last stored (use on the server; writes nothing)"""
import json
import sys
import time
from collections import Counter

from src.config import APY_BASIS, HOLD_HORIZON_H, MIN_APY_1X, PRIMARY_SIZE, REQUIRED_VENUE
from src.costs import refresh_costs, refresh_price_stats
from src.db import connect
from src.signals import evaluate_all, market_index, prioritized_assets

SZ = str(int(PRIMARY_SIZE))


def pct(x):
    return f"{x * 100:5.1f}%" if x is not None else "  n/a"


def money(x):
    return f"{x:7.2f}" if x is not None else "    n/a"


def eta(c):
    if c.get("status") != "OK":
        return "n/a"
    t, m = c.get("payback_h"), c.get("payback_h_maker")
    f = (lambda h: "never" if h is None else (f"{h:.0f}h" if h < 1000 else ">999h"))
    return f"{f(t)}/{f(m)}"


def vl(venue, symbol):
    """Short venue label: H, L, V; HIP-3 markets show their builder dex, e.g. H(xyz)."""
    return venue[:1].upper() + (f"({symbol.split(':')[0]})" if ":" in symbol else "")


def rank_key(r):
    v = json.loads(r["metrics"]).get("rank_net_hold_usd")
    return (v is None, -(v or 0))


def main():
    conn = connect()
    view = "--view" in sys.argv
    if view:
        asof = conn.execute("SELECT MAX(updated_at) FROM signals").fetchone()[0] or int(time.time())
    else:
        asof = conn.execute("SELECT MAX(ts) FROM settlements WHERE venue='hyperliquid'").fetchone()[0] or int(time.time())
    if not view and "--offline" not in sys.argv:
        pre = evaluate_all(conn, asof, persist=False)
        t0 = time.time()
        cst = refresh_costs(conn, pre["candidates"])
        pst = refresh_price_stats(conn, prioritized_assets(market_index(conn), pre["candidates"]), max_n=80)
        print(f"costs {cst}  price_stats {pst}  ({time.time() - t0:.0f}s)")
    print(f"as of {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(asof))}  basis={APY_BASIS}  "
          f"min APY 1x={MIN_APY_1X:.0%}  size=${PRIMARY_SIZE:,.0f}/leg  hold={HOLD_HORIZON_H:.0f}h  "
          f"required leg={REQUIRED_VENUE or 'any'}")
    if view:
        st = Counter(r[0] for r in conn.execute("SELECT status FROM signals"))
        print(f"view only (stored by the worker): pairs={sum(st.values())} status={dict(st)}")
    else:
        res = evaluate_all(conn, asof)
        print(f"pairs={res['pairs']} events={res['events']} removed={res['removed']} status={res['status']}")
    cls = Counter(r["asset_class"] for r in conn.execute(
        "SELECT DISTINCT asset, asset_class FROM markets WHERE matched=1 AND active=1"))
    hip3 = conn.execute("SELECT COUNT(*) FROM markets WHERE matched=1 AND active=1 AND flags LIKE '%HIP3%'").fetchone()[0]
    print(f"matched assets by class={dict(cls)}  matched HIP-3 markets={hip3}")

    rows = conn.execute("SELECT * FROM signals WHERE apy_1x IS NOT NULL").fetchall()
    head = (f"{'asset':<9}{'pair':<13}{'status':<11}{'cohort':<12}{'age':>4}{'APY1x':>7}  {'range':<15}"
            f"{'cost':>7}{'payback t/m':>13}{'net24':>8}{'net hold':>9}{'basis':>8}  flags / reasons")
    sections = (("QUALIFIED (ranked by expected net over hold)", [r for r in rows if r["status"] == "QUALIFIED"],
                 rank_key),
                ("TOP NON-QUALIFIED (by APY)", [r for r in rows if r["status"] != "QUALIFIED"],
                 lambda r: -r["apy_1x"]))
    for title, sel_all, key in sections:
        sel = sorted(sel_all, key=key)[:15]
        print(f"\n=== {title}: {len(sel_all)} total, showing {len(sel)} ===")
        print(head)
        for r in sel:
            m = json.loads(r["metrics"])
            c = m["costs"].get(SZ, {})
            rng = f"{pct(r['apy_1x']).strip()}-{pct(r['apy_max']).strip()} {int(r['max_lev'])}x"
            basis = f"{c['basis_bps']:5.1f}bp" if c.get("status") == "OK" else "n/a"
            flags = [f for f in (r["flags"] or "").split(",") if f and f not in ("ESTIMATED_DATA",)]
            extra = flags + ([] if r["status"] == "QUALIFIED" else json.loads(r["reasons"])[:2])
            pair = vl(r["long_venue"], r["long_symbol"]) + ">" + vl(r["short_venue"], r["short_symbol"])
            print(f"{r['asset']:<9}{pair:<13}"
                  f"{r['status']:<11}{(r['cohort'] or '-'):<12}{r['age_h']:>4.0f}{pct(r['apy_1x']):>7}  {rng:<15}"
                  f"{money(c.get('cost_usd'))}{eta(c):>13}{money(c.get('net24_usd')):>8}"
                  f"{money(c.get('net_hold_usd')):>9}{basis:>8}  {'; '.join(extra)}")

    print("\n=== Why runs >=24h are not qualified (reason counts) ===")
    cnt = Counter()
    for r in conn.execute("SELECT reasons FROM signals WHERE status IN ('MONITORING','SUSPENDED') AND age_h >= 24"):
        for reason in json.loads(r["reasons"]):
            key = reason
            for prefix, label in (("APY 1x", "APY 1x below minimum"), ("coverage", "coverage below minimum"),
                                  ("consistency", "consistency below minimum")):
                if reason.startswith(prefix):
                    key = label
            cnt[key] += 1
    for k, n in cnt.most_common(10):
        print(f"  {n:>4}  {k}")
    print("\nSIGNALS OK")


if __name__ == "__main__":
    main()
