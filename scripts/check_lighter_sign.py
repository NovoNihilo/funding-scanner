"""Milestone 1b: Lighter funding sign convention and value units, checked against Hyperliquid."""
import time

from src.venues import hyperliquid as hl, lighter as lt

NOW = int(time.time())
START = NOW - 72 * 3600
MAX_SYMBOLS = 25
THRESH = 2e-6  # ignore near-zero rates when testing sign agreement


def pearson(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else float("nan")


def pct(vals, q):
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def main():
    universe, ctxs = hl.meta_and_asset_ctxs()
    hl_px = {u["name"]: float(c["markPx"]) for u, c in zip(universe, ctxs)
             if not u.get("isDelisted") and c.get("markPx")}
    books = sorted((b for b in lt.order_books()
                    if b.get("market_type") == "perp" and b.get("status") == "active"
                    and b["symbol"] in hl_px), key=lambda b: b["market_id"])
    xs, ys, ratios, zero_dirs, per_sym = [], [], [], {}, []
    agree = total = 0
    for b in books[:MAX_SYMBOLS]:
        sym = b["symbol"]
        lrows = lt.fundings(b["market_id"], START, NOW)
        time.sleep(1.1)  # Lighter unauthenticated limit: 60 req/min
        hrows = hl.funding_history(sym, START * 1000)
        h = {int(r["time"]) // 1000 // 3600 * 3600: float(r["fundingRate"]) for r in hrows}
        sx, sy = [], []
        for r in lrows:
            mag = float(r["rate"]) / 100.0
            if mag == 0:
                zero_dirs[r["direction"]] = zero_dirs.get(r["direction"], 0) + 1
            if mag >= 5e-6:
                ratios.append(float(r["value"]) / mag / hl_px[sym])
            ts = int(r["timestamp"]) // 3600 * 3600
            if ts in h:
                signed = mag if r["direction"] == "long" else -mag  # assumption A: 'long' = longs pay
                sx.append(signed)
                sy.append(h[ts])
                if abs(signed) > THRESH and abs(h[ts]) > THRESH:
                    total += 1
                    agree += (signed > 0) == (h[ts] > 0)
        if len(sx) >= 24:
            per_sym.append((sym, len(sx), pearson(sx, sy)))
            xs += sx
            ys += sy
    share = agree / total if total else float("nan")
    print(f"symbols tested={len(per_sym)} aligned rows={len(xs)}")
    print(f"pooled corr (assume 'long'=longs pay): {pearson(xs, ys):+.3f}")
    print(f"sign agreement (assume 'long'=longs pay, both |rate|>{THRESH}): {share:.1%} of {total}")
    pos = sum(1 for _, _, c in per_sym if c > 0)
    print(f"per-symbol corr: positive={pos} negative={len(per_sym) - pos}")
    for sym, n, c in per_sym[:10]:
        print(f"  {sym:<10} n={n:<3} corr={c:+.3f}")
    print(f"zero-rate direction counts: {zero_dirs}")
    print(f"value/(|rate|*HL price): median={pct(ratios, 0.5):.3f} p10={pct(ratios, 0.1):.3f} "
          f"p90={pct(ratios, 0.9):.3f} n={len(ratios)}")
    if share > 0.65:
        print("CONVENTION: direction='long' => LONGS PAY (positive)")
    elif share < 0.35:
        print("CONVENTION: direction='short' => LONGS PAY (direction names receiving side)")
    else:
        print("CONVENTION: INCONCLUSIVE")


if __name__ == "__main__":
    main()
