import os

DB_PATH = os.environ.get("DB_PATH", "data/scanner.db")
PRICE_TOL = float(os.environ.get("PRICE_TOL", "0.03"))
BACKFILL_DAYS = float(os.environ.get("BACKFILL_DAYS", "7"))
VAR_RATE_SOURCE = os.environ.get("VAR_RATE_SOURCE", "pre")  # 'pre' or 'post'; set from timing verdict
VAR_WINDOW_S = int(os.environ.get("VAR_WINDOW_S", "900"))

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
