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

# THE LOOK'S READER. A deployment whose configured reader has no key looks at nothing and stores "the
# configured reader has no key in this environment" on every single upload, quietly, per job, in a log.
#
# WHAT THIS LAYER CAN AND CANNOT PROVE. generated.env carries container NAMES, never values, so all that
# can be checked here is whether a container is named at all - `create-secrets.sh` provisions this one as
# optional, so a named container may still hold no version. A named container is therefore reported as a
# WARNING and not a refusal: the refusal on the actual key belongs to the process that can read it, and
# settings/policy.py makes it at import, refusing to start a hosted deployment whose reader has no key.
# What IS refused here is the pair of configurations that cannot work however good the key is.
reader="${GEOMETRY_VISION_PROVIDER:-openai}"
reader_container=""
case "${reader}" in
  off)       reader_container="n/a" ;;
  openai)    reader_container="${OPENAI_API_KEY_SECRET:-}" ;;
  deepinfra) reader_container="${DEEPINFRA_API_KEY_SECRET:-}" ;;
  deepseek)  reader_container="${DEEPSEEK_API_KEY_SECRET:-}" ;;
  anthropic) reader_container="${ANTHROPIC_API_KEY_SECRET:-}" ;;
  *) add "GEOMETRY_VISION_PROVIDER '${reader}' is not a reader this deployment can use - one of openai, anthropic, deepinfra, deepseek, off. The application refuses to start on it, so this deploy would roll out a service that never comes up";;
esac
if [ -z "${reader_container}" ]; then
  warn "GEOMETRY_VISION_PROVIDER is ${reader} but no Secret Manager container is named for its key, so no
       upload would be looked at: every row would record that the reader has no key. bootstrap-env.sh
       writes OPENAI_API_KEY_SECRET by default, so an unset one was removed deliberately. Set it, or set
       GEOMETRY_VISION_PROVIDER=off to say this deployment takes no look - a hosted service refuses to
       start on the accident and starts on the statement."
fi

# A PROVIDER SWITCH IS A MODEL SWITCH, and this one cannot work at any key: the default model is an
# OpenAI model id, and pointed at another provider every look 404s. The rows would record that as a
# failed look rather than as the misconfiguration it is, so it is refused here.
if [ -n "${GEOMETRY_VISION_MODEL:-}" ] && [ "${reader}" != "openai" ] && [ "${reader}" != "off" ]; then
  case "${GEOMETRY_VISION_MODEL}" in
    gpt-*) add "GEOMETRY_VISION_PROVIDER is ${reader} but GEOMETRY_VISION_MODEL is '${GEOMETRY_VISION_MODEL}', an OpenAI model id that provider does not serve - every look would fail";;
  esac
fi

if [ ${#errs[@]} -gt 0 ]; then
  warn "configuration is invalid:"
  for e in "${errs[@]}"; do printf '    - %s\n' "${e}" >&2; done
  die "fix generated.env and rerun"
fi
info "configuration valid (mesh job: ${MESH_JOB_DISPOSITION}, bucket: ${MESH_BUCKET_DISPOSITION})"
