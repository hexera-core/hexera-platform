#!/usr/bin/env bash
# Responsibility: Retire the hand-made `hexera-<env>-*` resources once their `<env>-*` replacements are live.
# Owns: nothing. It deletes only names passed to it explicitly, and only after proving the replacement serves.
# Boundaries: READ-ONLY unless --delete is given; it never creates, never migrates data, and never guesses a name.

# THE OTHER HALF OF THE RENAME. deploy.sh reconciles resources BY EXACT NAME, so changing a
# deployment's names from `hexera-dev-*` to `dev-*` does not rename anything - it makes every
# lookup miss, and the provisioner creates a parallel stack beside the one that is serving. That
# is the intended cutover (the old stack was hand-made, drifted from `deploy/`, and is being
# replaced deliberately), but "intended" is not the same as "safe": left alone, the old stack
# keeps billing, and the old API keeps serving an image no release record names, against a
# database the new deploy is not migrating.
#
# So the cutover is two steps, and this is the second one:
#
#   1. Deploy. `dev-pg`, `dev-redis`, `dev-api`, `dev-workers` are created and migrated.
#   2. THIS. Prove each replacement exists, then delete the legacy resource it replaced.
#
#   bash deploy/gcp/scripts/decommission-legacy.sh                 # report only - deletes nothing
#   bash deploy/gcp/scripts/decommission-legacy.sh --delete        # after reading the report
#
# WHAT IT WILL NOT DO. It refuses to delete anything whose replacement it cannot see running, so a
# failed or partial deploy cannot be followed by a decommission that leaves the environment with
# neither stack. Cloud SQL is deleted LAST and only with --delete-database, because a database is
# the one resource here whose loss is not recoverable from `deploy/`.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

DO_DELETE=0
DELETE_DATABASE=0
for arg in "$@"; do
  case "${arg}" in
    --delete)          DO_DELETE=1 ;;
    --delete-database) DO_DELETE=1; DELETE_DATABASE=1 ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument '${arg}' - this takes --delete and --delete-database only" ;;
  esac
done

# The legacy prefix is DERIVED, not typed: these resources were named after the project, which is
# exactly the redundancy the rename removed (`hexera-dev-api` inside the project `hexera-dev`).
LEGACY_PREFIX="${LEGACY_PREFIX:-${GCP_PROJECT_ID}}"
ZONE="${WORKER_MIG_ZONE:-${GCP_REGION}-a}"

info "Legacy decommission for ${GCP_PROJECT_ID} (${LEGACY_PREFIX}-* -> ${DEPLOYMENT_ID}-*)"
[ "${DO_DELETE}" = "1" ] || log "REPORT ONLY - nothing will be deleted. Re-run with --delete to act."

FOUND=0
BLOCKED=0
PLAN=()

# plan <kind> <legacy-name> <replacement-name> <exists-legacy-cmd> <exists-replacement-cmd>
# Each row is decided on its own: a replacement that is missing blocks ONLY the resource it
# replaces, because a half-finished cutover should still be able to retire the parts that did land.
plan() {
  local kind="$1" legacy="$2" replacement="$3" legacy_cmd="$4" replacement_cmd="$5"
  if ! eval "${legacy_cmd}" >/dev/null 2>&1; then
    log "${kind}  ${legacy}  (absent - nothing to retire)"
    return 0
  fi
  FOUND=$((FOUND + 1))
  if eval "${replacement_cmd}" >/dev/null 2>&1; then
    log "${kind}  ${legacy}  -> ${replacement} is live  [RETIRE]"
    PLAN+=("${kind}|${legacy}")
  else
    BLOCKED=$((BLOCKED + 1))
    warn "${kind}  ${legacy}  -> ${replacement} NOT FOUND - refusing to retire this one"
  fi
}

plan "run service " "${LEGACY_PREFIX}-api" "${DEPLOYMENT_ID}-api" \
  "gc run services describe ${LEGACY_PREFIX}-api --region ${GCP_REGION}" \
  "gc run services describe ${DEPLOYMENT_ID}-api --region ${GCP_REGION}"

