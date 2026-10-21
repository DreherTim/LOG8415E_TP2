set -Eeuo pipefail
exec > >(tee -a /var/log/tp2-bootstrap.log | logger -t tp2-bootstrap -s 2>/dev/console) 2>&1

ROLE="__ROLE__"
REPO_URL="__REPO_URL__"
REPO_REF="__REPO_REF__"
WORKER1_IP="__WORKER1_IP__"
WORKER2_IP="__WORKER2_IP__"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git python3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# This bootstrap only supports repos accessible without credentials.
git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" /opt/log8415e-tp2
cd "/opt/log8415e-tp2/$ROLE"
if [[ "$ROLE" == "orchestrator" ]]; then
  python3 - "$WORKER1_IP" "$WORKER2_IP" <<'PY'
import json, sys
with open('status.json', 'w') as f:
    json.dump({
        'worker1': {'ip': sys.argv[1], 'port': '5001', 'status': 'free'},
        'worker2': {'ip': sys.argv[2], 'port': '5001', 'status': 'free'},
    }, f, indent=2)
PY
fi
docker compose up -d --build
printf 'bootstrap complete: %s\n' "$ROLE"
