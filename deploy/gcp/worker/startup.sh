#!/bin/bash
# Responsibility: Bring one worker instance up - its containers, and nothing else.
# Owns: docker install, registry auth, and the run arguments of every container on the instance.
# Owns: the two instance ROLES, and the refusal to run at all without being told which one this is.
# Boundaries: it runs what it is told to run; the image digest and every endpoint arrive as instance metadata.
#
# This is the worker instance startup script - the managed instance group's, and the one scheduler
# instance's. It is deliberately free of configuration: the image DIGEST, the database, the broker
# and the object store all arrive as instance metadata, so the instance template is the single place
# a deployment's values are written, and rolling the template is what changes them.
#
# The MESH container runs the celery worker at concurrency 1 - the fleet's capacity is its instance
# count, so a job's cost is attributable to a whole instance and one stuck job cannot occupy a slot
# another job would need. A second, short-work container beside it drains the other four queues; the
# block above the `docker run` lines says why it has to exist and why it is not the same worker. A
# THIRD container, celery beat, runs on the scheduler instance only, and the block above it says why
# running it on a group member instead would be between one and five schedulers.
set -euxo pipefail

md() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
# The project this instance runs in, asked of the metadata server rather than passed in: it is a
# fact about where we are, and a template that restated it could disagree with reality.
md_project() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/project/project-id"; }

WORKER_IMAGE="$(md worker-image)"
REDIS_URL="$(md redis-url)"
DATABASE_URL="$(md database-url)"
ENV_URI="$(md env-uri)"

# WHICH ROLE THIS INSTANCE IS, asked and never assumed. `pipeline` is a member of the managed
# instance group; `scheduler` is the single instance beside it that also runs celery beat. The two
# differ in one thing and it is the thing at the bottom of this file.
#
# NEITHER VALUE IS A DEFAULT, and that is the whole reason this is three lines instead of one. If
# absence meant `pipeline`, a scheduler instance whose metadata was written wrong would come up as an
# ordinary worker and the deployment would have no scheduler at all - silently, because a missing
# beat raises nothing anywhere: the periodic tasks stay registered, their queue stays drained, and
# nobody publishes to it. If absence meant `scheduler`, every group member would run beat and every
# periodic task would run once per instance. An unreadable or unknown role is refused instead, before
# a single container starts, which is the only answer that cannot be wrong in silence.
WORKER_ROLE="$(md worker-role || true)"
case "${WORKER_ROLE}" in
  pipeline|scheduler) ;;
  "") echo "FATAL: instance metadata carries no worker-role, so this instance cannot tell whether it
   is a group member (pipeline) or the one instance that runs celery beat (scheduler). Refusing to
   guess: guessing pipeline leaves the deployment with no scheduler and no periodic task ever firing,
   and guessing scheduler runs every periodic task once per instance in the group.
   deploy/gcp/scripts/create-worker-fleet.sh writes this key on both the template and the scheduler
   instance." >&2; exit 1 ;;
  *) echo "FATAL: worker-role='${WORKER_ROLE}' is not a role this script knows. It is 'pipeline' for
   a member of the managed instance group or 'scheduler' for the single instance that also runs
   celery beat." >&2; exit 1 ;;
esac

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

# THE TWO BIND-MOUNT ROOTS, CREATED WITH THE CONTAINER'S OWNERSHIP BEFORE ANYTHING MOUNTS THEM.
#
# Both containers below bind-mount a HOST directory over /srv/workspaces and /srv/data. The image
# creates those two paths and hands them to USER_A, uid 1000 (Dockerfile: `useradd -m -u 1000 USER_A`
# then `chown -R USER_A:USER_A /srv /data`), and a bind mount REPLACES that directory with the host's
# - ownership, mode and all. Docker creates a missing mount source itself, as root:root, and both
# containers run as uid 1000, so on a fresh instance the application could not write to its own
# workspace root or its data root: every job failed at the first mkdir under /srv/workspaces with
# EACCES, after the instance had booted, installed docker, pulled a multi-gigabyte image,
# authenticated and taken the job. Nothing in the template or in this script said a word about it -
# the fleet reported healthy and every job it accepted failed the same way.
#
# NUMERIC uid AND gid, because USER_A exists inside the image and nowhere else: the instance is an
# Ubuntu VM that has never heard of that account, so `chown USER_A` fails and `install -o USER_A`
# with it. 1000 is the uid the Dockerfile pins, and it is pinned there precisely so the host side of
# a mount can name it.
#
# `install -d` AND NOT `mkdir -p`, because this also has to repair a directory an earlier boot left
# root-owned - a mkdir that finds the path already there changes no ownership and leaves the same
# unwritable root behind, which is the state this exists to end.
install -d -o 1000 -g 1000 -m 0755 /var/lib/hexera/workspaces /var/lib/hexera/data

