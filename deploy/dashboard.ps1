# Opens the server dashboard through a private SSH tunnel; nothing is exposed publicly.
# Usage: .\deploy\dashboard.ps1   (leave the window open while you use the dashboard; press Enter to close)
$ip = (Get-Content "$PSScriptRoot\server.txt").Trim()
$port = 8765
$tunnel = Start-Process ssh -NoNewWindow -PassThru -ArgumentList @(
    "-i", "$HOME\.ssh\funding_scanner", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
    "-L", "${port}:127.0.0.1:${port}", "root@$ip")
Start-Sleep -Seconds 3
if ($tunnel.HasExited) { throw "SSH tunnel failed (is another dashboard window already open on port $port?)" }
Start-Process "http://localhost:$port"
Read-Host "Dashboard: http://localhost:$port  - press Enter to close the tunnel"
Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
