#!/usr/bin/env bash
# Idempotent: first run provisions the server; later runs just update code and restart.
set -euo pipefail
APP=/opt/funding-scanner
DATA=/var/lib/funding-scanner

if [ ! -f /etc/funding-scanner.provisioned ]; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv sqlite3 ufw
  ufw allow OpenSSH && ufw --force enable
  if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  touch /etc/funding-scanner.provisioned
fi

# clean up the mis-named account created by an earlier broken deploy
if id scanne >/dev/null 2>&1; then userdel scanne; fi
if getent group scanne >/dev/null; then groupdel scanne; fi

getent group scanner >/dev/null || groupadd --system scanner
id scanner >/dev/null 2>&1 || useradd --system --gid scanner --home "$DATA" --shell /usr/sbin/nologin scanner
usermod -g scanner scanner

mkdir -p "$APP" "$DATA"
rm -rf "$APP/src" "$APP/scripts"
tar -xf /tmp/release.tar -C "$APP"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
[ -f "$DATA/env" ] || echo "DB_PATH=$DATA/scanner.db" > "$DATA/env"
chmod 600 "$DATA/env"
chown -R scanner:scanner "$DATA"

cat > /etc/systemd/system/funding-scanner.service <<EOF
[Unit]
Description=Funding arbitrage scanner worker
After=network-online.target
Wants=network-online.target

[Service]
User=scanner
Group=scanner
WorkingDirectory=$APP
EnvironmentFile=$DATA/env
ExecStart=$APP/.venv/bin/python -m scripts.worker
Restart=always
RestartSec=10
MemoryMax=400M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable funding-scanner >/dev/null 2>&1
systemctl restart funding-scanner
sleep 3
systemctl --no-pager --lines=5 status funding-scanner
