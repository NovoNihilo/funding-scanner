import os

DB_PATH = os.environ.get("DB_PATH", "data/scanner.db")
PRICE_TOL = float(os.environ.get("PRICE_TOL", "0.03"))
BACKFILL_DAYS = float(os.environ.get("BACKFILL_DAYS", "7"))
VAR_RATE_SOURCE = os.environ.get("VAR_RATE_SOURCE", "pre")  # 'pre' or 'post'; settled empirically in M4
VAR_WINDOW_S = int(os.environ.get("VAR_WINDOW_S", "900"))
