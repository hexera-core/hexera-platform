#!/usr/bin/env bash
# Responsibility: Provision the maintenance sweep as a scheduled Cloud Run Job, and the schedule that runs it.
# Owns: the job, its schedule, the invoker grant the scheduler needs, and the one proving run.
# Boundaries: it schedules the sweeps; what each one does, and why running it twice is harmless, is
#             application/maintenance/ and runtime/maintenance_sweep.py.

# Provision the MAINTENANCE SWEEP. Idempotent and reconciling: an existing job is updated in place.
#
#   bash deploy/gcp/scripts/create-maintenance-sweep.sh
#
# WHY THIS EXISTS. celery_app.py declares a beat schedule - reap stalled jobs every ten minutes,
# purge expired uploads and reconcile orphaned objects hourly - and the compose stack runs it: a
# `beat` service enqueues the tasks and `worker-utility` consumes the cleanup queue. A hosted
# deployment had neither. The fleet's startup.sh runs one worker per instance on the simulation
# queue and nothing else, and no other workload ran beat. So a job whose instance the autoscaler
# replaced stayed `running` for good - and because a running job holds back its base credits
# (application/spend_gate.py), its organisation was refused every new job with "your remaining
# credits are held by the run already in progress". Observed on shared dev, 2026-09-28.
#
# WHY NOT BEAT ON THE FLEET. A beat beside every worker runs the schedule once per instance, and a
# managed group has no "first" instance to run it on alone. Its cleanup tasks would also need a
# consumer of the cleanup queue, which the fleet deliberately does not run - the fleet's capacity
# is its instance count, one job per instance. A schedule calling an admin route instead would
# put ADMIN_API_KEY, a cross-tenant credential, in the scheduler's configuration in the clear. The
# meter sweep settled the shape: a scheduled one-shot on the application image, as the API's
# identity, running the maintenance in-process (runtime/maintenance_sweep.py).
#
# WHY THE API'S IDENTITY. It already holds exactly what a sweep needs - the database password and
# the object-store credential - and a second identity would need the same grants and double the
# places a credential can be read from.
#
# INPUTS   the deployment env (APP_IMAGE, MIGRATE_DB_*, REDIS_*, MINIO_*, VPC_*, the *_SECRET names)
# MUTATES  the maintenance-sweep job, its schedule, and the scheduler's invoker binding.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

SWEEP_JOB="${MAINTENANCE_SWEEP_JOB:-${DEPLOYMENT_ID}-maintenance-sweep}"
SWEEP_SCHEDULER="${SWEEP_JOB}-tick"
# A DEPLOYMENT WITH NO HOSTED DATABASE HAS NO JOBS TABLE TO SWEEP. That is the mesh-only case,
# where the application - and its beat service - run on the operator's machine. A stated skip,
# like the schema stage's.
if [ -z "${MIGRATE_DB_HOST:-}" ] || [ -z "${POSTGRES_PASSWORD_SECRET:-}" ]; then
  info "No hosted database - skipping the maintenance sweep"
  log "the compose stack's beat service runs the schedule there; set MIGRATE_DB_HOST and"
  log "POSTGRES_PASSWORD_SECRET to sweep a hosted database from this deployment"
  exit 0
fi
require_digest_reference APP_IMAGE "${APP_IMAGE:-}"

SWEEP_SA="${API_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-api}"
SWEEP_SA_EMAIL="${SWEEP_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
# EVERY TEN MINUTES: the beat schedule's cadence for the reaper, the one sweep with a person
# waiting on it. The two byte-level sweeps were hourly there and ride along here; each is a
# bounded, claim-based pass that costs one query when there is nothing to do. An execution takes
# about a minute, nearly all of it pulling the application image, so ticks do not overlap - and if
# one did, every sweep is a compare-and-set or a claim, so two at once do the work once.
SWEEP_SCHEDULE="${MAINTENANCE_SWEEP_SCHEDULE:-*/10 * * * *}"

info "Maintenance sweep ${SWEEP_JOB} in ${GCP_REGION}"
log "image (validated digest): ${APP_IMAGE}"

