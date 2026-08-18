#!/usr/bin/env bash
# Responsibility: Create or validate the one deployed identity - the account the mesh job runs as.
# Boundaries: compute-only and never granted a secret; a supplied account is validated, never altered.

# Create or validate the mesh job's runtime identity (idempotent).
#
# There is exactly one deployed identity: the account the Cloud Run mesh job runs as. It is
# compute-only - its sole access is the exchange bucket (see apply-iam.sh) and it is never granted
# a secret. The local caller is whatever principal the operator's machine already authenticates
# as, so this tooling does not create an identity for it.
#
# MESH_SA_DISPOSITION (from bootstrap) decides whether the account is created here or supplied.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID MESH_SERVICE_ACCOUNT
MESH_SA_DISPOSITION="${MESH_SA_DISPOSITION:-reused}"

if [ "${MESH_SA_DISPOSITION}" = "created" ]; then
  if sa_exists "${MESH_SA_EMAIL}"; then
    log "service account ${MESH_SA_EMAIL} already exists - skipping"
  else
    info "Creating mesh runtime identity ${MESH_SA_EMAIL} (compute-only)"
    gc iam service-accounts create "${MESH_SERVICE_ACCOUNT}" \
      --display-name "Hexera mesh runner (compute-only)"
  fi
else
  info "Validating the SUPPLIED mesh runtime identity (not created here)"
  if sa_exists "${MESH_SA_EMAIL}"; then
    log "mesh SA ${MESH_SA_EMAIL} found"
  else
    warn "mesh SA ${MESH_SA_EMAIL} not found - set MESH_SERVICE_ACCOUNT to the mesh job's"
    warn "actual runtime identity (find it with: gcloud run jobs describe ${CLOUDRUN_MESH_JOB} \\"
    warn "  --region ${GCP_REGION} --format='value(spec.template.spec.template.spec.serviceAccountName)')"
    die "mesh service account mismatch - refusing to guess. Set MESH_SA_DISPOSITION=created to create it."
  fi
fi
log "done"
