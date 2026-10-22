# Funding scanner

A read-only scanner for cross-venue funding-rate arbitrage between Variational, Hyperliquid (main dex plus the
`xyz` HIP-3 builder dex) and Lighter. Every tracked pair has one leg on Variational. It never places trades.

It runs on a small DigitalOcean server (systemd service `funding-scanner`). You view it through a private SSH
tunnel; nothing is exposed to the internet.

## Daily use (PowerShell, from the repo folder)

| What | Command |
|---|---|
| Open the dashboard | `.\deploy\dashboard.ps1` (keep the window open; press Enter to close the tunnel) |
| Recent server logs | `.\deploy\logs.ps1` or `.\deploy\logs.ps1 -n 100` |
| Signals table in the terminal | `.\deploy\signals.ps1` |
| Download a database backup | `.\deploy\backup.ps1` (saved under `data\backups`) |
| Deploy the last commit | `git push`, then `.\deploy\deploy.ps1` |
| Run the tests | `.venv\Scripts\python -m pytest -q` |

`deploy.ps1` waits by itself if you deploy within 5 minutes of a Variational settlement (every 4 hours on the
hour, UTC), so a restart never lands on one.

## How it works

1. **Matching** (`src/matching.py`): an asset is tracked only if Variational and at least one other venue list it
   at the same price (within 3%). Prices quoted at a power-of-ten scale are reconciled. HIP-3 markets come only from
   allowlisted deployers (`HIP3_DEXES`).
2. **Funding history** (`src/funding.py`): Hyperliquid and Lighter settled funding is fetched every hour with
   7 days of backfill and automatic gap repair. Variational has no history API, so the worker snapshots it every
   5 minutes plus 3 minutes, 90 seconds and 30 seconds before each settlement, and records each settlement as soon
   as it passes (estimated from the last snapshot before it; a late fallback of up to 1 hour is marked as such).
3. **Signals** (`src/signals.py`): each pair becomes an hourly series. Every hour pairs the other venue's hourly rate
   with Variational's last recorded settlement, held until the next one. A run starts at the first profitable hour
   and is killed after 3 unprofitable hours in a row.
   - **Emerging**: run 8 to 24 hours old and passing the checks on the run so far.
   - **Qualified**: run 24 hours or older, conservative APY (lower of the 48h and 7-day averages) of at least 5% at
     1x on capital, profitable in at least 60% of hours, at least 90% data coverage, tradable markets.
   - **Suspended**: was qualified, now failing a check. **Terminated**: the run was killed.
4. **Costs** (`src/costs.py`, informational only): round-trip cost from live order books (taker) and fees (maker),
   payback time at the current 24h rate, expected net over 7 days, and a 24h stress test that caps leverage.
5. **Dashboard** (`src/dashboard.py`): tabs for Opportunities, Watchlist, Health and Activity, with a detail panel
   per pair showing its last 7 days hour by hour.

## Checking Variational estimates against real payments

Export your funding history from Variational as CSV, save it as `data\funding.csv`, then:

```powershell
.\deploy\backup.ps1
$db = (Get-ChildItem data\backups\*.db | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
.venv\Scripts\python -m scripts.calibrate data\funding.csv --db $db
```

Run it within 3 days of the settlements you want to check (snapshots are kept 3 days). `data\` is not committed.

## Settings

All settings are environment variables with sensible defaults (`src/config.py`). On the server they go in
`/var/lib/funding-scanner/env`, one `NAME=value` per line, followed by `systemctl restart funding-scanner`.

| Setting | Default | Meaning |
|---|---|---|
| `REQUIRED_VENUE` | `variational` | every pair must have a leg here (empty = all pairs) |
| `HIP3_DEXES` | `xyz` | trusted Hyperliquid builder dexes, comma-separated |
| `MIN_APY_1X` | `0.05` | minimum APY at 1x on capital |
| `TERMINATE_AFTER` | `3` | unprofitable hours in a row that kill a run |
| `EMERGING_MIN_H` / `QUALIFY_MIN_H` | `8` / `24` | run age for Emerging / Qualified |
| `MAX_LEVERAGE_CAP` | `5` | ceiling on the suggested leverage |
| `PRIMARY_SIZE`, `COST_SIZES` | `1000`, `1000,5000` | USD per leg used for costs |
| `HOLD_HORIZON_H` | `168` | holding period used for ranking |
| `VAR_PRE_CAPTURE_S` | `180,90,30` | extra Variational captures before each settlement |
| `VAR_SNAPSHOT_KEEP_DAYS`, `RUNS_KEEP_DAYS`, `EVENTS_KEEP_DAYS` | `3`, `30`, `30` | data retention |
| `DASHBOARD_PORT` | `8765` | dashboard port on the server's localhost |

## Repository layout

- `src/` scanner code; `scripts/` everyday commands (`worker`, `run_matching`, `sync_funding`, `run_signals`,
  `dashboard`, `calibrate`); `scripts/dev/` one-off API probes from the build.
- `deploy/` server setup and the PowerShell helpers above. `tests/` offline tests plus `test_api_smoke.py`,
  which calls the live APIs.

## Caveats

- Variational's terms of use (section 4.A.xv(6)) prohibit arbitrage between Variational and other platforms.
- Variational rates are estimates from live snapshots until checked against your own payments (see above).
- Lighter and HIP-3 builders publish no dividend procedure, so stock and commodity pairs on them carry
  dividend risk (`DIVIDEND_UNKNOWN`).
