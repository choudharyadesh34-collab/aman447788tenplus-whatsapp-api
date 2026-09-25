#!/usr/bin/env bash
set -Eeuo pipefail

# Run as ubuntu, not with sudo. The only account link happens after installation,
# when the user requests a QR in the dashboard and scans it on their phone.
REPO="https://github.com/choudharyadesh34-collab/aman447788tenplus-whatsapp-api.git"
BRANCH="tenplus-shared-dashboard-20260926"
ROOT="/home/ubuntu/tenplus-openwa"
TOKEN_FILE="/home/ubuntu/amber-live/worker-token"
UNIT="/etc/systemd/system/tenplus-openwa-worker.service"

if [[ "$(id -un)" != "ubuntu" ]]; then
  echo "Run this installer as ubuntu (without sudo). No changes made." >&2
  exit 1
fi
for cmd in git docker python3 sudo ss; do
  command -v "$cmd" >/dev/null || { echo "Missing required command: $cmd. No changes made." >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "Docker Compose is unavailable. No changes made." >&2; exit 1; }
[[ -s "$TOKEN_FILE" ]] || { echo "Existing shared dashboard worker token is missing. No changes made." >&2; exit 1; }

# Preserve any pre-existing OpenWA containers or saved session volumes.
if docker ps -a --format '{{.Names}}' | grep -qi 'openwa'; then
  echo "An OpenWA container already exists. Left it untouched; inspect before continuing." >&2
  exit 1
fi
if docker volume ls --format '{{.Name}}' | grep -qi 'openwa'; then
  echo "A possible existing OpenWA data volume was found. Left it untouched; inspect before continuing." >&2
  exit 1
fi
if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)2785$'; then
  echo "TCP port 2785 is already in use. Left it untouched; inspect before continuing." >&2
  exit 1
fi
if [[ -e "$ROOT" && ! -d "$ROOT/.git" ]]; then
  echo "$ROOT exists but is not the expected Git checkout. No changes made." >&2
  exit 1
fi

if [[ ! -d "$ROOT/.git" ]]; then
  git clone --branch "$BRANCH" --single-branch "$REPO" "$ROOT"
else
  git -C "$ROOT" fetch --quiet origin "$BRANCH"
  git -C "$ROOT" checkout --quiet "$BRANCH"
  git -C "$ROOT" pull --ff-only --quiet origin "$BRANCH"
fi

cd "$ROOT"
if [[ ! -f .env ]]; then
  umask 077
  cp .env.tenplus.example .env
  python3 - <<'PY'
from pathlib import Path
import secrets
p = Path('/home/ubuntu/tenplus-openwa/.env')
lines = p.read_text(encoding='utf-8').splitlines()
updates = {
    'BASE_URL': 'http://127.0.0.1:2785',
    'DASHBOARD_URL': 'http://127.0.0.1:2785',
    'CORS_ORIGINS': 'http://127.0.0.1:2785',
    'API_MASTER_KEY': secrets.token_hex(32),
    'API_KEY_PEPPER': secrets.token_hex(32),
}
out = []
seen = set()
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        key = line.split('=', 1)[0].strip()
        if key in updates:
            out.append(f'{key}={updates[key]}')
            seen.add(key)
            continue
    out.append(line)
if seen != set(updates):
    raise SystemExit('OpenWA environment template mismatch; no credentials printed.')
p.write_text('\n'.join(out) + '\n', encoding='utf-8')
p.chmod(0o600)
PY
fi

python3 - <<'PY'
from pathlib import Path
import re
p = Path('/home/ubuntu/tenplus-openwa/.env')
s = p.read_text(encoding='utf-8')
for key in ('API_MASTER_KEY', 'API_KEY_PEPPER'):
    m = re.search(rf'(?m)^{key}=(.+)$', s)
    if not m or m.group(1).strip().startswith('CHANGE_ME') or len(m.group(1).strip()) < 48:
        raise SystemExit(f'{key} is not configured. No service started.')
PY

echo "Building the isolated OpenWA API; this can take several minutes."
docker compose --project-name tenplus-openwa build openwa-api
docker compose --project-name tenplus-openwa up -d openwa-api

python3 - <<'PY'
import time
from urllib.request import urlopen
deadline = time.time() + 150
while time.time() < deadline:
    try:
        with urlopen('http://127.0.0.1:2785/api/health', timeout=3) as response:
            if response.status == 200:
                print('OpenWA local API health: OK')
                break
    except Exception:
        time.sleep(3)
else:
    raise SystemExit('OpenWA did not become healthy. Worker service not installed.')
PY

sudo tee "$UNIT" >/dev/null <<'UNIT'
[Unit]
Description=TenPlus shared OpenWA dashboard bridge
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/tenplus-openwa
Environment=OPENWA_ROOT=/home/ubuntu/tenplus-openwa
Environment=OPENWA_API_BASE=http://127.0.0.1:2785
Environment=DASHBOARD_BASE=https://tenplus-dual-agents.onrender.com
Environment=DASHBOARD_WORKER_TOKEN_FILE=/home/ubuntu/amber-live/worker-token
ExecStart=/usr/bin/python3 -u /home/ubuntu/tenplus-openwa/scripts/tenplus_dashboard_worker.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now tenplus-openwa-worker.service
sudo systemctl is-active tenplus-openwa-worker.service >/dev/null
echo "OpenWA and the shared Heena/Amber bridge are online."
echo "No WhatsApp number was linked and no message was sent."
echo "Use only your dedicated secondary WhatsApp number for the QR link; do not link the verified primary business number."
echo "Open the shared dashboard, choose WhatsApp, click Login / Connect, then scan its QR."
sudo journalctl -u tenplus-openwa-worker.service -n 8 --no-pager
