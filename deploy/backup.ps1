# Pulls a consistent SQLite backup to data\backups. Usage: .\deploy\backup.ps1
$ip = (Get-Content "$PSScriptRoot\server.txt").Trim()
$key = "$HOME\.ssh\funding_scanner"
ssh -i $key "root@$ip" "sqlite3 /var/lib/funding-scanner/scanner.db '.backup /tmp/scanner-backup.db'"
New-Item -ItemType Directory -Force "data\backups" | Out-Null
$f = "data\backups\scanner-$(Get-Date -Format yyyyMMdd-HHmm).db"
scp -i $key "root@${ip}:/tmp/scanner-backup.db" $f
Write-Output "saved $f"
