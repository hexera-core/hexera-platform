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
# ATTEMPT, then VERIFY - rather than assume the attempt was permitted. Enabling a service needs
# serviceusage.services.enable, which the federated deploy identity deliberately does not hold:
# its four roles are run.admin, artifactregistry.writer, iam.serviceAccountUser and
# compute.instanceAdmin, and widening them to cover this one call would hand a CI identity the
# ability to turn on any Google service in the project. So a refusal here is EXPECTED under
# automation and is not, by itself, a failure.
#
# What matters is the end state, not who reached it. If every API is already on - the ordinary
# case, because an owner enables them once per project - the deploy proceeds. Only an API that is
# genuinely off, and that this caller could not turn on, stops the run.
if ! gc services enable "${APIS[@]}" 2>/dev/null; then
  warn "could not enable APIs as $(gcloud config get-value account 2>/dev/null) - checking whether they are already on"
fi

enabled="$(gc services list --enabled --format='value(config.name)' 2>/dev/null || true)"
missing=()
for api in "${APIS[@]}"; do
  printf '%s\n' "${enabled}" | grep -qx "${api}" || missing+=("${api}")
done
if [ "${#missing[@]}" -gt 0 ]; then
  die "these APIs are not enabled on ${GCP_PROJECT_ID} and this account could not enable them:
    $(printf '%s ' "${missing[@]}")
  Run once as a project owner:  gcloud services enable ${missing[*]} --project=${GCP_PROJECT_ID}"
fi
log "done - all ${#APIS[@]} APIs enabled"
