# Deploys the last COMMITTED version to the VPS. Usage: .\deploy\deploy.ps1
# Settlement guard: Variational settles every 4h on the hour (UTC). A deploy restarts the worker, so it waits
# whenever it would land in the 5 minutes before or after a settlement.
$ip = (Get-Content "$PSScriptRoot\server.txt").Trim()
$key = "$HOME\.ssh\funding_scanner"
$u = (Get-Date).ToUniversalTime()
$s = ($u.Hour % 4) * 3600 + $u.Minute * 60 + $u.Second   # seconds since the last 4h boundary
$wait = 0
if ($s -ge 14100) { $wait = 14400 - $s + 300 } elseif ($s -lt 300) { $wait = 300 - $s }
if ($wait -gt 0) {
    Write-Output "Variational settlement window: waiting $wait s before deploying..."
    Start-Sleep -Seconds $wait
}
git archive --format=tar -o "$PSScriptRoot\release.tar" HEAD
if ($LASTEXITCODE) { throw "git archive failed" }
scp -i $key "$PSScriptRoot\release.tar" "$PSScriptRoot\setup.sh" "root@${ip}:/tmp/"
if ($LASTEXITCODE) { throw "scp failed" }
ssh -i $key "root@$ip" "tr -d '\r' < /tmp/setup.sh > /tmp/setup_lf.sh && bash /tmp/setup_lf.sh"
