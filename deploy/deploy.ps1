# Deploys the last COMMITTED version to the VPS. Usage: .\deploy\deploy.ps1
$ip = (Get-Content "$PSScriptRoot\server.txt").Trim()
$key = "$HOME\.ssh\funding_scanner"
git archive --format=tar -o "$PSScriptRoot\release.tar" HEAD
if ($LASTEXITCODE) { throw "git archive failed" }
scp -i $key "$PSScriptRoot\release.tar" "$PSScriptRoot\setup.sh" "root@${ip}:/tmp/"
if ($LASTEXITCODE) { throw "scp failed" }
ssh -i $key "root@$ip" 'sed -i "s/\r$//" /tmp/setup.sh && bash /tmp/setup.sh'
