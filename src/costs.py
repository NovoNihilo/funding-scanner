"""Execution costs and leverage stress inputs.

Round-trip cost per market, as a fraction of notional, in two execution styles:
- taker: buy + sell `size` USD against the current book (spread + slippage) + 2 x taker fee (worst case)
- maker: resting orders filled at the touch -> 2 x maker fee only (best case; fills are not guaranteed)
Each leg of a pair is opened and later closed once, so a pair's cost is the sum of both markets' round trips
plus any adverse cross-venue basis at entry.
- Hyperliquid and Lighter: executable public order books.
- Variational: indicative RFQ quotes (cached up to 10 min by the venue) -> penalised; no maker option (RFQ only).
"""
import time
from datetime import datetime, timezone

from src.config import (COST_SIZES, DEFAULT_STRESS_MOVE, HL_MAKER_FEE, HL_TAKER_FEE, MIN_STRESS_MOVE,
                        PRICE_STATS_DAYS, STRESS_BUFFER, VAR_QUOTE_PENALTY, VAR_STALE_PENALTY, VAR_STALE_S,
                        VAR_TAKER_FEE)
from src.venues import hyperliquid as hl, lighter as lt

HOUR = 3600


# ---------------- pure calculations ----------------

def walk(levels, notional):
    """Average fill price for `notional` USD against [(price, base_size)] best-first, or None if depth is short."""
    filled_usd = filled_base = 0.0
    for px, sz in levels:
        take = min(px * sz, notional - filled_usd)
        filled_base += take / px
        filled_usd += take
        if filled_usd >= notional * (1 - 1e-9):
            return filled_usd / filled_base
    return None


def book_round_trip(bids, asks, notional, fee):
    """(cost_fraction or None, mid) for buying and selling `notional` USD plus two taker fees."""
    if not bids or not asks:
        return None, None
    mid = (bids[0][0] + asks[0][0]) / 2
    buy, sell = walk(asks, notional), walk(bids, notional)
    if buy is None or sell is None:
        return None, mid
    return (buy - sell) / mid + 2 * fee, mid