# TWO WORKERS, ONE INSTANCE, AND WHY THERE HAS TO BE A SECOND ONE. This ran a single container on
# `--queues simulation_jobs`, and that one word is the whole reason the Surveyor did nothing on a
# deployed platform. The platform publishes to FIVE queues (adapters/pipeline_execution/queues.py): a mesh to
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

# THE SCHEDULER, ON THIS INSTANCE ONLY, AND WHY IT IS NOT ON EVERY INSTANCE.
#
# celery_app.conf.beat_schedule declares four periodic tasks - purge-expired-workspaces,
# reap-stalled-jobs, purge-expired-geometry-sources and reconcile-orphan-artifacts - and a periodic
# task only happens because something runs `celery beat` to publish it. docker-compose.yml has a beat
# service; the GCP deployment had NOTHING, so on the deployed platform not one of the four had ever
# fired. Expired workspaces were never purged, uploaded geometry bytes outlived
# UPLOAD_RETENTION_DAYS for ever, orphaned artifacts were never reconciled, and a job whose worker
# died stayed RUNNING until somebody looked instead of being failed after STALLED_JOB_TIMEOUT_HOURS -
# so the one mechanism that turns a dead worker into a customer-visible failure was itself absent.
#
# NOTHING RAISED, which is why it outlived the fleet's own audit. This is the undrained-queue failure
# with the ends swapped: there the publisher had no consumer, here the consumer had no publisher. The
# tasks are registered, cleanup_tasks is drained by the utility worker above, and the worker sits
# there ready - and no message ever arrives. There is no exception, no metric and no failing
# assertion in that story.
#
# EXACTLY ONE, WHICH IS WHY THE ROLE EXISTS. Two beats means every periodic task runs twice: two
# reapers racing the same stalled job, two purges deleting the same workspace, and the reconciliation
# sweep burning two of the five RECONCILE_MAX_RETRIES attempts per interval instead of one. The group
# holds between WORKER_MIG_MIN_REPLICAS and WORKER_MIG_MAX_REPLICAS instances and the autoscaler
# moves that number on queue depth, so a beat container run unconditionally here would be one
# scheduler at the floor and five under a backlog - a number that varies with the load. The group's
# template therefore carries worker-role=pipeline and create-worker-fleet.sh creates ONE instance
# beside it with worker-role=scheduler, deleting the previous one before creating its replacement so
# even a rotation cannot briefly have two.
#
# THE SCHEDULER INSTANCE IS ALSO A WORKER - the two `docker run` lines above are not conditional -
# because it is an ordinary instance that happens to schedule, and an e2-standard-4 kept alive for
# one tiny python process would be capacity thrown away. It is outside the group only because a group
# member is not something there is exactly one of.
#
# NO MOUNTS AND NO QUEUES: beat publishes and consumes nothing, so it needs neither the workspace
# root nor the data root. Its schedule file stays in the container (the image creates /data/beat), so
# replacing the instance starts a fresh schedule and each task's first tick comes up to one interval
# early. That is harmless for four idempotent sweeps on a 10- or 60-minute period, and the
# alternative - a fifth host directory whose ownership could be wrong - is the defect above.
if [ "${WORKER_ROLE}" = "scheduler" ]; then
  docker rm -f hexera-beat >/dev/null 2>&1 || true
  docker run -d --name hexera-beat --restart always \
    --env-file /etc/hexera/worker.env \
    "${WORKER_IMAGE}" \
    celery -A meshpipeline.runtime.celery_worker beat \
      --loglevel info \
      --scheduler celery.beat:PersistentScheduler \
      --schedule /data/beat/celerybeat-schedule
fi

# AND NOTHING ELSE RUNS HERE. The queue-depth exporter used to: a systemd unit beside every worker,
# publishing the group-wide backlog against this instance's own resource. That made the fleet the
# only writer of the number that wakes the fleet, so a group at zero instances could never come
# back - the reason its minimum is 1. Publication moved to a scheduled Cloud Run job
# (deploy/gcp/scripts/create-queue-depth-publisher.sh), which reports the depth whether or not any
# instance exists.
