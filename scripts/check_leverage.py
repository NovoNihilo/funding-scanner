"""Print max leverage for a few reference markets (verifies Lighter IMF units)."""
from src.db import connect
from src.matching import refresh_markets

conn = connect()
refresh_markets(conn)
for r in conn.execute("SELECT asset, venue, symbol, max_leverage FROM markets WHERE matched=1 "
                      "AND asset IN ('BTC','ETH','SOL','DOGE','XAU','NVDA') ORDER BY asset, venue"):
    print(tuple(r))
