#!/usr/bin/env bash
# Responsibility: Record what this deployment provisioned, and which resources it owns rather than reuses.
# Boundaries: names, dispositions and digests only - it holds no secret value and mutates no resource.

# Write the machine-readable DEPLOYMENT-STATE MANIFEST (deploy/output/deployment.json) after a
# successful mesh deployment. It records what exists and - crucially - which resources this
# tooling OWNS (created) versus REUSES (supplied), so diagnostics, reruns and upgrades act on
# recorded ownership rather than on a guessed prefix or operator memory.
#
# It describes what THIS tooling provisioned: the mesh executor, and the two tiers a deployment may
# declare beside it - the migration job that moved the schema, and the queue-depth publisher the
# worker fleet scales on. A tier this deployment does not declare is absent from the manifest rather
# than recorded as empty.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env

# DEPLOY_OUTPUT_DIR chooses where the manifest lands, defaulting to the deployment's own
# deploy/output/ - which is what every production path uses. Same shape as RELEASE_RECORD in
# deploy-preflight.sh: an override for a caller that must not write into the checkout.
OUT_DIR="${DEPLOY_OUTPUT_DIR:-${REPO_ROOT}/deploy/output}"
mkdir -p "${OUT_DIR}"
OUT="${OUT_DIR}/deployment.json"

MESH_DIGEST="$(resolve_digest "${MESH_IMAGE:-}" 2>/dev/null || printf '%s' "${MESH_IMAGE:-}")"
APP_DIGEST="$(resolve_digest "${APP_IMAGE:-}" 2>/dev/null || printf '%s' "${APP_IMAGE:-}")"

python3 - "${OUT}" <<PY
import datetime, json, sys
out = sys.argv[1]
doc = {
  "schema_version": 2,
  "product_version": "$(product_version)",
  "deployment_id": "${DEPLOYMENT_ID}",
  "project": "${GCP_PROJECT_ID}",
  "project_number": "${GCP_PROJECT_NUMBER}",
  "region": "${GCP_REGION}",
  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
  "resources": {
    "artifact_registry":    {"name": "${ARTIFACT_REGISTRY_REPOSITORY}", "disposition": "created"},
    "mesh_job":             {"name": "${CLOUDRUN_MESH_JOB}",  "disposition": "${MESH_JOB_DISPOSITION}"},
    "exchange_bucket":      {"name": "${GCP_MESH_BUCKET}",    "disposition": "${MESH_BUCKET_DISPOSITION}"},
    "mesh_service_account": {"name": "${MESH_SA_EMAIL}",      "disposition": "${MESH_SA_DISPOSITION}"},
  },
  "images": {"mesh": "${MESH_DIGEST}", "app": "${APP_DIGEST}"},
}
if "${MIGRATE_DB_HOST:-}":
  doc["resources"]["migration_job"] = {
    "name": "${CLOUDRUN_MIGRATE_JOB:-}", "disposition": "created",
    # The database is recorded by ADDRESS and never by URL: a DSN would carry the credential this
    # manifest exists to prove it does not hold.
    "database": "${MIGRATE_DB_HOST:-}:${MIGRATE_DB_PORT:-5432}/${MIGRATE_DB_NAME:-}",
    "password_secret": "${POSTGRES_PASSWORD_SECRET:-}",
  }
if "${WORKER_MIG:-}":
  doc["resources"]["queue_depth_job"] = {
    "name": "${CLOUDRUN_QUEUE_DEPTH_JOB:-}", "disposition": "created",
    "schedule": "${QUEUE_DEPTH_SCHEDULE:-}", "queue": "${QUEUE_NAME:-}",
    "scales": "${WORKER_MIG:-}", "zone": "${WORKER_MIG_ZONE:-}",
    "replicas": "${WORKER_MIG_MIN_REPLICAS:-}..${WORKER_MIG_MAX_REPLICAS:-}",
    "jobs_per_instance": "${WORKER_JOBS_PER_INSTANCE:-}",
  }
if "${CLOUDSQL_INSTANCE:-}":
  # By ADDRESS and tier, never by URL - the same reason the migration job records a host: a DSN
  # would carry the credential this manifest exists to prove it does not hold.
  doc["resources"]["cloud_sql"] = {
    "name": "${CLOUDSQL_INSTANCE:-}", "disposition": "${CLOUDSQL_DISPOSITION:-created}",
    "connection_name": "${CLOUDSQL_CONNECTION_NAME:-}", "tier": "${CLOUDSQL_TIER:-}",
  }
if "${REDIS_INSTANCE:-}":
  doc["resources"]["memorystore"] = {
    "name": "${REDIS_INSTANCE:-}", "disposition": "${REDIS_DISPOSITION:-created}",
    "tier": "${REDIS_TIER:-}",
  }
if "${MINIO_BUCKET:-}":
  # The access key is the PUBLIC half and is recorded so a later run can prove which credential
  # this deployment uses without minting another. The secret half is a container NAME.
  doc["resources"]["object_store"] = {
    "bucket": "${MINIO_BUCKET:-}", "disposition": "${ARTIFACTS_BUCKET_DISPOSITION:-created}",
    "endpoint": "${MINIO_ENDPOINT:-}", "access_key": "${MINIO_ACCESS_KEY:-}",
    "secret_container": "${MINIO_SECRET_KEY_SECRET:-}",
    "service_account": "${OBJECT_STORE_SERVICE_ACCOUNT:-}",
  }
if "${CLOUDRUN_API_SERVICE:-}":
  doc["resources"]["api_service"] = {
    "name": "${CLOUDRUN_API_SERVICE:-}", "disposition": "${API_SERVICE_DISPOSITION:-created}",
    "service_account": "${API_SERVICE_ACCOUNT:-}",
    "instances": "${API_MIN_INSTANCES:-}..${API_MAX_INSTANCES:-}",
    "public": "${API_ALLOW_UNAUTHENTICATED:-0}" == "1",
  }
if "${WORKER_MIG:-}":
  # The TEMPLATE is the rotation record: a digest change makes a new template and the group rolls
  # onto it, so the template name is what says which bytes the fleet is actually running.
  doc["resources"]["worker_fleet"] = {
    "name": "${WORKER_MIG:-}", "disposition": "${WORKER_MIG_DISPOSITION:-created}",
    "zone": "${WORKER_MIG_ZONE:-}", "template": "${WORKER_TEMPLATE_NAME:-}",
    "service_account": "${WORKER_SERVICE_ACCOUNT:-}",
    "warm_floor": "${WORKER_MIG_MIN_REPLICAS:-}",
  }
json.dump(doc, open(out, "w"), indent=2)
print(out)
PY
info "Wrote deployment-state manifest: ${OUT}"
log "resources owned=created, reused=supplied"
