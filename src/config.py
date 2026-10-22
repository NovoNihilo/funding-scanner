import os

DB_PATH = os.environ.get("DB_PATH", "data/scanner.db")
PRICE_TOL = float(os.environ.get("PRICE_TOL", "0.03"))
BACKFILL_DAYS = float(os.environ.get("BACKFILL_DAYS", "7"))
VAR_RATE_SOURCE = os.environ.get("VAR_RATE_SOURCE", "pre")  # 'pre' or 'post'; set from timing verdict
VAR_WINDOW_S = int(os.environ.get("VAR_WINDOW_S", "900"))
# Variational capture reliability: it has no funding history API, so every settlement must be captured live.
VAR_PRE_CAPTURE_S = [int(x) for x in os.environ.get("VAR_PRE_CAPTURE_S", "180,90,30").split(",")]  # s before each
VAR_LATE_WINDOW_S = int(os.environ.get("VAR_LATE_WINDOW_S", "3600"))  # fallback: newest snapshot within this window
VAR_SNAPSHOT_KEEP_DAYS = float(os.environ.get("VAR_SNAPSHOT_KEEP_DAYS", "3"))
RUNS_KEEP_DAYS = float(os.environ.get("RUNS_KEEP_DAYS", "30"))
# Dashboard: served on localhost only, reached through an SSH tunnel (deploy/dashboard.ps1)
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
# Hyperliquid HIP-3 builder dexes to include (comma list). Each deployer runs its own oracle: allowlist only trusted ones.
HIP3_DEXES = [d.strip() for d in os.environ.get("HIP3_DEXES", "xyz").split(",") if d.strip()]
# Every tracked pair must have one leg on this venue (empty = all cross-venue pairs).
REQUIRED_VENUE = os.environ.get("REQUIRED_VENUE", "variational").strip()

# Signal economics
MIN_APY_1X = float(os.environ.get("MIN_APY_1X", "0.05"))  # minimum funding APY at 1x
APY_BASIS = os.environ.get("APY_BASIS", "capital")  # 'capital' (total collateral) or 'notional' (per leg)
MAX_LEVERAGE_CAP = float(os.environ.get("MAX_LEVERAGE_CAP", "5"))

# Signal lifecycle
TERMINATE_AFTER = int(os.environ.get("TERMINATE_AFTER", "3"))  # consecutive non-positive periods
MIN_POSITIVE_SHARE = float(os.environ.get("MIN_POSITIVE_SHARE", "0.6"))  # of valid hours, last 48h
MIN_COVERAGE = float(os.environ.get("MIN_COVERAGE", "0.9"))  # valid data hours / window hours, last 24h
SIGNAL_LOOKBACK_DAYS = float(os.environ.get("SIGNAL_LOOKBACK_DAYS", "14"))
ANOMALY_MIN_HOURLY = float(os.environ.get("ANOMALY_MIN_HOURLY", "0.0005"))  # 0.05%/h floor for outliers
ANOMALY_MAD_K = float(os.environ.get("ANOMALY_MAD_K", "10"))

# Execution costs and payback (informational only: costs never block or terminate a signal)
COST_SIZES = [float(x) for x in os.environ.get("COST_SIZES", "1000,5000").split(",")]  # USD per leg
PRIMARY_SIZE = float(os.environ.get("PRIMARY_SIZE", "1000"))  # size used for ranking and alerts
HOLD_HORIZON_H = float(os.environ.get("HOLD_HORIZON_H", "168"))  # ranking: expected net over a 7-day hold
PAYBACK_FAST_H = float(os.environ.get("PAYBACK_FAST_H", "24"))  # payback label thresholds
PAYBACK_OK_H = float(os.environ.get("PAYBACK_OK_H", "72"))
COST_MAX_AGE_S = int(os.environ.get("COST_MAX_AGE_S", "7200"))  # older cost estimates count as missing
HL_TAKER_FEE = float(os.environ.get("HL_TAKER_FEE", "0.00045"))  # Hyperliquid base tier taker fee
HL_MAKER_FEE = float(os.environ.get("HL_MAKER_FEE", "0.00015"))  # Hyperliquid base tier maker fee
HIP3_FEE_MULT = float(os.environ.get("HIP3_FEE_MULT", "2.0"))  # HIP-3 markets: deployer fee share, ~2x base
VAR_TAKER_FEE = float(os.environ.get("VAR_TAKER_FEE", "0"))  # Variational Omni charges no trading fee
VAR_QUOTE_PENALTY = float(os.environ.get("VAR_QUOTE_PENALTY", "1.25"))  # indicative (RFQ) quotes
VAR_STALE_PENALTY = float(os.environ.get("VAR_STALE_PENALTY", "2.0"))  # quotes older than VAR_STALE_S
VAR_STALE_S = int(os.environ.get("VAR_STALE_S", "600"))

# Leverage stress test (once-daily rebalance -> 24h adverse move)
STRESS_BUFFER = float(os.environ.get("STRESS_BUFFER", "1.25"))
MIN_STRESS_MOVE = float(os.environ.get("MIN_STRESS_MOVE", "0.05"))
DEFAULT_STRESS_MOVE = float(os.environ.get("DEFAULT_STRESS_MOVE", "0.30"))  # when no price history yet
PRICE_STATS_DAYS = int(os.environ.get("PRICE_STATS_DAYS", "20"))
