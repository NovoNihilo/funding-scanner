"""Offline tests for the Variational calibration report."""
from src.calibration import compare, read_export
from src.db import connect

H = 3600
T = 1792540800  # 2026-10-21 00:00:00 UTC


def test_read_export_keeps_perpetual_funding_rows_only(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text(
        "id,created_at,qty,asset,transfer_type,status,underlying,instrument_type,fee_type,funding_rate,funding_type\n"
        "a,2026-10-21T00:00:00Z,0.9,USDC,funding,confirmed,ERA,perpetual_future,,-0.0082,\n"
        "b,2026-10-21T00:00:00Z,0.4,USDC,funding,confirmed,TTWO,perpetual_rwa_future,,0.00023,\n"
        "c,2026-10-21T00:00:00Z,4.3,USDC,funding,confirmed,VXI,swap,,,financing\n", encoding="utf-8")
    assert read_export(str(p)) == [("ERA-PERP", T, -0.0082), ("TTWO-PERP", T, 0.00023)]


def test_compare_scores_each_estimator():
    conn = connect(":memory:")
    for k, rate in enumerate((1.0e-4, 1.5e-4, 2.0e-4)):  # displayed rate drifts during the 4h interval
        conn.execute("INSERT INTO var_snapshots (symbol, captured_at, next_ts, interval_s, interval_rate) "
                     "VALUES ('X-PERP', ?, ?, 14400, ?)", (T - 4 * H + k * 2 * H - (30 if k == 2 else 0), T, rate))
    conn.execute("INSERT INTO var_snapshots (symbol, captured_at, next_ts, interval_s, interval_rate) "
                 "VALUES ('X-PERP', ?, ?, 14400, 9e-5)", (T + 30, T + 4 * H))
    conn.execute("INSERT INTO settlements VALUES ('variational','X-PERP',?,14400,2.0e-4,NULL,'ESTIMATED','var_pre',"
                 "NULL,0)", (T,))
    detail, summary = compare(conn, [("X-PERP", T, 2.0e-4), ("Y-PERP", T, 1e-4)])
    d = detail[0]
    assert d["stored_err"] == 0 and d["last_err"] == 0 and d["last_age_s"] == 30
    assert d["post_err"] < 0 and d["twap"] < 2.0e-4                         # post and twap disagree with official
    assert summary["stored"]["match_share"] == 1.0 and summary["post"]["match_share"] == 0.0
    assert detail[1]["stored"] is None and summary["stored"]["n"] == 1      # no scanner data for Y-PERP
