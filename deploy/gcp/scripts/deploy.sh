#!/usr/bin/env bash
# Responsibility: Run every mesh provisioning stage in order, behind one confirmation of the plan.
# Owns: the single confirmation gate - the sub-scripts never prompt, so a mutation is agreed to exactly once.
# Boundaries: it provisions the mesh executor only; the application and its data stores run on the operator's machine.

# THE entry point for the mesh executor. One idempotent command that provisions or updates the
# Cloud Run mesh job the local pipeline submits to, and the exchange bucket they trade workspaces
# through.
#
#     make mesh-deploy     (from the repository root)
#
# It deploys nothing else. The API, the pipeline, PostgreSQL, Redis, MinIO and SearXNG run on the
# operator's machine and are configured through the application's own .env.
#
# Every stage is idempotent and safe to rerun after a partial failure: existing resources are
# reconciled rather than duplicated, and a supplied mesh job or bucket is validated and reused,
# never recreated or deleted. Project number, region, image URI, bucket name and the service
# account email are discovered or derived; if configuration is missing, this stops with exactly
# one instruction.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

S="$(cd "$(dirname "$0")" && pwd)"
STAGE=0
stage() { STAGE=$((STAGE + 1)); printf '\n\033[1m━━━ [%d/%d] %s ━━━\033[0m\n' "${STAGE}" 12 "$1"; }

