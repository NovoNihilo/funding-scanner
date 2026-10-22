# Read-only review of the signals stored on the server (includes Variational, which only the server collects).
# Usage: .\deploy\signals.ps1
$ip = (Get-Content "$PSScriptRoot\server.txt").Trim()
ssh -i "$HOME\.ssh\funding_scanner" "root@$ip" 'cd /opt/funding-scanner && sudo -u scanner env DB_PATH=/var/lib/funding-scanner/scanner.db .venv/bin/python -m scripts.run_signals --view'