def parse_iso(s):
    """Parse '2026-10-16T02:51:10.363561208Z' (nanoseconds) to epoch seconds."""
    if not s:
        return None
    return int(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def var_round_trip(snap, notional, now):
    """Variational indicative quotes at $1k and $100k, interpolated by size. Returns (cost, mid, flags)."""
    b1, a1, b100, a100 = snap["bid_1k"], snap["ask_1k"], snap["bid_100k"], snap["ask_100k"]
    if not (b1 and a1):
        return None, snap["mark_price"], ["NO_QUOTES"]
    mid = (b1 + a1) / 2
    s1 = (a1 - b1) / mid
    if b100 and a100 and notional > 1000:
        s100 = (a100 - b100) / ((a100 + b100) / 2)
        w = min(1.0, (notional - 1000) / 99000)
        spread = s1 + w * (s100 - s1)
    else:
        spread = s1
    flags, penalty = ["INDICATIVE"], VAR_QUOTE_PENALTY
    updated = parse_iso(snap["quotes_updated_at"])
    if updated is None or now - updated > VAR_STALE_S:
        flags.append("STALE_QUOTES")
        penalty = VAR_STALE_PENALTY
    return spread * penalty + 2 * VAR_TAKER_FEE, mid, flags


def p99_24h_move(closes):
    """99th percentile absolute 24h return from {hour_ts: close}."""
    moves = [abs(closes[t] / closes[t - 24 * HOUR] - 1) for t in closes if t - 24 * HOUR in closes and
             closes[t - 24 * HOUR] > 0]
    if len(moves) < 48:
        return None, len(moves)
    moves.sort()
    return moves[min(len(moves) - 1, int(0.99 * len(moves)))], len(moves)


def stress_leverage(p99, legs):
    """Highest leverage where neither leg is liquidated by a buffered 24h adverse move.
    legs: [(venue, venue_max_leverage)]. Variational maintenance = half the initial margin at the CHOSEN
    leverage (liquidation distance 1/(2L)); Hyperliquid/Lighter maintenance ~ half the initial at MAX leverage."""
    move = max(p99 if p99 is not None else DEFAULT_STRESS_MOVE, MIN_STRESS_MOVE) * STRESS_BUFFER
    safe = []
    for venue, vmax in legs:
        if venue == "variational":
            safe.append(1 / (2 * move))
        else:
            mmr = 1 / (2 * vmax) if vmax else 0.1
            safe.append(1 / (move + mmr))
    return max(1.0, float(int(min(safe)))), move


# ---------------- data refresh (network) ----------------

def _save_costs(conn, rows):
    with conn:
        conn.executemany("INSERT OR REPLACE INTO book_costs (venue, symbol, size_usd, rt_cost, mid, executable, "
                         "flags, captured_at, rt_cost_maker) VALUES (?,?,?,?,?,?,?,?,?)", rows)


def refresh_costs(conn, markets, now=None):
    """markets: iterable of (venue, symbol, market_id). Stores round-trip costs for every COST_SIZES entry."""
    now = int(now or time.time())
    stats = {"markets": 0, "errors": 0, "no_depth": 0}
    lt_fees = None
    var_snaps = {r["symbol"]: r for r in conn.execute(
        "SELECT * FROM var_snapshots s WHERE captured_at = (SELECT MAX(captured_at) FROM var_snapshots "
        "WHERE symbol = s.symbol)")}
    for venue, symbol, market_id in sorted(set(markets)):
        stats["markets"] += 1
        rows = []
        try:
            if venue == "variational":
                snap = var_snaps.get(symbol)
                if snap is None:
                    continue
                for size in COST_SIZES:
                    cost, mid, flags = var_round_trip(snap, size, now)
                    rows.append((venue, symbol, size, cost, mid, 0, ",".join(flags), now, cost))
            else:
                if venue == "hyperliquid":
                    bids, asks = hl.l2_book(symbol)
                    fee, maker_fee = HL_TAKER_FEE, HL_MAKER_FEE
                else:
                    if lt_fees is None:  # fees are quoted in percent
                        lt_fees = {str(d["market_id"]): (float(d.get("taker_fee") or 0) / 100,
                                                         float(d.get("maker_fee") or 0) / 100)
                                   for d in lt.order_book_details("perp")}
                    bids, asks = lt.order_book_orders(int(market_id))
                    fee, maker_fee = lt_fees.get(str(market_id), (0.0, 0.0))
                for size in COST_SIZES:
                    cost, mid = book_round_trip(bids, asks, size, fee)
                    flags = "" if cost is not None else "NO_DEPTH"
                    stats["no_depth"] += cost is None
                    maker = 2 * maker_fee if cost is not None else None
                    rows.append((venue, symbol, size, cost, mid, 1, flags, now, maker))
            _save_costs(conn, rows)
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = f"{venue}:{symbol} {e!r}"[:160]
    return stats


def refresh_price_stats(conn, assets, max_n=40, now=None):
    """Update the 24h-move percentile for up to max_n assets whose stats are missing or older than 24h.
    assets: {asset: [(venue, symbol, market_id)]} in priority order. Prefers Hyperliquid candles, else Lighter."""
    now = int(now or time.time())
    fresh = {r["asset"] for r in conn.execute("SELECT asset FROM price_stats WHERE updated_at > ?", (now - 86400,))}
    pending = [a for a in assets if a not in fresh]
    todo = pending[:max_n]
    stats = {"updated": 0, "errors": 0, "remaining": len(pending) - len(todo)}
    start = now - PRICE_STATS_DAYS * 86400
    for asset in todo:
        legs = {v: (s, mid) for v, s, mid in assets[asset]}
        try:
            if "hyperliquid" in legs:
                closes, src = hl.hourly_closes(legs["hyperliquid"][0], start, now), "hyperliquid"
            elif "lighter" in legs:
                closes, src = lt.hourly_closes(int(legs["lighter"][1]), start, now), "lighter"
            else:
                continue
            p99, n = p99_24h_move(closes)
            with conn:
                conn.execute("INSERT OR REPLACE INTO price_stats VALUES (?,?,?,?,?)", (asset, src, p99, n, now))
            stats["updated"] += 1
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = f"{asset} {e!r}"[:160]
    return stats
