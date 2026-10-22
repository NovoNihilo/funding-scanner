"""Check the scanner's Variational funding estimates against your official funding-history export.
Usage: python -m scripts.calibrate <export.csv> [more.csv ...] [--db path\\to\\scanner.db]
Get a current copy of the server database first with .\\deploy\\backup.ps1 (snapshots are kept 3 days,
so run this within 3 days of the settlements you want to check)."""
import sys
import time

from src.calibration import ESTIMATORS, compare, read_export
from src.db import connect


def fmt(x):
    return "      n/a" if x is None else f"{x:+.3e}"


def main():
    args = sys.argv[1:]
    db = None
    if "--db" in args:
        i = args.index("--db")
        db = args[i + 1]
        args = args[:i] + args[i + 2:]
    if not args:
        sys.exit(__doc__)
    rows = sorted({r for path in args for r in read_export(path)}, key=lambda x: (x[1], x[0]))
    conn = connect(db)
    detail, summary = compare(conn, rows)
    covered = [d for d in detail if any(d[e] is not None for e in ESTIMATORS)]
    print(f"official settlements in export: {len(rows)}   with scanner data: {len(covered)}")
    if covered:
        print(f"\n{'time (UTC)':<17}{'market':<14}{'official':>11}" + "".join(f"{e + ' err':>12}" for e in ESTIMATORS)
              + f"{'last age':>9}  source")
        for d in covered:
            age = f"{d['last_age_s']}s" if d["last_age_s"] is not None else "n/a"
            print(f"{time.strftime('%m-%d %H:%M', time.gmtime(d['ts'])):<17}{d['symbol']:<14}{d['official']:>+11.3e}"
                  + "".join(f"{fmt(d[e + '_err']):>12}" for e in ESTIMATORS) + f"{age:>9}  {d['source'] or '-'}")
    print("\n=== SUMMARY (match = within 1% of the official rate) ===")
    for e in ESTIMATORS:
        s = summary[e]
        if not s["n"]:
            print(f"  {e:<7} no data")
            continue
        print(f"  {e:<7} n={s['n']:<4} match={s['match_share']:6.1%}  sign agrees={s['sign_agree']:6.1%}  "
              f"median |err|={s['median_abs_err']:.2e}  max |err|={s['max_abs_err']:.2e}")
    if not covered:
        print("\nNo overlap yet: the export's settlements predate the scanner's data (or snapshots were pruned).")
    print("\nCALIBRATION OK")


if __name__ == "__main__":
    main()
