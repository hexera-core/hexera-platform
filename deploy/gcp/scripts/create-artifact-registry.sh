#!/usr/bin/env bash
# Responsibility: Create the regional Docker repository the mesh image is pushed to.
# Boundaries: idempotent - an existing repository is left as it is, and no image is pushed here.

# Create the regional Docker Artifact Registry repo the images live in (idempotent).
# Must run before images are pushed and before preflight validates them.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION ARTIFACT_REGISTRY_REPOSITORY

if ar_repo_exists; then
  log "Artifact Registry repo ${ARTIFACT_REGISTRY_REPOSITORY} already exists in ${GCP_REGION}"
else
  info "Creating Artifact Registry docker repo ${ARTIFACT_REGISTRY_REPOSITORY} (${GCP_REGION})"
  gc artifacts repositories create "${ARTIFACT_REGISTRY_REPOSITORY}" \
    --repository-format=docker --location "${GCP_REGION}" \
    --description "mesh application images"
fi
# A repository nobody can push to is not provisioned. Docker learns the credential helper for
# this host here, next to the creation, because the alternative is the publish step failing later
# with a message about tag reads that names neither Docker nor this command.
REGISTRY_HOST="${GCP_REGION}-docker.pkg.dev"
if gcloud auth configure-docker "${REGISTRY_HOST}" --quiet >/dev/null 2>&1; then
  log "done - docker configured for ${REGISTRY_HOST}"
else
  log "done - repository created, but docker could not be configured for ${REGISTRY_HOST}."
  log "      run it yourself: gcloud auth configure-docker ${REGISTRY_HOST}"
fi