plan "worker MIG " "${LEGACY_PREFIX}-workers" "${DEPLOYMENT_ID}-workers" \
  "gc compute instance-groups managed describe ${LEGACY_PREFIX}-workers --zone ${ZONE}" \
  "gc compute instance-groups managed describe ${DEPLOYMENT_ID}-workers --zone ${ZONE}"

plan "redis      " "${LEGACY_PREFIX}-redis" "${DEPLOYMENT_ID}-redis" \
  "gc redis instances describe ${LEGACY_PREFIX}-redis --region ${GCP_REGION}" \
  "gc redis instances describe ${DEPLOYMENT_ID}-redis --region ${GCP_REGION}"

plan "cloud sql  " "${LEGACY_PREFIX}-pg" "${DEPLOYMENT_ID}-pg" \
  "gc sql instances describe ${LEGACY_PREFIX}-pg" \
  "gc sql instances describe ${DEPLOYMENT_ID}-pg"

if [ "${FOUND}" -eq 0 ]; then
  info "no ${LEGACY_PREFIX}-* resources remain - this environment is already on ${DEPLOYMENT_ID}-* names"
  exit 0
fi

if [ "${DO_DELETE}" != "1" ]; then
  printf '\n'
  info "${#PLAN[@]} resource(s) would be retired, ${BLOCKED} blocked on a missing replacement"
  log "re-run with --delete to retire them (Cloud SQL additionally needs --delete-database)"
  exit 0
fi

printf '\n'
for row in "${PLAN[@]}"; do
  kind="${row%%|*}"; name="${row##*|}"
  case "${kind}" in
    "run service ")
      info "Deleting Cloud Run service ${name}"
      gc run services delete "${name}" --region "${GCP_REGION}" --quiet >/dev/null \
        && log "retired  ${name}" ;;
    "worker MIG ")
      # The autoscaler is a separate object and must go first; deleting a group that still has one
      # leaves an autoscaler pointing at nothing, which is reported as a broken policy forever.
      info "Deleting autoscaler and MIG ${name}"
      gc compute instance-groups managed stop-autoscaling "${name}" --zone "${ZONE}" --quiet >/dev/null 2>&1 || true
      gc compute instance-groups managed delete "${name}" --zone "${ZONE}" --quiet >/dev/null \
        && log "retired  ${name}" ;;
    "redis      ")
      info "Deleting Memorystore instance ${name}"
      gc redis instances delete "${name}" --region "${GCP_REGION}" --quiet >/dev/null \
        && log "retired  ${name}" ;;
    "cloud sql  ")
      if [ "${DELETE_DATABASE}" != "1" ]; then
        warn "NOT deleting Cloud SQL ${name} - a database is the one thing here that deploy/ cannot
       recreate. Take a final export first, then re-run with --delete-database:
         gcloud sql export sql ${name} gs://<bucket>/${name}-final.sql.gz \\
           --database=${MIGRATE_DB_NAME:-meshpipeline} --project ${GCP_PROJECT_ID}"
        continue
      fi
      info "Deleting Cloud SQL instance ${name}"
      # Deletion protection is exactly what should stop this if somebody set it deliberately, so
      # it is reported rather than cleared.
      if gc sql instances delete "${name}" --quiet >/dev/null; then
        log "retired  ${name}"
      else
        die "could not delete ${name} - if it has deletion protection, clearing it is a
   deliberate act: gcloud sql instances patch ${name} --no-deletion-protection --project ${GCP_PROJECT_ID}"
      fi ;;
  esac
done

printf '\n\033[1m━━━ legacy decommission complete ━━━\033[0m\n'
log "verify what remains:  gcloud run services list --project ${GCP_PROJECT_ID}"
log "                      gcloud sql instances list --project ${GCP_PROJECT_ID}"
log "                      gcloud redis instances list --region ${GCP_REGION} --project ${GCP_PROJECT_ID}"
