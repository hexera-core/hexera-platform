#!/bin/bash
# Responsibility: Bring one worker instance up - the pipeline container plus the queue-depth exporter.
# Owns: docker install, registry auth, the worker container's run arguments, and the exporter service.
# Boundaries: it runs what it is told to run; the image digest and every endpoint arrive as instance metadata.
#
# This is the MIG instance startup script. It is deliberately free of configuration: the image
# DIGEST, the database, the broker and the object store all arrive as instance metadata, so the
# instance template is the single place a deployment's values are written, and rolling the template
# is what changes them.
#
# The container runs the celery worker at concurrency 1 - the fleet's capacity is its instance
# count, so a job's cost is attributable to a whole instance and one stuck job cannot occupy a slot
# another job would need.
set -euxo pipefail

md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }

WORKER_IMAGE="$(md worker-image)"
REDIS_URL="$(md redis-url)"
DATABASE_URL="$(md database-url)"
ENV_URI="$(md env-uri)"

export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y ca-certificates curl gnupg python3-pip
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y docker-ce docker-ce-cli containerd.io
  systemctl enable --now docker
fi

# The instance's own service account authenticates to Artifact Registry; no key material is placed
# on the instance, and the image is named by DIGEST so a rolled tag cannot change what runs here.
gcloud auth configure-docker "$(echo "${WORKER_IMAGE}" | cut -d/ -f1)" --quiet
docker pull "${WORKER_IMAGE}"

# The application's own settings (model keys, policy) come from the deployment's env object.
mkdir -p /etc/hexera
gcloud storage cp "${ENV_URI}" /etc/hexera/worker.env --quiet
chmod 600 /etc/hexera/worker.env
# Endpoints are authoritative from metadata: whatever the env object says about where the database
# and broker live is stale the moment a deployment moves them.
{
  echo "DATABASE_URL=${DATABASE_URL}"
  echo "REDIS_URL=${REDIS_URL}"
  echo "CELERY_BROKER_URL=${REDIS_URL}"
  echo "CELERY_RESULT_BACKEND=${REDIS_URL}"
} >> /etc/hexera/worker.env

docker rm -f hexera-worker >/dev/null 2>&1 || true
docker run -d --name hexera-worker --restart always \
  --env-file /etc/hexera/worker.env \
  -v /var/lib/hexera/workspaces:/srv/workspaces \
  -v /var/lib/hexera/data:/srv/data \
  "${WORKER_IMAGE}" \
  celery -A meshpipeline.runtime.celery_worker worker \
    --queues simulation_jobs --concurrency 1 --loglevel info

# The queue-depth exporter the autoscaler reads. It runs on the host rather than in the worker
# container so that a worker restart never interrupts the metric the group is scaling on.
pip3 install --quiet redis google-cloud-monitoring
gcloud storage cp "$(dirname "${ENV_URI}")/queue_depth_exporter.py" /usr/local/bin/queue_depth_exporter.py --quiet
cat > /etc/systemd/system/hexera-queue-exporter.service <<EOS
[Unit]
Description=Hexera queue depth exporter
After=network-online.target

[Service]
Environment=REDIS_URL=${REDIS_URL}
ExecStart=/usr/bin/python3 /usr/local/bin/queue_depth_exporter.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOS
systemctl daemon-reload
systemctl enable --now hexera-queue-exporter
