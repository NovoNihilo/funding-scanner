"""Milestone 2: refresh cross-venue market matching and print a review summary."""
from collections import Counter, defaultdict
from itertools import combinations

from src.config import REQUIRED_VENUE
from src.db import connect
from src.matching import refresh_markets


def fmt(r):
    px = f"{r['unit_price']:.6g}" if r["unit_price"] else "n/a"
    return f"{r['venue'][:3]}:{r['symbol']}@{px}"


def main():
    conn = connect()
    rows = refresh_markets(conn)
    print(f"markets fetched: {dict(Counter(r['venue'] for r in rows))}")
    by_asset = defaultdict(list)
    for r in rows:
        by_asset[r["asset"]].append(r)
    matched = {a: [r for r in g if r["matched"]] for a, g in by_asset.items()}
    matched = {a: g for a, g in matched.items() if g}
    combos = Counter(" & ".join(sorted({r["venue"] for r in g})) for g in matched.values())
    print(f"\nmatched assets={len(matched)}")
    for k, n in combos.most_common():
        print(f"  {k}: {n}")
    pairs = sum(len([c for c in combinations(sorted({r['venue'] for r in g}), 2)
                     if not REQUIRED_VENUE or REQUIRED_VENUE in c]) for g in matched.values())
    print(f"cross-venue pairs (unordered)={pairs}  required venue={REQUIRED_VENUE or 'any'}")
    print(f"matched by asset class: {dict(Counter(g[0]['asset_class'] for g in matched.values()))}")
    print("\nmultiplier-normalized matches:")
    for a, g in sorted(matched.items()):
        if any(r["multiplier"] != 1 for r in g):
            print(f"  {a}: " + "  ".join(fmt(r) for r in g))
    print("\ntradfi matches:")
    print("  " + ", ".join(sorted(a for a, g in matched.items() if g[0]["asset_class"] == "tradfi")))
    mism = sorted(((a, g) for a, g in by_asset.items()
                   if any("PRICE_MISMATCH" in r["flags"] for r in g)), key=lambda x: x[0])
    print(f"\nPRICE_MISMATCH groups={len(mism)} (first 25):")
    for a, g in mism[:25]:
        print(f"  {a}: " + "  ".join(fmt(r) for r in g))
    dup = Counter((r["venue"], r["asset"]) for r in rows)
    print(f"\nsame-venue duplicate assets: {[k for k, n in dup.items() if n > 1][:15]}")
    print(f"flag counts: {dict(Counter(f for r in rows for f in r['flags']))}")
    n = conn.execute("SELECT COUNT(*) FROM markets WHERE matched=1 AND active=1").fetchone()[0]
    print(f"\nDB matched market rows={n}")
    print("MATCHING OK")


if __name__ == "__main__":
    main()
