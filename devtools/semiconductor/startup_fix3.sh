#!/bin/bash
# Startup for V5. Sentinel first: GCE re-runs a startup script on every boot and that has
# silently relaunched a 96-core experiment in this project before.
exec > >(tee -a /var/log/fix3-startup.log) 2>&1
set -u
echo "=== startup $(date -u) ==="
SENTINEL=/var/lib/fix3.started
if [ -e "$SENTINEL" ]; then echo "SENTINEL PRESENT, not relaunching. $(cat $SENTINEL)"; exit 0; fi
date -u > "$SENTINEL"

B=gs://dev-rehaan-artifacts-224734058693/corintis
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io pigz python3 >/dev/null 2>&1
systemctl start docker
mkdir -p /mnt/work
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker pull us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324 2>&1 | tail -2

gsutil cp "$B/drivers/fix3.sh" /root/fix3.sh
chmod +x /root/fix3.sh
echo "=== running fix3.sh ==="
/root/fix3.sh > /var/log/fix3.log 2>&1
rc=$?
echo "fix3.sh rc=$rc"
gsutil -q cp /var/log/fix3.log "$B/mesh/fix1/fix3.log"
gsutil -q cp /var/log/fix3-startup.log "$B/mesh/fix1/fix3-startup.log"

# The mesh itself is kept this time. Both earlier boxes self-deleted with their meshes on them,
# so neither V3's nor V4's weight failures could ever be located. 2.5 GB of tarball is cheaper
# than a repeat run.
echo "=== archiving the layered coolant so the evidence outlives the box ==="
cd /mnt/work/V5 && tar -I "pigz -p $(nproc)" -cf /mnt/work/V5_layered.tar.gz processor* 2>/dev/null
ls -l /mnt/work/V5_layered.tar.gz
gsutil -q -o GSUtil:parallel_composite_upload_threshold=150M cp /mnt/work/V5_layered.tar.gz "$B/mesh/v5/V5_layered.tar.gz"
echo "archived rc=$?"

NAME=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name)
ZONE=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | awk -F/ '{print $NF}')
echo "self-deleting $NAME in $ZONE"
gcloud compute instances delete "$NAME" --zone "$ZONE" --quiet 2>&1 | tail -2