JOB_ENV=(
  "ENV=${APP_ENV:-}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "POSTGRES_HOST=${MIGRATE_DB_HOST}"
  "POSTGRES_PORT=${MIGRATE_DB_PORT:-5432}"
  "POSTGRES_DB=${MIGRATE_DB_NAME:-meshpipeline}"
  "POSTGRES_USER=${MIGRATE_DB_USER:-meshpipeline}"
)
# THE BROKER, for the reaper's closing line to any client still streaming a dead job - under this
# deployment's key prefix, the same way the API and the fleet address it, or the line would go to
# a channel nobody is subscribed to.
if [ -n "${REDIS_URL:-}" ]; then
  JOB_ENV+=("REDIS_URL=${REDIS_URL}" "REDIS_KEY_PREFIX=${REDIS_KEY_PREFIX:-}")
fi
# THE OBJECT STORE'S NON-SECRET HALF, for the upload purge and the orphan reconcile - the same
# values the API stage states, for the same reason: without them the adapter dials the local
# stack's localhost:9000, which in a Cloud Run job is the container itself.
if [ -n "${MINIO_ENDPOINT:-}" ]; then
  require_vars MINIO_ACCESS_KEY MINIO_BUCKET MINIO_REGION
  JOB_ENV+=(
    "MINIO_ENDPOINT=${MINIO_ENDPOINT}"
    "MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}"
    "MINIO_BUCKET=${MINIO_BUCKET}"
    "MINIO_REGION=${MINIO_REGION}"
    "MINIO_SECURE=${MINIO_SECURE:-true}"
  )
fi
# THE KNOBS THE SWEEPS READ, forwarded only when this deployment states them; otherwise the sweep
# runs on the code defaults, exactly as the API and the fleet's env object do.
for _knob in STALLED_JOB_TIMEOUT_HOURS WORKER_LEASE_SECONDS UPLOAD_RETENTION_DAYS; do
  [ -z "${!_knob:-}" ] || JOB_ENV+=("${_knob}=${!_knob}")
done
for entry in "${JOB_ENV[@]}"; do
  case "${entry}" in
    *"|"*) die "the setting '${entry%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with." ;;
  esac
done

JOB_SECRETS=()
for pair in "POSTGRES_PASSWORD:POSTGRES_PASSWORD_SECRET" \
            "MINIO_SECRET_KEY:MINIO_SECRET_KEY_SECRET"; do
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  JOB_SECRETS+=("${pair%%:*}=${secret_name}:latest")
done

VPC_NETWORK="${VPC_NETWORK:-default}"
VPC_SUBNET="${VPC_SUBNET:-default}"
job_args=(
  --image "${APP_IMAGE}"
  --region "${GCP_REGION}"
  --service-account "${SWEEP_SA_EMAIL}"
  --command python
  # ATTACHED WITH '=', because the value starts with '-': written as two words, gcloud's parser
  # reads "-m,..." as a flag of its own (create-meter-sweep.sh found this the hard way).
  --args=-m,meshpipeline.runtime.maintenance_sweep
  # ONE RETRY. Every sweep is a compare-and-set or a claim, so a retry does the work once; more
  # than one only delays the next tick, which retries anyway.
  --max-retries 1
  --task-timeout 600s
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${JOB_ENV[*]}")"
  --set-secrets "$(IFS=,; printf '%s' "${JOB_SECRETS[*]}")"
  # THE PRIVATE ROUTE to Cloud SQL's and Memorystore's private addresses - the API's three flags.
  --network "${VPC_NETWORK}"
  --subnet "${VPC_SUBNET}"
  --vpc-egress private-ranges-only
  --labels "app=hexera,component=maintenance-sweep,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
)

if gc run jobs describe "${SWEEP_JOB}" --region "${GCP_REGION}" >/dev/null 2>&1; then
  info "Updating maintenance sweep job ${SWEEP_JOB}"
  gc run jobs update "${SWEEP_JOB}" "${job_args[@]}"
