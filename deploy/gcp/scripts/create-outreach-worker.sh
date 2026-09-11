#!/usr/bin/env bash
# Responsibility: Provision the outreach sender as a scheduled Cloud Run Job, and the schedule that runs it.
# Owns: the job, its schedule, and the two grants the scheduler needs to invoke it.
# Boundaries: it never arms sending - that is the two-switch interlock, and both switches live
#             outside this script.

# Provision the OUTREACH SENDER. Idempotent and reconciling: an existing job is updated in place.
#
#   bash deploy/gcp/scripts/create-outreach-worker.sh
#
# WHY A JOB AND NOT A SERVICE. One tick is verify -> send -> poll. It answers no HTTP request, and
# a Cloud Run SERVICE that nobody calls either scales to zero and never runs, or holds an instance
# warm to do nothing. A Job on a schedule is the shape of the work.
#
# WHY THIS IS THE MOST DANGEROUS SCRIPT IN THE DEPLOY. Cold outreach from a laptop throttles
# itself: the machine is closed most of the day, and a mistake stops when the lid does. A job on a
# schedule has no such property - it will happily work through a contact list at 3am, and the
# people on the other end are real. Every safeguard below exists because of that asymmetry.
#
# THE TWO-SWITCH INTERLOCK IS NOT SET HERE, and cannot be. A message leaves only when DRY_RUN is
# false on the service AND live_sending is true in the database. This script sets DRY_RUN=1 on the
# job unless the deployment says otherwise, so a fresh provision is always a rehearsal: it renders
# every due message, records nothing, and sends nothing. Arming it is two deliberate acts by two
# different routes, which is the point.
#
# INPUTS   the deployment env (APP_IMAGE, OUTREACH_*, the *_SECRET names)
# MUTATES  the outreach job, its schedule, and the scheduler's invoker binding.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

OUTREACH_JOB="${OUTREACH_WORKER_JOB:-}"

# A deployment may run no outreach at all - it is a single prod-only instance. That is a skip and
# it is stated, the same way the fleet and the publisher state theirs.
if [ -z "${OUTREACH_JOB}" ]; then
  info "No outreach worker configured - skipping the outreach sender"
  log "set OUTREACH_WORKER_JOB to run the outreach engine from this deployment"
  exit 0
fi

require_digest_reference ADMIN_IMAGE "${ADMIN_IMAGE:-}"
require_vars OUTREACH_DB_HOST OUTREACH_DB_USER

OUTREACH_SA="${ADMIN_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-admin}"
OUTREACH_SA_EMAIL="${OUTREACH_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
OUTREACH_SCHEDULE="${OUTREACH_WORKER_SCHEDULE:-*/15 8-17 * * 1-5}"
OUTREACH_TZ="${OUTREACH_WORKER_TIMEZONE:-America/Los_Angeles}"

# THE SAME IDENTITY THE CONSOLE RUNS AS, deliberately. The job reads and writes the same outreach
# tables and opens the same KMS-sealed token; a second identity would need the same grants and
# would double the number of places a mailbox credential can be reached from.
info "Outreach sender ${OUTREACH_JOB} in ${GCP_REGION}"
log "image (validated digest): ${ADMIN_IMAGE}"

# The interlock's first switch, defaulted CLOSED. A deployment that wants a live sender must say so
# explicitly, and even then the database switch still has to agree.
OUTREACH_DRY_RUN="${DRY_RUN:-1}"
if [ "${OUTREACH_DRY_RUN}" = "0" ] || [ "${OUTREACH_DRY_RUN}" = "false" ]; then
  warn "DRY_RUN=${OUTREACH_DRY_RUN} - this job is permitted to send real mail once live_sending is
       also true in the database. Both switches are then on and messages reach real people."
else
  log "DRY_RUN=${OUTREACH_DRY_RUN} - rehearsal only; the job renders every due message and sends none"
fi

JOB_ENV=(
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "ENV=${APP_ENV:-}"
  "NODE_ENV=production"
  "DRY_RUN=${OUTREACH_DRY_RUN}"
  "OUTREACH_DB_HOST=${OUTREACH_DB_HOST}"
  "OUTREACH_DB_NAME=${OUTREACH_DB_NAME:-}"
  "OUTREACH_DB_USER=${OUTREACH_DB_USER}"
  "OUTREACH_KMS_KEY=${OUTREACH_KMS_KEY:-}"
  "GOOGLE_REDIRECT_URI=${GOOGLE_REDIRECT_URI:-}"
)
for entry in "${JOB_ENV[@]}"; do
  case "${entry}" in
    *"|"*) die "the setting '${entry%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with." ;;
  esac
