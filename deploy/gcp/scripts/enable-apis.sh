#!/usr/bin/env bash
# Responsibility: Enable the Google APIs the mesh tier needs.
# Boundaries: idempotent - enabling an already-enabled service is a no-op, and nothing else is granted.

# Enable the Google APIs the deployment needs. Idempotent: `services enable` is a no-op when
# already enabled.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
# Enabling needs ONE fact - which project - and discovery needs the APIs to be on before it can
# ask Cloud Run and Cloud Storage what exists. Requiring discovery's output here would make the
# two depend on each other and neither could run first on a blank project. So the project comes
# from the generated config when discovery has already run, and otherwise from the operator's own
# .env or active gcloud configuration.
if [ -f "${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}" ]; then
  load_env
else
  set -a; [ -f "${REPO_ROOT}/.env" ] && . "${REPO_ROOT}/.env"; set +a
  GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
  export GCP_PROJECT_ID
fi
require_vars GCP_PROJECT_ID

APIS=(
  run.googleapis.com
  artifactregistry.googleapis.com
  secretmanager.googleapis.com
  storage.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com          # SignBlob for v4 signed URLs without a key
  cloudresourcemanager.googleapis.com
)

info "Enabling ${#APIS[@]} APIs on ${GCP_PROJECT_ID} (idempotent)"
gc services enable "${APIS[@]}"
log "done"
