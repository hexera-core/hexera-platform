#!/bin/bash
# Responsibility: Bring one worker instance up - the pipeline container, the geometry-check container beside it, and nothing else.
# Owns: docker install, registry auth, and both containers' run arguments.
# Boundaries: it runs what it is told to run; the image digest and every endpoint arrive as instance metadata.
#
# This is the MIG instance startup script. It is deliberately free of configuration: the image
# DIGEST, the database, the broker and the object store all arrive as instance metadata, so the
# instance template is the single place a deployment's values are written, and rolling the template
# is what changes them.
#
# The simulation container runs the celery worker at concurrency 1 - the fleet's capacity is its
# instance count, so a job's cost is attributable to a whole instance and one stuck job cannot
# occupy a slot another job would need. The geometry-check container beside it is the slot the
# simulation queue cannot take: an upload is drawn while a job runs, not after it.
set -euxo pipefail

md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
# The project this instance runs in, asked of the metadata server rather than passed in: it is a
# fact about where we are, and a template that restated it could disagree with reality.
md_project() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/project/project-id"; }

WORKER_IMAGE="$(md worker-image)"
REDIS_URL="$(md redis-url)"
REDIS_KEY_PREFIX="$(md redis-key-prefix)"
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
  echo "REDIS_KEY_PREFIX=${REDIS_KEY_PREFIX}"
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
            "DEEPSEEK_API_KEY:deepseek-api-key-secret" \
            "OPENAI_API_KEY:openai-api-key-secret"; do
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

# THE WORKER'S DIRECTORIES, owned by the user the image runs as. A bind mount keeps the host's
# ownership, and docker creates a missing mount point as root - while the worker runs as the
# image's non-root user. So the container could not create a single job workspace: every job on
# the fleet died on its first mkdir with "Permission denied", before the pipeline had done
# anything. The uid is read off the image rather than restated here, so a Dockerfile that changes
# it does not strand the fleet the same way again.
WORKER_UID="$(docker run --rm --entrypoint id "${WORKER_IMAGE}" -u 2>/dev/null || echo 1000)"
mkdir -p /var/lib/hexera/workspaces /var/lib/hexera/data
chown "${WORKER_UID}:${WORKER_UID}" /var/lib/hexera/workspaces /var/lib/hexera/data

# --stop-timeout: when the VM is deleted, docker's own shutdown may stop this container before the
# shutdown script does (shutdown.sh). Docker's default is 10 seconds and then SIGKILL; this gives
# the running job the same 75 seconds to hand itself back to the queue whichever stop comes first.
docker rm -f hexera-worker >/dev/null 2>&1 || true
docker run -d --name hexera-worker --restart always --stop-timeout 75 \
  --env-file /etc/hexera/worker.env \
  -v /var/lib/hexera/workspaces:/srv/workspaces \
  -v /var/lib/hexera/data:/srv/data \
  "${WORKER_IMAGE}" \
  celery -A meshpipeline.runtime.celery_worker worker \
    --queues simulation_jobs --concurrency 1 --loglevel info

# THE GEOMETRY CHECK, IN A CONTAINER OF ITS OWN. The scout draws an upload the moment it lands and
# the naming runs when the user says what the part is: seconds of work, with a person watching.
# Routed to the simulation queue they waited behind whichever job the slot above was running - for
# as long as that job took, up to the pipeline deadline - while the console said "drawing your
# part... in a few seconds". This container consumes ONLY `geometry_checks` (celery_app.py
# task_routes), so a check always has a slot a mesh job cannot occupy. Same image, same settings,
# same mounts. Two slots, because two uploads arriving together is ordinary and a check is light
# enough that the instance carries both beside a running job. NOT counted by the autoscaler: a new
# instance takes minutes to arrive and a check takes seconds to drain, so the slots here are the
# capacity lever, not the fleet's size (create-queue-depth-publisher.sh publishes this queue's
# depth beside the fleet's, and scales on the fleet's alone).
docker rm -f hexera-geometry-worker >/dev/null 2>&1 || true
docker run -d --name hexera-geometry-worker --restart always --stop-timeout 30 \
  --env-file /etc/hexera/worker.env \
  -v /var/lib/hexera/workspaces:/srv/workspaces \
  -v /var/lib/hexera/data:/srv/data \
  "${WORKER_IMAGE}" \
  celery -A meshpipeline.runtime.celery_worker worker \
    --queues geometry_checks --concurrency 2 --loglevel info --hostname geometry@%h

# NOTHING ELSE RUNS HERE. The queue-depth exporter used to: a systemd unit beside every worker,
# publishing the group-wide backlog against this instance's own resource. That made the fleet the
# only writer of the number that wakes the fleet, so a group at zero instances could never come
# back - the reason its minimum is 1. Publication moved to a scheduled Cloud Run job
# (deploy/gcp/scripts/create-queue-depth-publisher.sh), which reports the depth whether or not any
# instance exists.
#
# NO BEAT HERE EITHER. celery_app.py's schedule - the stalled-job reaper, the upload purge, the
# orphan reconcile - runs for a hosted deployment as the maintenance sweep, a scheduled Cloud Run
# job (deploy/gcp/scripts/create-maintenance-sweep.sh). A beat beside every worker would run the
# schedule once per instance, and the two workers here consume only the simulation and
# geometry-check queues, so a beat's cleanup tasks would have no consumer anyway. Until that job existed nothing hosted ran the
# reaper, and a job whose instance the autoscaler replaced stayed `running` for good.
# tests/unit/deploy/test_fleet_schedule_contract.py holds both facts.
