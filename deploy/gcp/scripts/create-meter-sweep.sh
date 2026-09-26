#!/usr/bin/env bash
# Responsibility: Provision the meter sweep as a scheduled Cloud Run Job, and the schedule that runs it.
# Owns: the job, its schedule, and the invoker grant the scheduler needs.
# Boundaries: it reports usage the ledger already recorded; what counts as overage, and why a
#             repeated report is harmless, is application/metering_service.py.

# Provision the METER SWEEP. Idempotent and reconciling: an existing job is updated in place.
#
#   bash deploy/gcp/scripts/create-meter-sweep.sh
#
# WHY THIS EXISTS. A job's charge is written to the ledger inside the transaction that ends it; the
# part of that charge a subscriber's allowance did not cover has to reach Stripe's meter too, and
# that is an HTTP call that cannot ride the transaction. Something has to carry it afterwards. The
# Celery beat schedule that does so on the local stack does not exist in a hosted deployment - the
# fleet runs only the simulation queue and nothing runs beat - so without this job overage was
# recorded and never billed.
#
# WHY A JOB, AS THE API'S OWN IDENTITY. It is the queue-depth publisher's and the outreach sender's
# shape: a scheduled one-shot, not a service nobody calls. It runs the APPLICATION image by digest
# under the API's service account because that identity already holds exactly the grants a sweep
# needs - the database password and the two Stripe secrets - and a second identity would need the
# same grants and double the places a payment credential can be read from. It is NOT driven through
# the admin HTTP route: that would put ADMIN_API_KEY, a cross-tenant credential, in the scheduler's
# configuration in the clear.
#
# INPUTS   the deployment env (APP_IMAGE, MIGRATE_DB_*, VPC_*, the *_SECRET names)
# MUTATES  the meter-sweep job, its schedule, and the scheduler's invoker binding.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

# BILLING IS OPT-IN PER DEPLOYMENT. Without both Stripe credentials there is no gateway and nothing
# to report to - the API answers its billing routes 503 - so this is a stated skip, like every other
# optional tier. Both, because settings/billing.enabled() requires both.
METER_JOB="${METER_SWEEP_JOB:-${DEPLOYMENT_ID}-meter-sweep}"
METER_SCHEDULER="${METER_JOB}-tick"
if [ -z "${STRIPE_API_KEY_SECRET:-}" ] || [ -z "${STRIPE_WEBHOOK_SECRET_SECRET:-}" ]; then
  info "No billing configured - skipping the meter sweep"
  log "set STRIPE_API_KEY_SECRET and STRIPE_WEBHOOK_SECRET_SECRET to bill overage from this deployment"
  # TURNING BILLING OFF MUST STOP THE SWEEP TOO. A job provisioned while billing was on keeps its
  # own Stripe secret references, so leaving its schedule running would go on reporting to the
  # meter after the API stopped charging. Paused, not deleted: turning billing back on resumes it,
  # and the job's history stays readable.
  if gc scheduler jobs describe "${METER_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1; then
    gc scheduler jobs pause "${METER_SCHEDULER}" --location "${GCP_REGION}" >/dev/null \
      || die "billing is off but the existing schedule ${METER_SCHEDULER} could not be paused, so
   the meter sweep would keep reporting usage to Stripe:
     gcloud scheduler jobs pause ${METER_SCHEDULER} --location ${GCP_REGION} --project ${GCP_PROJECT_ID}"
    log "paused the existing schedule ${METER_SCHEDULER}"
  fi
  exit 0
fi
# A sweep with no database has no ledger to read.
if [ -z "${MIGRATE_DB_HOST:-}" ] || [ -z "${POSTGRES_PASSWORD_SECRET:-}" ]; then
  die "billing is configured but this deployment names no database (MIGRATE_DB_HOST,
   POSTGRES_PASSWORD_SECRET), so the meter sweep would have no ledger to read. Overage would be
   recorded and never billed - refusing rather than provisioning a job that can only fail."
fi

require_digest_reference APP_IMAGE "${APP_IMAGE:-}"

METER_SA="${API_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-api}"
METER_SA_EMAIL="${METER_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
# EVERY FIFTEEN MINUTES. Stripe prices a meter at the period close, so the cadence decides only how
# stale the customer's upcoming-invoice preview is - and how much overage straddles a period
# boundary into the next invoice. Fifteen minutes keeps both small at a few cents of job time a day.
METER_SCHEDULE="${METER_SWEEP_SCHEDULE:-*/15 * * * *}"

info "Meter sweep ${METER_JOB} in ${GCP_REGION}"
log "image (validated digest): ${APP_IMAGE}"

