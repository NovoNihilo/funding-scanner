import os

DB_PATH = os.environ.get("DB_PATH", "data/scanner.db")
PRICE_TOL = float(os.environ.get("PRICE_TOL", "0.03"))
BACKFILL_DAYS = float(os.environ.get("BACKFILL_DAYS", "7"))
VAR_RATE_SOURCE = os.environ.get("VAR_RATE_SOURCE", "pre")  # 'pre' or 'post'; set from timing verdict
VAR_WINDOW_S = int(os.environ.get("VAR_WINDOW_S", "900"))
# Signal economics (used from M5)
MIN_APY_1X = float(os.environ.get("MIN_APY_1X", "0.05"))
APY_BASIS = os.environ.get("APY_BASIS", "capital")  # 'capital' (total collateral) or 'notional' (per leg)
MAX_LEVERAGE_CAP = float(os.environ.get("MAX_LEVERAGE_CAP", "5"))
