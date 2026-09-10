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

# THE CONSOLE'S DIGEST, on the console's own selection. A run that reconciled the API but not the
# console must record what the console is actually running, not the digest this commit validated.
if _selected console; then
  CONSOLE_DIGEST="$(resolve_digest "${CONSOLE_IMAGE:-}" 2>/dev/null || printf '%s' "${CONSOLE_IMAGE:-}")"
elif [ -n "${CLOUDRUN_CONSOLE_SERVICE:-}" ]; then
  CONSOLE_DIGEST="$(gc run services describe "${CLOUDRUN_CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
else
  CONSOLE_DIGEST=""
fi

# THE ADMIN'S DIGEST, on the admin's own selection - same reasoning as the console's above.
if _selected admin; then
  ADMIN_DIGEST="$(resolve_digest "${ADMIN_IMAGE:-}" 2>/dev/null || printf '%s' "${ADMIN_IMAGE:-}")"
elif [ -n "${CLOUDRUN_ADMIN_SERVICE:-}" ]; then
  ADMIN_DIGEST="$(gc run services describe "${CLOUDRUN_ADMIN_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
else
  ADMIN_DIGEST=""
fi

# THE CERTIFICATE'S STATE AND THE RESERVED ADDRESS, both read back regardless of this run's own
# selection - either may have been provisioned by an earlier run, and an operator checking whether
# DNS is working needs to know what the edge IS, not merely which run last touched it.
#
# THE ADDRESS IS READ, NOT INHERITED. create-edge.sh sets EDGE_IP_ADDRESS as an in-process export,
# but deploy.sh runs every stage as its own `bash` process, so that value never reaches this one
# and the manifest field recorded "" on every real deploy - the field the edge exists to publish.
# EDGE_IP_NAME arrives here through generated.env exactly as EDGE_CERT does, so the address can
# simply be asked for, with no cross-process assumption to get wrong. Failure is tolerated the same
# way the certificate's is: a manifest is worth writing even when one read-back did not answer.
#
# THERE IS NO LONGER ONE CERTIFICATE TO DESCRIBE. create-edge.sh gives each hostname its own,
# named ${EDGE_CERT}-console and ${EDGE_CERT}-admin from the same base, because a managed
# certificate serves only once it is wholly ACTIVE and one shared certificate therefore gave every
# hostname one fate. EDGE_CERT still arrives here through generated.env, but as a BASE NAME - so
# describing it names nothing, and the manifest recorded an empty state for the field an operator
# reads to find out whether HTTPS is working. One record per hostname instead, each with the
# certificate that actually covers it.
if [ -n "${CONSOLE_DOMAIN:-}" ] || [ -n "${ADMIN_DOMAIN:-}" ]; then
  EDGE_CERT_REPORT=""
  for _edge_pair in "${CONSOLE_DOMAIN:-}|console" "${ADMIN_DOMAIN:-}|admin"; do
    _edge_host="${_edge_pair%%|*}"
    [ -n "${_edge_host}" ] || continue
    _edge_cert="${EDGE_CERT:-}-${_edge_pair##*|}"
    _edge_state="$(gc compute ssl-certificates describe "${_edge_cert}" --global \
      --format='value(managed.status)' 2>/dev/null || true)"
    EDGE_CERT_REPORT="${EDGE_CERT_REPORT}${_edge_host} ${_edge_cert} ${_edge_state:-unknown}
"
  done
  EDGE_IP_ADDRESS="$(gc compute addresses describe "${EDGE_IP_NAME:-}" --global \
    --format='value(address)' 2>/dev/null || true)"
else
  EDGE_CERT_REPORT=""
  EDGE_IP_ADDRESS=""
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
# CONSOLE_SERVICE_DISPOSITION is always empty in practice: each stage in deploy.sh runs as a
# separate `bash` process, so create-console-service.sh cannot export it back here. Same as
# API_SERVICE_DISPOSITION above - _disp's fallback to the prior manifest is what makes that fine.
CONSOLE_DISP="$(_disp console "${CONSOLE_SERVICE_DISPOSITION:-}" console_service)"
# ADMIN_SERVICE_DISPOSITION is always empty in practice, for the same reason CONSOLE's is above:
# create-admin-service.sh runs as its own `bash` process and cannot export back into this one.
ADMIN_DISP="$(_disp admin "${ADMIN_SERVICE_DISPOSITION:-}" admin_service)"
# EDGE_DISPOSITION is always empty in practice, for the same reason CONSOLE's and ADMIN's are
# above: create-edge.sh runs as its own `bash` process and cannot export back into this one.
# EDGE_IP_ADDRESS used to be empty for that same reason; it is now read back from the reserved
# address above rather than inherited, so the manifest records the address on every run.
EDGE_DISP="$(_disp edge "${EDGE_DISPOSITION:-}" edge)"
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
CONSOLE_RECON="$(_recon console)"
ADMIN_RECON="$(_recon admin)"
EDGE_RECON="$(_recon edge)"

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
  # the running workloads is already answered per workload, by 'reconciled' on mesh_job and
  # api_service - saying it a second time here cost 'set(doc["images"])' its meaning, which is
  # exactly what a consumer iterating for digests relies on.
  "images": {"mesh": "${MESH_DIGEST}", "app": "${APP_DIGEST}", "console": "${CONSOLE_DIGEST}", "admin": "${ADMIN_DIGEST}"},
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
if "${CLOUDRUN_CONSOLE_SERVICE:-}":
  doc["resources"]["console_service"] = {
    "name": "${CLOUDRUN_CONSOLE_SERVICE:-}",
    "service_account": "${CONSOLE_SERVICE_ACCOUNT:-}",
    "disposition": "${CONSOLE_DISP}",
    "reconciled": ${CONSOLE_RECON},
  }
if "${CLOUDRUN_ADMIN_SERVICE:-}":
  doc["resources"]["admin_service"] = {
    "name": "${CLOUDRUN_ADMIN_SERVICE:-}",
    "service_account": "${ADMIN_SERVICE_ACCOUNT:-}",
    "disposition": "${ADMIN_DISP}",
    "reconciled": ${ADMIN_RECON},
  }
if "${CONSOLE_DOMAIN:-}" or "${ADMIN_DOMAIN:-}":
  # A deployment with no custom hostname declared has no edge - Cloud Run's own *.run.app URL
  # works without one. EDGE_IP_ADDRESS is the address read back from EDGE_IP_NAME above, so it is
  # the address that is actually reserved whether or not this run selected 'edge' - and empty only
  # when the reservation does not exist yet or could not be read, never merely because the edge
  # stage ran in a different process.
  doc["resources"]["edge"] = {
    "address": "${EDGE_IP_ADDRESS:-}",
    "hostnames": [h for h in ("${CONSOLE_DOMAIN:-}", "${ADMIN_DOMAIN:-}") if h],
    "certificates": [
        dict(zip(("hostname", "name", "state"), _line.split()))
        for _line in """${EDGE_CERT_REPORT}""".strip().splitlines() if _line.strip()
    ],
    "disposition": "${EDGE_DISP}",
    "reconciled": ${EDGE_RECON},
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
