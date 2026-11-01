#!/bin/bash
set -Eeuo pipefail
exec > >(tee -a /var/log/tp2-bootstrap.log | logger -t tp2-bootstrap -s 2>/dev/console) 2>&1

ROLE="__ROLE__"
WORKER1_IP="__WORKER1_IP__"
WORKER2_IP="__WORKER2_IP__"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl python3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# No git clone. The local VS Code source is embedded by aws/deploy.py.
mkdir -p /opt/log8415e-tp2
base64 --decode <<'TP2_SOURCE_ARCHIVE' | tar -xz -C /opt/log8415e-tp2
__APP_ARCHIVE_BASE64__
TP2_SOURCE_ARCHIVE

cd "/opt/log8415e-tp2/$ROLE"
if [[ "$ROLE" == "orchestrator" ]]; then
  python3 - "$WORKER1_IP" "$WORKER2_IP" <<'PYCODE'
import json, sys
with open('status.json', 'w', encoding='utf-8') as f:
    json.dump({
        'worker1': {'ip': sys.argv[1], 'port': '5000', 'status': 'free'},
        'worker2': {'ip': sys.argv[2], 'port': '5000', 'status': 'free'},
    }, f, indent=2)
PYCODE
fi
docker compose up -d --build
printf 'bootstrap complete from local source: %s\n' "$ROLE"
