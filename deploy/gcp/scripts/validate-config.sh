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
# A THIRD STATE EXISTS, and conflating it with the half-declared one broke a deploy. An address is
# allocated by Google when the instance is created, so a deployment that NAMES its Cloud SQL
# instance but has no host yet is not half-declared - it is fully declared and not yet built.
# bootstrap-env.sh resolves the address from the named instance when one exists; when it does not,
# the data tier stage is what creates it. Only a deployment that names NEITHER a host nor an
# instance is the mistake this rule is for.
if [ -n "${MIGRATE_DB_HOST:-}" ] || [ -n "${POSTGRES_PASSWORD_SECRET:-}" ]; then
  [ -n "${MIGRATE_DB_HOST:-}" ] || [ -n "${CLOUDSQL_INSTANCE:-}" ] \
    || add "POSTGRES_PASSWORD_SECRET is set but neither MIGRATE_DB_HOST nor CLOUDSQL_INSTANCE is - a migration target is an address or the instance that has one"
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
  # Same third state as the migration block above: a named Memorystore instance that does not
  # exist yet has no address, and that is a deploy waiting to happen rather than a broken config.
  [ -n "${REDIS_URL:-}" ] || [ -n "${REDIS_INSTANCE:-}" ] \
    || add "WORKER_MIG is set but neither REDIS_URL nor REDIS_INSTANCE is - the queue's depth is read from the broker"
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

# THE API SERVICE. Empty CLOUDRUN_API_SERVICE means this deployment serves no API and the stage is
# skipped, so nothing below applies.
if [ -n "${CLOUDRUN_API_SERVICE:-}" ]; then
  if [[ "${API_MIN_INSTANCES:-}" =~ ^[0-9]+$ ]] && [[ "${API_MAX_INSTANCES:-}" =~ ^[0-9]+$ ]]; then
    [ "${API_MIN_INSTANCES}" -le "${API_MAX_INSTANCES}" ] \
      || add "API_MIN_INSTANCES (${API_MIN_INSTANCES}) exceeds API_MAX_INSTANCES (${API_MAX_INSTANCES})"
  fi
  # A service reaching a private Cloud SQL and Memorystore needs the network it egresses into.
  [ -n "${VPC_NETWORK:-}" ] && [ -n "${VPC_SUBNET:-}" ] \
    || add "CLOUDRUN_API_SERVICE is set but VPC_NETWORK/VPC_SUBNET are not - the service could not reach the private data tier"
fi

# THE CONSOLE. Empty CLOUDRUN_CONSOLE_SERVICE means this deployment serves no browser console and
# the stage is skipped, so nothing below applies. A console that IS declared must be able to reach
# the API and to resolve its two credentials, because both failures present only at runtime: an
# unreachable API is a console that renders and then 503s, and a missing AUTH_SECRET is a revision
# that never becomes ready.
#
# console_selected - mirrors deploy.sh's own `want()`: DEPLOY_COMPONENTS is a comma-separated list
# or `all`, and UNSET means `all`, never "less" (deploy.sh defaults it the same way before
# exporting it). Only the HEXERA_API_BASE_URL check below is gated on this: it is the one check
# that depends on a LIVE resource (the API service's discovered URL) rather than static
# configuration, and on a fresh environment that resource does not exist until stage 14 - twelve
# stages after this one. A run that selects, say, only 'images' must not be refused at stage 2 for
# a console it is not touching this run.
console_selected() {
  case ",${DEPLOY_COMPONENTS:-all}," in
    *,all,*) return 0 ;;
    *,console,*) return 0 ;;
    *) return 1 ;;
  esac
}
if [ -n "${CLOUDRUN_CONSOLE_SERVICE:-}" ]; then
  if console_selected; then
    [ -n "${HEXERA_API_BASE_URL:-}" ] \
      || add "CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not - the console's /api/v1 proxy would have no origin to forward to"
  fi
  [ -n "${AUTH_SECRET_SECRET:-}" ] \
    || add "the console is configured but AUTH_SECRET_SECRET names no Secret Manager container - Auth.js refuses to start without a secret"
  [ -n "${CONSOLE_AUTH_USERS_SECRET:-}" ] \
    || add "the console is configured but CONSOLE_AUTH_USERS_SECRET names no Secret Manager container - nobody could sign in"
  if [[ "${CONSOLE_MIN_INSTANCES:-}" =~ ^[0-9]+$ ]] && [[ "${CONSOLE_MAX_INSTANCES:-}" =~ ^[0-9]+$ ]]; then
    [ "${CONSOLE_MIN_INSTANCES}" -le "${CONSOLE_MAX_INSTANCES}" ] \
      || add "CONSOLE_MIN_INSTANCES (${CONSOLE_MIN_INSTANCES}) exceeds CONSOLE_MAX_INSTANCES (${CONSOLE_MAX_INSTANCES})"
  fi
fi

# THE OBJECT STORE. The access key is the PUBLIC half; its secret is a container name. Both or
# neither - a half-configured store fails at the first upload rather than here.
if [ -n "${MINIO_ACCESS_KEY:-}" ] || [ -n "${MINIO_BUCKET:-}" ]; then
  [ -n "${MINIO_BUCKET:-}" ]            || add "MINIO_ACCESS_KEY is set but MINIO_BUCKET is not"
  [ -n "${MINIO_SECRET_KEY_SECRET:-}" ] || add "the object store is configured but MINIO_SECRET_KEY_SECRET names no Secret Manager container"
  # Google Cloud Storage's S3 endpoint serves TLS only; MINIO_SECURE=false against it fails every
  # request, and the adapter defaults to false for the local plain-HTTP stack.
  if [ "${MINIO_ENDPOINT:-}" = "storage.googleapis.com" ] && [ "${MINIO_SECURE:-}" != "true" ]; then
    add "MINIO_ENDPOINT is Google Cloud Storage but MINIO_SECURE is '${MINIO_SECURE:-unset}' - that endpoint refuses plain HTTP"
  fi
fi

# THE DATA TIER. A declared database must name the container its password lives in; the value is
# never here. The reverse - a container named with no host - is a leftover, not a configuration.
if [ -n "${MIGRATE_DB_HOST:-}" ]; then
  [ -n "${POSTGRES_PASSWORD_SECRET:-}" ] \
    || add "MIGRATE_DB_HOST is set but POSTGRES_PASSWORD_SECRET names no Secret Manager container"
fi

if [ ${#errs[@]} -gt 0 ]; then
  warn "configuration is invalid:"
  for e in "${errs[@]}"; do printf '    - %s\n' "${e}" >&2; done
  die "fix generated.env and rerun"
fi
info "configuration valid (mesh job: ${MESH_JOB_DISPOSITION}, bucket: ${MESH_BUCKET_DISPOSITION})"
