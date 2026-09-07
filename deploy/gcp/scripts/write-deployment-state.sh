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

# WHAT THIS RUN ACTUALLY RECONCILED. A partial deploy (DEPLOY_COMPONENTS) leaves some workloads
# untouched, and a manifest that records the freshly promoted digests regardless would claim bytes
# were deployed that are not running. This file is read by deploy-preflight.sh to decide whether an
# artifact may be promoted, so a wrong answer here is not a cosmetic one.
COMPONENTS="${DEPLOY_COMPONENTS:-all}"
_selected() { case ",${COMPONENTS}," in *,all,*) return 0 ;; *",$1,"*) return 0 ;; *) return 1 ;; esac; }

if _selected images; then
  MESH_DIGEST="$(resolve_digest "${MESH_IMAGE:-}" 2>/dev/null || printf '%s' "${MESH_IMAGE:-}")"
  APP_DIGEST="$(resolve_digest "${APP_IMAGE:-}" 2>/dev/null || printf '%s' "${APP_IMAGE:-}")"
else
  # READ BACK FROM THE WORKLOADS, not from the release record. promote-release.sh runs on every
  # deploy and sets MESH_IMAGE/APP_IMAGE to the digests this commit validated - but nothing
  # deployed them this time, so the truthful value is whatever the mesh job and the API service
  # are actually running. An absent workload records an empty string rather than a digest it does
  # not have.
  MESH_DIGEST="$(gc run jobs describe "${CLOUDRUN_MESH_JOB:-}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [ -n "${CLOUDRUN_API_SERVICE:-}" ]; then
    APP_DIGEST="$(gc run services describe "${CLOUDRUN_API_SERVICE}" --region "${GCP_REGION}" \
      --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  else
    APP_DIGEST=""
  fi
fi

# TWO DIFFERENT QUESTIONS, kept apart because one is durable and one is about this run.
#
#   disposition  - OWNERSHIP. Did this tooling create the resource, or was it supplied? It does not
#                  change because a later run declined to look at it, and mesh-destroy.sh deletes
#                  only what is recorded as `created` - so overwriting this with a per-run value
#                  would make a partial deploy quietly disown everything it did not touch.
#   reconciled   - DID THIS RUN TOUCH IT. False means the digests and settings recorded beside it
#                  were observed, not applied.
#
# For an unreconciled resource the ownership answer is CARRIED FORWARD from the manifest the last
# run wrote, because that is the last time anybody actually established it.
_prior() {  # _prior <resource-key> - the disposition the previous manifest recorded, or empty
  [ -f "${OUT}" ] || return 0
  python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
r = d.get("resources", {}).get(sys.argv[2]) or {}
v = r.get("disposition", "")
print(v if v != "unknown" else "")' "${OUT}" "$1" 2>/dev/null || true
}
_disp() {  # _disp <component> <this-run-disposition> <resource-key>
  if _selected "$1"; then
    printf '%s' "${2:-created}"
  else
    _p="$(_prior "$3")"
    printf '%s' "${_p:-unknown}"
  fi
}
# CAPITALISED, because the heredoc below is Python source and not JSON. A bare `true` there is an
# undefined name, and the manifest write fails with a NameError after the deploy has finished -
# the one point at which a failure is most confusing and least recoverable.
_recon() { if _selected "$1"; then printf 'True'; else printf 'False'; fi; }

MESH_JOB_DISP="$(_disp images "${MESH_JOB_DISPOSITION:-}" mesh_job)"
API_DISP="$(_disp images "${API_SERVICE_DISPOSITION:-}" api_service)"
SQL_DISP="$(_disp data "${CLOUDSQL_DISPOSITION:-}" cloud_sql)"
REDIS_DISP="$(_disp data "${REDIS_DISPOSITION:-}" memorystore)"
STORE_DISP="$(_disp storage "${ARTIFACTS_BUCKET_DISPOSITION:-}" object_store)"
FLEET_DISP="$(_disp workers "${WORKER_MIG_DISPOSITION:-}" worker_fleet)"
QUEUE_DISP="$(_disp queue created queue_depth_job)"
MIGRATE_DISP="$(_disp migrate created migration_job)"