else
  info "Creating maintenance sweep job ${SWEEP_JOB}"
  gc run jobs create "${SWEEP_JOB}" "${job_args[@]}"
fi

# ONE RUN NOW, waited on, before a schedule is trusted to do it unwatched. It proves the identity
# reads its secrets, the database answers over the private route, and the entrypoint is the
# entrypoint. It is also the run that frees a tenant whose credits are held by a job a dead worker
# left behind - the deploy that ships the sweep should not leave that to the next tick. The
# queue-depth publisher set the precedent: a first run that fails fails the deploy, with the
# command that says why, rather than leaving a schedule firing at a job that cannot do its work.
info "Sweeping once, to prove the path before the schedule depends on it"
gc run jobs execute "${SWEEP_JOB}" --region "${GCP_REGION}" --wait \
  || die "the maintenance sweep failed its first run, so stalled jobs are NOT being reaped in this
   deployment. Read why:
     gcloud run jobs executions list --job ${SWEEP_JOB} --region ${GCP_REGION} --project ${GCP_PROJECT_ID}"

SCHEDULER_URI="https://${GCP_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT_ID}/jobs/${SWEEP_JOB}:run"
gc services enable cloudscheduler.googleapis.com >/dev/null 2>&1 || true
sched_args=(
  --location "${GCP_REGION}"
  --schedule "${SWEEP_SCHEDULE}"
  --time-zone UTC
  --uri "${SCHEDULER_URI}"
  --http-method POST
  --oauth-service-account-email "${SWEEP_SA_EMAIL}"
  --attempt-deadline 60s
)
if gc scheduler jobs describe "${SWEEP_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1; then
  gc scheduler jobs update http "${SWEEP_SCHEDULER}" "${sched_args[@]}" >/dev/null
  # A schedule somebody paused by hand stays paused through an update. This one has no off
  # switch - a hosted deployment always has stalled jobs to fail - so a deploy resumes it.
  gc scheduler jobs resume "${SWEEP_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1 || true
  SCHED_DISPOSITION=updated
else
  gc scheduler jobs create http "${SWEEP_SCHEDULER}" "${sched_args[@]}" \
    --description "Runs the ${DEPLOYMENT_ID} maintenance sweep: stalled jobs, expired uploads, orphaned objects" >/dev/null
  SCHED_DISPOSITION=created
fi

# THE GRANT IS VERIFIED, NOT HOPED FOR. Without it every tick gets 403 and stalled jobs go unreaped
# behind a deploy that looked green - so a failed add is only acceptable if the binding is already
# there (a deploy identity that may not SET job IAM re-running against one an owner already set).
if ! gc run jobs add-iam-policy-binding "${SWEEP_JOB}" \
      --region "${GCP_REGION}" \
      --member "serviceAccount:${SWEEP_SA_EMAIL}" \
      --role roles/run.invoker >/dev/null 2>&1; then
  gc run jobs get-iam-policy "${SWEEP_JOB}" --region "${GCP_REGION}" \
      --flatten "bindings[].members" --filter "bindings.role=roles/run.invoker" \
      --format "value(bindings.members)" 2>/dev/null \
    | grep -qx "serviceAccount:${SWEEP_SA_EMAIL}" \
    || die "${SWEEP_SA_EMAIL} may not invoke ${SWEEP_JOB}, and this identity could not grant it.
   Every scheduled tick would get 403 and stalled jobs would go unreaped. As an owner:
     gcloud run jobs add-iam-policy-binding ${SWEEP_JOB} --project ${GCP_PROJECT_ID} \\
       --region ${GCP_REGION} --member serviceAccount:${SWEEP_SA_EMAIL} --role roles/run.invoker"
  log "run.invoker on ${SWEEP_JOB} already held by ${SWEEP_SA_EMAIL}"
fi

log "maintenance sweep ${SWEEP_JOB}"
log "  identity        ${SWEEP_SA_EMAIL}"
log "  image           ${APP_IMAGE}"
log "  schedule        ${SWEEP_SCHEDULE} (UTC)  [${SCHED_DISPOSITION}]"
log "done"
