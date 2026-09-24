#!/bin/bash
# Responsibility: Bring one worker instance up - the pipeline containers, and nothing else.
# Owns: docker install, registry auth, and the run arguments of every worker container on the instance.
# Boundaries: it runs what it is told to run; the image digest and every endpoint arrive as instance metadata.
#
# This is the MIG instance startup script. It is deliberately free of configuration: the image
# DIGEST, the database, the broker and the object store all arrive as instance metadata, so the
# instance template is the single place a deployment's values are written, and rolling the template
# is what changes them.
#
# The MESH container runs the celery worker at concurrency 1 - the fleet's capacity is its instance
# count, so a job's cost is attributable to a whole instance and one stuck job cannot occupy a slot
# another job would need. A second, short-work container beside it drains the other four queues; the
# block above the `docker run` lines says why it has to exist and why it is not the same worker.
set -euxo pipefail

md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
# The project this instance runs in, asked of the metadata server rather than passed in: it is a
# fact about where we are, and a template that restated it could disagree with reality.
md_project() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/project/project-id"; }

WORKER_IMAGE="$(md worker-image)"
REDIS_URL="$(md redis-url)"
DATABASE_URL="$(md database-url)"
ENV_URI="$(md env-uri)"

export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y ca-certificates curl gnupg
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

# The application's NON-SECRET settings (policy, model routing, limits) come from the deployment's
# env object. It carries no credential: a bucket object and instance metadata are both readable by
# anyone who can describe the instance, which is how the four credentials in the live audit came to
# be published in the first place.
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

# CREDENTIALS are fetched by NAME, under this instance's own identity, and only ever exist in a
# root-owned file on the instance. Metadata carries the secret's name; the value is never in the
# template, never in an object in a bucket, and never in `gcloud compute instances describe`.
# Rotating a credential is then a new secret version plus an instance roll, with nothing to edit.
#
# TRACING OFF for this block, and it must stay off. This script runs under `set -x`, which echoes
# every expanded command to the startup log and the serial console - so the assignment below would
# print each secret in full, republishing exactly what moving it into Secret Manager removed. The
# guard against a plaintext credential in a spec would still pass while the log carried the value.
set +x
for pair in "POSTGRES_PASSWORD:postgres-password-secret" \
            "MINIO_SECRET_KEY:minio-secret-key-secret" \
            "DEEPINFRA_API_KEY:deepinfra-api-key-secret" \
            "OPENAI_API_KEY:openai-api-key-secret" \
            "DEEPSEEK_API_KEY:deepseek-api-key-secret"; do
  var="${pair%%:*}"; key="${pair##*:}"
  name="$(md "${key}" || true)"
  [ -n "${name}" ] || continue          # a credential this deployment does not use
  # --secret= names it; the value goes straight to the file and is never an argument or a log line.
  if value="$(gcloud secrets versions access latest --secret="${name}" --project="$(md_project)" 2>/dev/null)"; then
    printf '%s=%s\n' "${var}" "${value}" >> /etc/hexera/worker.env
  else
    echo "FATAL: cannot read secret ${name} for ${var} - refusing to start with a missing credential" >&2
    exit 1
  fi
done
unset value
set -x

# TWO WORKERS, ONE INSTANCE, AND WHY THERE HAS TO BE A SECOND ONE. This ran a single container on
# `--queues simulation_jobs`, and that one word is the whole reason the Surveyor did nothing on a
# deployed platform. The platform publishes to FIVE queues (celery_app.published_queues): a mesh to
# simulation_jobs, a measurement to geometry_measurement, a look to geometry_look, the periodic
# cleanups to cleanup_tasks and a training export to training_export. This fleet is the ONLY worker
# any deployment target starts, so the four it did not name were queued by the API, accepted by the
# broker, logged as "queued", and never run by anybody. An undrained queue raises nothing and alerts
# nothing, which is why it survived every check: the only consumer of geometry_look in the whole
# repository was a docker-compose service that exists on a laptop.
#
# THE SPLIT IS THE SAME ONE docker-compose.yml MAKES, and it is not cosmetic. The mesh worker holds
# concurrency 1 because a job's cost has to be attributable to a whole instance, and a mesh runs for
# hours. Putting the short work on that same worker would mean a measurement somebody IS waiting on
# sitting behind a multi-hour mesh, which is what the separate queues were created to prevent - the
# queues would be honoured and the waiting would not be. A second container with its own concurrency
# keeps the short work moving while the long work runs, on one instance, at no extra machine cost.
#
# WHAT IT CANNOT FIX BY ITSELF. The autoscaler is driven by the depth of QUEUE_NAME
# (simulation_jobs) alone, so a fleet whose floor is 0 is not woken by a queued measurement or look.
# deploy/gcp/scripts/validate-config.sh refuses that combination rather than leaving it to be
# discovered the way this was.
docker rm -f hexera-worker hexera-worker-utility >/dev/null 2>&1 || true
docker run -d --name hexera-worker --restart always \
  --env-file /etc/hexera/worker.env \
  -v /var/lib/hexera/workspaces:/srv/workspaces \
  -v /var/lib/hexera/data:/srv/data \
  "${WORKER_IMAGE}" \
  celery -A meshpipeline.runtime.celery_worker worker \
    --queues simulation_jobs --concurrency 1 --loglevel info --hostname simulation@%h

# The short work: the two Surveyor queues, the periodic cleanups and the training export. Concurrency
# 2 is docker-compose.yml's number for the same container. A measurement is seconds of native
# tessellation and a look is a render plus one provider call, so two of them beside a mesh fit an
# e2-standard-4 without competing for the core the mesh worker holds.
docker run -d --name hexera-worker-utility --restart always \
  --env-file /etc/hexera/worker.env \
  -v /var/lib/hexera/workspaces:/srv/workspaces \
  -v /var/lib/hexera/data:/srv/data \
  "${WORKER_IMAGE}" \
  celery -A meshpipeline.runtime.celery_worker worker \
    --queues cleanup_tasks,training_export,geometry_measurement,geometry_look \
    --concurrency 2 --loglevel info --hostname utility@%h

# NOTHING ELSE RUNS HERE. The queue-depth exporter used to: a systemd unit beside every worker,
# publishing the group-wide backlog against this instance's own resource. That made the fleet the
# only writer of the number that wakes the fleet, so a group at zero instances could never come
# back - the reason its minimum is 1. Publication moved to a scheduled Cloud Run job
# (deploy/gcp/scripts/create-queue-depth-publisher.sh), which reports the depth whether or not any
# instance exists.
