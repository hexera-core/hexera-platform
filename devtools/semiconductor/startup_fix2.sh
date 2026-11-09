#!/bin/bash
# Startup for the V4 patch-split run. GCE re-runs a startup script on every boot and that has
# silently relaunched a 96-core experiment here before, so the sentinel is not optional.
exec > >(tee -a /var/log/fix2-startup.log) 2>&1
set -u
echo "=== startup $(date -u) ==="

SENTINEL=/var/lib/fix2.started
if [ -e "$SENTINEL" ]; then
  echo "SENTINEL PRESENT, this boot does not relaunch the work. $(cat $SENTINEL)"
  exit 0
fi
date -u > "$SENTINEL"

B=gs://dev-rehaan-artifacts-224734058693/corintis
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io pigz python3 g++ >/dev/null 2>&1
systemctl start docker
echo "docker $(docker --version 2>&1)"

mkdir -p /mnt/work
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker pull us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324 2>&1 | tail -2

gsutil cp "$B/drivers/fix2.sh" /root/fix2.sh
chmod +x /root/fix2.sh
echo "=== running fix2.sh ==="
/root/fix2.sh > /var/log/fix2.log 2>&1
rc=$?
echo "fix2.sh rc=$rc"
gsutil -q cp /var/log/fix2.log "$B/mesh/fix1/fix2.log"
gsutil -q cp /var/log/fix2-startup.log "$B/mesh/fix1/fix2-startup.log"
echo "logs uploaded"

NAME=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name)
ZONE=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | awk -F/ '{print $NF}')
echo "self-deleting $NAME in $ZONE"
gcloud compute instances delete "$NAME" --zone "$ZONE" --quiet 2>&1 | tail -3
