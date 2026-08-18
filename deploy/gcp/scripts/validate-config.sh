#!/usr/bin/env bash
# Responsibility: Validate the generated configuration as one typed schema before any cloud mutation.
# Boundaries: read-only and offline, so a misconfiguration fails here rather than half-way through provisioning.

# Validate generated.env as ONE typed contract before any cloud mutation. This is the single
# authoritative schema check - read-only, no gcloud calls - so a misconfiguration fails here with
# one precise instruction rather than half-way through provisioning.
#
# It validates the mesh executor and nothing else. The API, the pipeline and every data store run
# locally and are configured through the application's own .env, not through this file.
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

if [ ${#errs[@]} -gt 0 ]; then
  warn "configuration is invalid:"
  for e in "${errs[@]}"; do printf '    - %s\n' "${e}" >&2; done
  die "fix generated.env and rerun"
fi
info "configuration valid (mesh job: ${MESH_JOB_DISPOSITION}, bucket: ${MESH_BUCKET_DISPOSITION})"