MESH_JOB_RECON="$(_recon images)";  API_RECON="$(_recon images)"
SQL_RECON="$(_recon data)";         REDIS_RECON="$(_recon data)"
STORE_RECON="$(_recon storage)";    FLEET_RECON="$(_recon workers)"
QUEUE_RECON="$(_recon queue)";      MIGRATE_RECON="$(_recon migrate)"

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
    "mesh_job":             {"name": "${CLOUDRUN_MESH_JOB}",  "disposition": "${MESH_JOB_DISP}", "reconciled": ${MESH_JOB_RECON}},
    "exchange_bucket":      {"name": "${GCP_MESH_BUCKET}",    "disposition": "${MESH_BUCKET_DISPOSITION}"},
    "mesh_service_account": {"name": "${MESH_SA_EMAIL}",      "disposition": "${MESH_SA_DISPOSITION}"},
  },
  # WHICH TIERS THIS RUN TOUCHED. A consumer that assumes every manifest describes a full
  # reconcile would otherwise read a partial deploy as a complete one.
  "components": "${COMPONENTS}",
  # A NAME-TO-DIGEST MAP AND NOTHING ELSE. Whether these were deployed by this run or observed on
  # the running workloads is already answered per workload, by `reconciled` on mesh_job and
  # api_service - saying it a second time here cost `set(doc["images"])` its meaning, which is
  # exactly what a consumer iterating for digests relies on.
  "images": {"mesh": "${MESH_DIGEST}", "app": "${APP_DIGEST}"},
}
if "${MIGRATE_DB_HOST:-}":
  doc["resources"]["migration_job"] = {
    "name": "${CLOUDRUN_MIGRATE_JOB:-}", "disposition": "${MIGRATE_DISP}", "reconciled": ${MIGRATE_RECON},
    # The database is recorded by ADDRESS and never by URL: a DSN would carry the credential this
    # manifest exists to prove it does not hold.
    "database": "${MIGRATE_DB_HOST:-}:${MIGRATE_DB_PORT:-5432}/${MIGRATE_DB_NAME:-}",
    "password_secret": "${POSTGRES_PASSWORD_SECRET:-}",
  }
if "${WORKER_MIG:-}":
  doc["resources"]["queue_depth_job"] = {
    "name": "${CLOUDRUN_QUEUE_DEPTH_JOB:-}", "disposition": "${QUEUE_DISP}", "reconciled": ${QUEUE_RECON},
    "schedule": "${QUEUE_DEPTH_SCHEDULE:-}", "queue": "${QUEUE_NAME:-}",
    "scales": "${WORKER_MIG:-}", "zone": "${WORKER_MIG_ZONE:-}",
    "replicas": "${WORKER_MIG_MIN_REPLICAS:-}..${WORKER_MIG_MAX_REPLICAS:-}",
    "jobs_per_instance": "${WORKER_JOBS_PER_INSTANCE:-}",
  }
if "${CLOUDSQL_INSTANCE:-}":
  # By ADDRESS and tier, never by URL - the same reason the migration job records a host: a DSN
  # would carry the credential this manifest exists to prove it does not hold.
  doc["resources"]["cloud_sql"] = {
    "name": "${CLOUDSQL_INSTANCE:-}", "disposition": "${SQL_DISP}", "reconciled": ${SQL_RECON},
    "connection_name": "${CLOUDSQL_CONNECTION_NAME:-}", "tier": "${CLOUDSQL_TIER:-}",
  }
if "${REDIS_INSTANCE:-}":
  doc["resources"]["memorystore"] = {
    "name": "${REDIS_INSTANCE:-}", "disposition": "${REDIS_DISP}", "reconciled": ${REDIS_RECON},
    "tier": "${REDIS_TIER:-}",
  }
if "${MINIO_BUCKET:-}":
  # The access key is the PUBLIC half and is recorded so a later run can prove which credential
  # this deployment uses without minting another. The secret half is a container NAME.
  doc["resources"]["object_store"] = {
    "bucket": "${MINIO_BUCKET:-}", "disposition": "${STORE_DISP}", "reconciled": ${STORE_RECON},
    "endpoint": "${MINIO_ENDPOINT:-}", "access_key": "${MINIO_ACCESS_KEY:-}",
    "secret_container": "${MINIO_SECRET_KEY_SECRET:-}",
    "service_account": "${OBJECT_STORE_SERVICE_ACCOUNT:-}",
  }
if "${CLOUDRUN_API_SERVICE:-}":
  doc["resources"]["api_service"] = {
    "name": "${CLOUDRUN_API_SERVICE:-}", "disposition": "${API_DISP}", "reconciled": ${API_RECON},
    "service_account": "${API_SERVICE_ACCOUNT:-}",
    "instances": "${API_MIN_INSTANCES:-}..${API_MAX_INSTANCES:-}",
    "public": "${API_ALLOW_UNAUTHENTICATED:-0}" == "1",
  }
if "${WORKER_MIG:-}":
  # The TEMPLATE is the rotation record: a digest change makes a new template and the group rolls
  # onto it, so the template name is what says which bytes the fleet is actually running.
  doc["resources"]["worker_fleet"] = {
    "name": "${WORKER_MIG:-}", "disposition": "${FLEET_DISP}", "reconciled": ${FLEET_RECON},
    "zone": "${WORKER_MIG_ZONE:-}", "template": "${WORKER_TEMPLATE_NAME:-}",
    "service_account": "${WORKER_SERVICE_ACCOUNT:-}",
    "warm_floor": "${WORKER_MIG_MIN_REPLICAS:-}",
  }
json.dump(doc, open(out, "w"), indent=2)
print(out)
PY
info "Wrote deployment-state manifest: ${OUT}"
log "resources owned=created, reused=supplied"