done

JOB_SECRETS=()
for pair in "GOOGLE_CLIENT_SECRET:GOOGLE_CLIENT_SECRET_SECRET" \
            "OUTREACH_DB_PASSWORD:OUTREACH_DB_PASSWORD_SECRET" \
            "ANTHROPIC_API_KEY:ANTHROPIC_API_KEY_SECRET" \
            "VERIFIER_API_KEY:VERIFIER_API_KEY_SECRET"; do
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  JOB_SECRETS+=("${pair%%:*}=${secret_name}:latest")
done

job_args=(
  --image "${ADMIN_IMAGE}"
  --region "${GCP_REGION}"
  --service-account "${OUTREACH_SA_EMAIL}"
  --command node
  --args "apps/admin-console/outreach-worker.js,--once"
  --max-retries 1
  --task-timeout 900s
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${JOB_ENV[*]}")"
  --labels "app=hexera,component=outreach,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
)
if [ ${#JOB_SECRETS[@]} -gt 0 ]; then
  job_args+=(--set-secrets "$(IFS=,; printf '%s' "${JOB_SECRETS[*]}")")
fi
# The database is private-IP only, so the job reaches it the same way the console does.
[ -z "${OUTREACH_CLOUDSQL_INSTANCE:-}" ] || job_args+=(--set-cloudsql-instances "${OUTREACH_CLOUDSQL_INSTANCE}")

# MAX-RETRIES 1, NOT THE DEFAULT 3. A tick that fails halfway has already sent whatever it sent;
# retrying it re-runs the scheduler over the same due list, and the idempotency that makes that
# safe lives in the send ledger rather than in this job. One retry covers a transient database
# blip; three invites a duplicate send.
if gc run jobs describe "${OUTREACH_JOB}" --region "${GCP_REGION}" >/dev/null 2>&1; then
  info "Updating outreach job ${OUTREACH_JOB}"
  gc run jobs update "${OUTREACH_JOB}" "${job_args[@]}"
else
  info "Creating outreach job ${OUTREACH_JOB}"
  gc run jobs create "${OUTREACH_JOB}" "${job_args[@]}"
fi

# THE SCHEDULE, in BUSINESS HOURS ON WEEKDAYS by default. Not a cost decision: a cold email that
# arrives at 3am on a Sunday reads as a blast, and the engine's own send windows are per-campaign -
# a job that only wakes during plausible hours is a second, coarser guard on the same thing.
OUTREACH_SCHEDULER="${OUTREACH_JOB}-tick"
SCHEDULER_URI="https://${GCP_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT_ID}/jobs/${OUTREACH_JOB}:run"

gc services enable cloudscheduler.googleapis.com >/dev/null 2>&1 || true
sched_args=(
  --location "${GCP_REGION}"
  --schedule "${OUTREACH_SCHEDULE}"
  --time-zone "${OUTREACH_TZ}"
  --uri "${SCHEDULER_URI}"
  --http-method POST
  --oauth-service-account-email "${OUTREACH_SA_EMAIL}"
)
if gc scheduler jobs describe "${OUTREACH_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1; then
  gc scheduler jobs update http "${OUTREACH_SCHEDULER}" "${sched_args[@]}" >/dev/null
  SCHED_DISPOSITION=updated
else
  gc scheduler jobs create http "${OUTREACH_SCHEDULER}" "${sched_args[@]}" >/dev/null
  SCHED_DISPOSITION=created
fi

gc run jobs add-iam-policy-binding "${OUTREACH_JOB}" \
  --region "${GCP_REGION}" \
  --member "serviceAccount:${OUTREACH_SA_EMAIL}" \
  --role roles/run.invoker >/dev/null 2>&1 \
  || warn "could not grant run.invoker on ${OUTREACH_JOB} to ${OUTREACH_SA_EMAIL}; the schedule
       will fire and get 403 until this exists:
         gcloud run jobs add-iam-policy-binding ${OUTREACH_JOB} --project ${GCP_PROJECT_ID} \\
           --region ${GCP_REGION} --member serviceAccount:${OUTREACH_SA_EMAIL} --role roles/run.invoker"

log "outreach sender  ${OUTREACH_JOB}"
log "  identity       ${OUTREACH_SA_EMAIL}"
log "  image          ${ADMIN_IMAGE}"
log "  schedule       ${OUTREACH_SCHEDULE} (${OUTREACH_TZ})  [${SCHED_DISPOSITION}]"
log "  sending        DRY_RUN=${OUTREACH_DRY_RUN}; live_sending in the database must ALSO be true"
log "done"