JOB_ENV=(
  "ENV=${APP_ENV:-}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "POSTGRES_HOST=${MIGRATE_DB_HOST}"
  "POSTGRES_PORT=${MIGRATE_DB_PORT:-5432}"
  "POSTGRES_DB=${MIGRATE_DB_NAME:-meshpipeline}"
  "POSTGRES_USER=${MIGRATE_DB_USER:-meshpipeline}"
)
for entry in "${JOB_ENV[@]}"; do
  case "${entry}" in
    *"|"*) die "the setting '${entry%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with." ;;
  esac
done

JOB_SECRETS=()
for pair in "POSTGRES_PASSWORD:POSTGRES_PASSWORD_SECRET" \
            "STRIPE_API_KEY:STRIPE_API_KEY_SECRET" \
            "STRIPE_WEBHOOK_SECRET:STRIPE_WEBHOOK_SECRET_SECRET"; do
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
  --service-account "${METER_SA_EMAIL}"
  --command python
  --args "-m,meshpipeline.runtime.meter_sweep"
  # ONE RETRY. The sweep is idempotent - the ledger row id is the provider's idempotency key and a
  # row is stamped only after the provider accepted it - so a retry is safe; more than one only
  # delays the next tick, which retries anyway.
  --max-retries 1
  --task-timeout 600s
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${JOB_ENV[*]}")"
  --set-secrets "$(IFS=,; printf '%s' "${JOB_SECRETS[*]}")"
  # THE PRIVATE ROUTE to Cloud SQL's private address - the same three flags the API service carries.
  --network "${VPC_NETWORK}"
  --subnet "${VPC_SUBNET}"
  --vpc-egress private-ranges-only
  --labels "app=hexera,component=meter-sweep,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
)

if gc run jobs describe "${METER_JOB}" --region "${GCP_REGION}" >/dev/null 2>&1; then
  info "Updating meter sweep job ${METER_JOB}"
  gc run jobs update "${METER_JOB}" "${job_args[@]}"
else
  info "Creating meter sweep job ${METER_JOB}"
  gc run jobs create "${METER_JOB}" "${job_args[@]}"
fi

SCHEDULER_URI="https://${GCP_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT_ID}/jobs/${METER_JOB}:run"
gc services enable cloudscheduler.googleapis.com >/dev/null 2>&1 || true
sched_args=(
  --location "${GCP_REGION}"
  --schedule "${METER_SCHEDULE}"
  --time-zone UTC
  --uri "${SCHEDULER_URI}"
  --http-method POST
  --oauth-service-account-email "${METER_SA_EMAIL}"
  --attempt-deadline 60s
)
if gc scheduler jobs describe "${METER_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1; then
  gc scheduler jobs update http "${METER_SCHEDULER}" "${sched_args[@]}" >/dev/null
  # A schedule paused when billing was last turned off stays paused through an update.
  gc scheduler jobs resume "${METER_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1 || true
  SCHED_DISPOSITION=updated
else
  gc scheduler jobs create http "${METER_SCHEDULER}" "${sched_args[@]}" \
    --description "Reports ${DEPLOYMENT_ID} overage to the billing provider's meter" >/dev/null
  SCHED_DISPOSITION=created
fi

# THE GRANT IS VERIFIED, NOT HOPED FOR. Without it every tick gets 403 and overage goes unbilled
# behind a deploy that looked green - so a failed add is only acceptable if the binding is already
# there (a deploy identity that may not SET job IAM re-running against one an owner already set).
if ! gc run jobs add-iam-policy-binding "${METER_JOB}" \
      --region "${GCP_REGION}" \
      --member "serviceAccount:${METER_SA_EMAIL}" \
      --role roles/run.invoker >/dev/null 2>&1; then
  gc run jobs get-iam-policy "${METER_JOB}" --region "${GCP_REGION}" \
      --flatten "bindings[].members" --filter "bindings.role=roles/run.invoker" \
      --format "value(bindings.members)" 2>/dev/null \
    | grep -qx "serviceAccount:${METER_SA_EMAIL}" \
    || die "${METER_SA_EMAIL} may not invoke ${METER_JOB}, and this identity could not grant it.
   Every scheduled sweep would get 403 and overage would go unbilled. As an owner:
     gcloud run jobs add-iam-policy-binding ${METER_JOB} --project ${GCP_PROJECT_ID} \\
       --region ${GCP_REGION} --member serviceAccount:${METER_SA_EMAIL} --role roles/run.invoker"
  log "run.invoker on ${METER_JOB} already held by ${METER_SA_EMAIL}"
fi

log "meter sweep      ${METER_JOB}"
log "  identity       ${METER_SA_EMAIL}"
log "  image          ${APP_IMAGE}"
log "  schedule       ${METER_SCHEDULE} (UTC)  [${SCHED_DISPOSITION}]"
log "done"