# confirmation policy
# INTERACTIVE BY DEFAULT. A cloud-mutating deploy requires a deliberate go-ahead: after the
# read-only plan is shown, the operator types the exact project ID. Automation opts out ONLY with
# DEPLOY_NONINTERACTIVE=1, and then MUST pin every ambiguous target explicitly - ambient gcloud
# config alone is never enough to mutate a cloud project unattended.
NONINTERACTIVE="${DEPLOY_NONINTERACTIVE:-0}"
if [ "${NONINTERACTIVE}" = "1" ]; then
  _missing=()
  for _v in GCP_PROJECT_ID GCP_REGION CLOUDRUN_MESH_JOB GCP_MESH_BUCKET; do
    [ -n "${!_v:-}" ] || _missing+=("${_v}")
  done
  [ ${#_missing[@]} -eq 0 ] || die \
"DEPLOY_NONINTERACTIVE=1 requires every deployment target to be explicit - set: ${_missing[*]}.
   Ambient gcloud configuration alone is not enough to mutate a cloud project unattended."
fi
# Sub-scripts never prompt on their own; THIS driver owns the single confirmation gate (below).
export ASSUME_YES=1

# confirm_plan - show WHO / WHERE / WHAT before the first cloud mutation, then require the operator
# to type the exact project ID (interactive), or accept the explicitly-pinned targets (DEPLOY_NONINTERACTIVE).
confirm_plan() {
  load_env
  local account impersonation
  account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
  impersonation="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
  [ -n "${impersonation}" ] && [ "${impersonation}" != "(unset)" ] || impersonation="(none)"
  cat <<PLAN

  This will MUTATE Google Cloud. Review the plan before anything is created:

    Human account         ${account:-<none>}
    Impersonating         ${impersonation}
    Deployment ID         ${DEPLOYMENT_ID}
    Project               ${GCP_PROJECT_ID}  (#${GCP_PROJECT_NUMBER})
    Region                ${GCP_REGION}
    Mesh job              ${CLOUDRUN_MESH_JOB}          (${MESH_JOB_DISPOSITION})
    Mesh runtime identity ${MESH_SA_EMAIL}              (${MESH_SA_DISPOSITION})
    Mesh exchange bucket  gs://${GCP_MESH_BUCKET}       (${MESH_BUCKET_DISPOSITION})
    Artifact Registry     ${ARTIFACT_REGISTRY_REPOSITORY}
    Public access         NONE - the mesh job is private and invoked by your local pipeline
PLAN
  if [ "${NONINTERACTIVE}" = "1" ]; then
    log "DEPLOY_NONINTERACTIVE=1 - every target was pinned explicitly; proceeding"
    return 0
  fi
  printf '\n  Type the project ID (%s) to deploy, or anything else to abort: ' "${GCP_PROJECT_ID}"
  local reply; read -r reply
  [ "${reply}" = "${GCP_PROJECT_ID}" ] || die "aborted - the typed value did not match the project ID"
  log "confirmed - proceeding to provision the mesh executor"
}

command -v gcloud >/dev/null 2>&1 || die \
  "gcloud not found. In Google Cloud Shell it is preinstalled; elsewhere install the Google Cloud
   CLI (https://cloud.google.com/sdk/docs/install), then: gcloud auth login && gcloud config set project <PROJECT_ID>"
command -v envsubst >/dev/null 2>&1 || die \
  "envsubst not found (package: gettext). Cloud Shell has it; on Debian/Ubuntu: sudo apt-get install -y gettext"
command -v python3 >/dev/null 2>&1 || die "python3 not found (needed to render config and parse results)"

stage "Discover the environment and generate config"
bash "${S}/bootstrap-env.sh"

stage "Validate the configuration schema (typed, read-only - one precise error if wrong)"
bash "${S}/validate-config.sh"

stage "Preflight (read-only: auth, project, permissions, region, existing mesh resources)"
bash "${S}/preflight.sh"

stage "Confirm the deployment plan (last read-only step before any cloud mutation)"
confirm_plan

stage "Enable required Google Cloud APIs"
bash "${S}/enable-apis.sh"

stage "Artifact Registry + the mesh runtime identity"
bash "${S}/create-artifact-registry.sh"
bash "${S}/create-service-accounts.sh"

stage "Promote the validated release artifact (no build - see docs/development/gates.md)"
# Deployment does NOT build. It promotes the exact images Gate C validated and release-publish
# pushed, identified by immutable registry digests read from deploy/output/release.json. The
# previous step here submitted a Cloud Build, so the bytes that shipped were built in the cloud
# from source and were never the bytes that were validated.
bash "${S}/promote-release.sh"

stage "Mesh tier (exchange bucket + mesh job; the mesh IMAGE is promoted, never built here)"
# The mesh job MUST exist before IAM - the local caller is granted invoke ON it, and the mesh
# identity is granted exchange access - so this runs AFTER the identity, BEFORE IAM.
bash "${S}/create-mesh-tier.sh"

stage "IAM (least privilege: invoke the mesh job, exchange objects)"
bash "${S}/apply-iam.sh"

# Record the machine-readable deployment-state manifest (ownership + digests; no secret values).
bash "${S}/write-deployment-state.sh" || warn "deployment-state manifest could not be written (non-fatal)"

# final summary
load_env
MESH_DIGEST="$(resolve_digest "${MESH_IMAGE:-}" 2>/dev/null || printf '%s' "${MESH_IMAGE:-}")"

printf '\n\033[1m━━━ mesh executor ready ━━━\033[0m\n'
cat <<SUMMARY

  Deployment id     ${DEPLOYMENT_ID}
  Project / region  ${GCP_PROJECT_ID} / ${GCP_REGION}
  Mesh job          ${CLOUDRUN_MESH_JOB}            [private, ${MESH_JOB_DISPOSITION}]
  Mesh identity     ${MESH_SA_EMAIL}                (${MESH_SA_DISPOSITION})
  Mesh image        ${MESH_DIGEST}
  Mesh exchange     gs://${GCP_MESH_BUCKET}         (${MESH_BUCKET_DISPOSITION})
  Deployment state  deploy/output/deployment.json   (ownership + digests; no secret values)

  Point the local application at it: set GCP_PROJECT_ID, GCP_REGION,
  CLOUDRUN_JOB=${CLOUDRUN_MESH_JOB} and GCP_MESH_BUCKET=${GCP_MESH_BUCKET} in .env,
  then run: make dev-up

  Rerun this at any time - it reconciles rather than recreates.

SUMMARY
