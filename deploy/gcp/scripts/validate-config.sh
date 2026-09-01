#!/usr/bin/env bash
# Responsibility: Validate the generated configuration as one typed schema before any cloud mutation.
# Boundaries: read-only and offline, so a misconfiguration fails here rather than half-way through provisioning.

# Validate generated.env as ONE typed contract before any cloud mutation. This is the single
# authoritative schema check - read-only, no gcloud calls - so a misconfiguration fails here with
# one precise instruction rather than half-way through provisioning.
#
# It validates the mesh executor, plus the two tiers a deployment may DECLARE beside it: the hosted
# database whose schema the deploy migrates, and the worker fleet whose queue depth it publishes.
# Neither is discovered, so each is checked for being fully stated rather than half stated.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env

errs=()
add() { errs+=("$1"); }

for v in DEPLOYMENT_ID GCP_PROJECT_ID GCP_PROJECT_NUMBER GCP_REGION \
         ARTIFACT_REGISTRY_REPOSITORY CLOUDRUN_MESH_JOB MESH_SERVICE_ACCOUNT GCP_MESH_BUCKET \
         MESH_JOB_DISPOSITION MESH_SA_DISPOSITION MESH_BUCKET_DISPOSITION; do
  [ -n "${!v:-}" ] || add "missing required config: ${v}"
done

# Each mesh resource is either created by this tooling or pre-existing and validated. The
# disposition is recorded per resource, so a project that already owns a mesh job can still let
# this tooling create the bucket beside it.
for d in "${MESH_JOB_DISPOSITION:-}" "${MESH_SA_DISPOSITION:-}" "${MESH_BUCKET_DISPOSITION:-}"; do
  case "${d}" in created|reused) ;; *) add "resource disposition must be created|reused (got '${d}')";; esac
done

# service-account ID syntax (gcloud: 6-30 chars, lower-alnum + hyphen, start letter)
[[ "${MESH_SERVICE_ACCOUNT:-}" =~ ^[a-z][a-z0-9-]{5,29}$ ]] \
  || add "service-account id '${MESH_SERVICE_ACCOUNT:-}' is not a valid GCP account id (6-30 chars, [a-z][a-z0-9-])"

# GCS bucket naming, checked here so provisioning fails on the schema rather than on the API call
[[ "${GCP_MESH_BUCKET:-}" =~ ^[a-z0-9][a-z0-9._-]{2,62}$ ]] \
  || add "GCP_MESH_BUCKET '${GCP_MESH_BUCKET:-}' is not a valid bucket name"

# THE TWO OPTIONAL TIERS. Each is either fully declared or not declared at all. A HALF-declared one
# is the dangerous state: run-migrations.sh reads a missing database host as "this deployment has no
# database" and skips, so a deployment that named its password secret and forgot its host would look
# like a deliberate skip rather than the mistake it is.
if [ -n "${MIGRATE_DB_HOST:-}" ] || [ -n "${POSTGRES_PASSWORD_SECRET:-}" ]; then
  [ -n "${MIGRATE_DB_HOST:-}" ] \
    || add "POSTGRES_PASSWORD_SECRET is set but MIGRATE_DB_HOST is not - a migration target is host AND credential, or neither"
  [ -n "${POSTGRES_PASSWORD_SECRET:-}" ] \
    || add "MIGRATE_DB_HOST is set but POSTGRES_PASSWORD_SECRET is not - the migration reads the password by reference, never as a value"
  [[ "${MIGRATE_DB_PORT:-5432}" =~ ^[0-9]{1,5}$ ]] \
    || add "MIGRATE_DB_PORT '${MIGRATE_DB_PORT:-}' is not a port number"
  [[ "${MIGRATE_SERVICE_ACCOUNT:-}" =~ ^[a-z][a-z0-9-]{5,29}$ ]] \
    || add "service-account id '${MIGRATE_SERVICE_ACCOUNT:-}' is not a valid GCP account id (6-30 chars, [a-z][a-z0-9-])"
fi

if [ -n "${WORKER_MIG:-}" ] || [ -n "${WORKER_MIG_ZONE:-}" ]; then
  [ -n "${WORKER_MIG:-}" ]      || add "WORKER_MIG_ZONE is set but WORKER_MIG is not - name the instance group the metric scales"
  [ -n "${WORKER_MIG_ZONE:-}" ] || add "WORKER_MIG is set but WORKER_MIG_ZONE is not - a managed instance group is zonal"
  [ -n "${REDIS_URL:-}" ]       || add "WORKER_MIG is set but REDIS_URL is not - the queue's depth is read from the broker"
  [[ "${QUEUE_DEPTH_SERVICE_ACCOUNT:-}" =~ ^[a-z][a-z0-9-]{5,29}$ ]] \
    || add "service-account id '${QUEUE_DEPTH_SERVICE_ACCOUNT:-}' is not a valid GCP account id (6-30 chars, [a-z][a-z0-9-])"
  for n in WORKER_MIG_MIN_REPLICAS WORKER_MIG_MAX_REPLICAS WORKER_MIG_COOLDOWN_SECONDS WORKER_JOBS_PER_INSTANCE; do
    [[ "${!n:-}" =~ ^[0-9]+$ ]] || add "${n} '${!n:-}' is not a whole number"
  done
  # The max replica count is the environment's COST CEILING, so it is a deliberate number and not
  # something a typo may invert.
  if [[ "${WORKER_MIG_MIN_REPLICAS:-}" =~ ^[0-9]+$ ]] && [[ "${WORKER_MIG_MAX_REPLICAS:-}" =~ ^[0-9]+$ ]]; then
    [ "${WORKER_MIG_MIN_REPLICAS}" -le "${WORKER_MIG_MAX_REPLICAS}" ] \
      || add "WORKER_MIG_MIN_REPLICAS (${WORKER_MIG_MIN_REPLICAS}) exceeds WORKER_MIG_MAX_REPLICAS (${WORKER_MIG_MAX_REPLICAS})"
  fi
  [ "${WORKER_JOBS_PER_INSTANCE:-1}" != "0" ] \
    || add "WORKER_JOBS_PER_INSTANCE is 0 - the autoscaler divides the queue depth by it"
fi

if [ ${#errs[@]} -gt 0 ]; then
  warn "configuration is invalid:"
  for e in "${errs[@]}"; do printf '    - %s\n' "${e}" >&2; done
  die "fix generated.env and rerun"
fi
info "configuration valid (mesh job: ${MESH_JOB_DISPOSITION}, bucket: ${MESH_BUCKET_DISPOSITION})"
