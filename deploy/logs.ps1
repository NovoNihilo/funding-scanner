# Shows recent worker logs. Usage: .\deploy\logs.ps1 [-n 80]
param([int]$n = 40)
$ip = (Get-Content "$PSScriptRoot\server.txt").Trim()
ssh -i "$HOME\.ssh\funding_scanner" "root@$ip" "journalctl -u funding-scanner -n $n --no-pager"
