#!/usr/bin/env bash
# Responsibility: Record what this deployment provisioned, and which resources it owns rather than reuses.
# Boundaries: names, dispositions and digests only - it holds no secret value and mutates no resource.

# Write the machine-readable DEPLOYMENT-STATE MANIFEST (deploy/output/deployment.json) after a
# successful mesh deployment. It records what exists and - crucially - which resources this
# tooling OWNS (created) versus REUSES (supplied), so diagnostics, reruns and upgrades act on
# recorded ownership rather than on a guessed prefix or operator memory.
#
# It describes the mesh executor only. The application runs locally and has no deployed state.
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
  "images": {"mesh": "${MESH_DIGEST}"},
}
json.dump(doc, open(out, "w"), indent=2)
print(out)
PY
info "Wrote deployment-state manifest: ${OUT}"
log "resources owned=created, reused=supplied"
