#!/usr/bin/env bash
# Responsibility: Generate the deployment's configuration by discovering the live project rather than by hand.
# Boundaries: a name match is a hint, not a licence to guess - zero or several candidates stop with one fix.

# Generate deploy/gcp/generated.env by DISCOVERING the environment - no hand editing.
#
# Everything below is read from the authenticated gcloud session and the live project - the
# project number, the mesh job's runtime service account, the exchange bucket and the image
# URIs. The only inputs a human supplies are the five external credentials, which are
# configured in the application's own .env and never appear here.
#
# Idempotent: an existing `env` is REUSED, and only genuinely missing keys are filled in, so a
# value you deliberately changed (a region, a bucket, CORS_ORIGINS resolved by a previous deploy)
# survives a rerun. Pass --force to regenerate from scratch.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

ENV_FILE="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
FORCE=0
if [ "${1:-}" = "--force" ]; then FORCE=1; fi

# DEPLOYMENT_ID: the stable, generic ownership prefix for every resource this deployment creates.
# Never derived from the original project; defaults to the target project id (portable + unique).

# _require_exactly_one <what> <explicit-var> <newline-separated-candidates>
# Deterministic selection. A name match is a HINT, never a licence to guess: zero candidates or
# more than one both STOP the deploy with one instruction (set <explicit-var>), instead of the
# old `... | head -1` that silently accepted the first of several. On exactly one, the choice is
# left in the global _PICKED. Called as a normal statement so its `die` aborts cleanly under set -e.
_require_exactly_one() {
  local what="$1" var="$2" cands n
  cands="$(printf '%s\n' "$3" | sed '/^[[:space:]]*$/d')"
  n="$(printf '%s' "${cands}" | grep -c . || true)"
  if [ "${n}" -eq 0 ]; then
    die "no ${what} found. This deployment REUSES it and never creates it - provision it first, or set ${var} in ${ENV_FILE} to its exact name."
  elif [ "${n}" -gt 1 ]; then
    die "$(printf 'multiple candidates for the %s - refusing to guess. Set %s in %s to exactly one of:\n' "${what}" "${var}" "${ENV_FILE}")
$(printf '%s\n' "${cands}" | sed 's/^/    /')"
  fi
  _PICKED="${cands}"
}

# the discovered facts
discover() {
  PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
  [ -n "${PROJECT_ID}" ] && [ "${PROJECT_ID}" != "(unset)" ] \
    || die "no GCP project set - run: gcloud config set project <PROJECT_ID>"

  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null)" \
    || die "cannot read project ${PROJECT_ID} - is the account authorised for it?"

  # Region: explicit env > gcloud run/config default > the region the mesh job actually lives in.
  REGION="${GCP_REGION:-$(gcloud config get-value run/region 2>/dev/null)}"
  if [ "${REGION}" = "(unset)" ]; then REGION=""; fi
  if [ -z "${REGION}" ]; then
    REGION="$(gcloud run jobs list --format='value(metadata.labels."cloud.googleapis.com/location")' \
                --limit 1 2>/dev/null | head -1)"
  fi
  REGION="${REGION:-us-central1}"

  # THE OPERATOR'S DECLARED NAMES. .env is where a person writes which job and bucket they want,
  # and the application reads those same values at run time, so provisioning must use them
  # verbatim rather than derive its own - otherwise it creates resources the application will
  # never look for. Values already exported win, so a caller can still override.
  if [ -f "${REPO_ROOT}/.env" ]; then
    while IFS='=' read -r _k _v; do
      case "${_k}" in
        GCP_PROJECT_ID|GCP_REGION|CLOUDRUN_JOB|GCP_MESH_BUCKET|DEPLOYMENT_ID)
          [ -n "${_v}" ] && [ -z "$(eval printf '%s' "\${${_k}:-}")" ] && export "${_k}=${_v}" ;;
      esac
    done < <(grep -E '^[A-Z_]+=' "${REPO_ROOT}/.env" 2>/dev/null || true)
  fi
  # The job the operator declared, under the name they declared it.
  CLOUDRUN_MESH_JOB="${CLOUDRUN_MESH_JOB:-${CLOUDRUN_JOB:-}}"

  # DEPLOYMENT_ID: generic ownership prefix; default = the target project id (unique + portable).
  DEPLOY_ID="${DEPLOYMENT_ID:-${PROJECT_ID}}"

  # Each mesh resource is decided on its own: if the named resource exists it is REUSED and
  # validated; if it does not, this tooling creates it. There is no mode to choose - a project
  # that already owns a mesh job can still have its exchange bucket created here.
  MESH_JOB="${CLOUDRUN_MESH_JOB:-${DEPLOY_ID}-mesh}"
  MESH_SA="${MESH_SERVICE_ACCOUNT:-${DEPLOY_ID}-mesh}"
  # the exchange bucket must be GLOBALLY unique - suffix with the project number (stable per project)
  MESH_BUCKET="${GCP_MESH_BUCKET:-${DEPLOY_ID}-exchange-${PROJECT_NUMBER}}"

  if gcloud run jobs describe "${MESH_JOB}" --region "${REGION}" >/dev/null 2>&1; then
    MESH_JOB_DISPOSITION=reused
    # Read the job's REAL runtime identity off the job itself; a job that declares none genuinely
    # runs as the default compute identity.
    _SA_FULL="$(gcloud run jobs describe "${MESH_JOB}" --region "${REGION}" \
      --format='value(spec.template.spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
    if [ -n "${_SA_FULL}" ]; then MESH_SA="${_SA_FULL%%@*}"; else MESH_SA="${PROJECT_NUMBER}-compute"; fi
    MESH_SA_DISPOSITION=reused
    info "mesh job ${MESH_JOB} exists in ${REGION} - reusing it (identity ${MESH_SA})"
  else
    MESH_JOB_DISPOSITION=created
    MESH_SA_DISPOSITION=created
    info "mesh job ${MESH_JOB} not found in ${REGION} - it will be created"
  fi

  if gcloud storage buckets describe "gs://${MESH_BUCKET}" >/dev/null 2>&1; then
    MESH_BUCKET_DISPOSITION=reused
    info "exchange bucket gs://${MESH_BUCKET} exists - reusing it"
  else
    MESH_BUCKET_DISPOSITION=created
    info "exchange bucket gs://${MESH_BUCKET} not found - it will be created"
  fi

  return 0
}

emit_env() {
  cat <<ENVFILE
# deploy/gcp/generated.env - GENERATED deployment state (scripts/bootstrap-env.sh).
# DO NOT HAND-EDIT: rerunning discovery preserves existing values, and deploy stages write the
# image/CORS results back here. To override a discovered value deliberately, edit and rerun -
# but this file is internal state, not a template.
# NO SECRET VALUES: only Secret Manager CONTAINER names appear below. This file is .gitignored.
#
# BOUND TO project ${PROJECT_ID} / region ${REGION}. If the active gcloud project or region
# changes, bootstrap-env REJECTS this file rather than deploy elsewhere - regenerate with --force.
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ).

# ownership
DEPLOYMENT_ID=${DEPLOY_ID}
# per-resource disposition - 'created' (this deployment owns it) or 'reused' (supplied, untouched).
# A resource marked 'reused' is validated and never modified by this tooling.
MESH_JOB_DISPOSITION=${MESH_JOB_DISPOSITION}
MESH_SA_DISPOSITION=${MESH_SA_DISPOSITION}
MESH_BUCKET_DISPOSITION=${MESH_BUCKET_DISPOSITION}

# discovered GCP environment
GCP_PROJECT_ID=${PROJECT_ID}
GCP_PROJECT_NUMBER=${PROJECT_NUMBER}
GCP_REGION=${REGION}

# Artifact Registry. The IMAGE references are deliberately left EMPTY here.
ARTIFACT_REGISTRY_REPOSITORY=${AR_REPO}
# The Cloud Run MESH JOB. The local application reads it as CLOUDRUN_JOB.
CLOUDRUN_MESH_JOB=${MESH_JOB}
# The mesh IMAGE, written by scripts/promote-release.sh as the validated digest; empty until then,
# because empty stops the deploy with one instruction rather than promoting an unvalidated image.
MESH_IMAGE=${MESH_IMAGE:-}

# the GCS exchange bucket the local pipeline and the mesh job trade workspaces through
GCP_MESH_BUCKET=${MESH_BUCKET}

# the mesh runtime identity (ID; email derived <id>@<project>.iam.gserviceaccount.com)
MESH_SERVICE_ACCOUNT=${MESH_SA}

# mesh job sizing
MESH_CPU=4
MESH_MEMORY=8Gi
MESH_TIMEOUT_SECONDS=14400
ENVFILE
}

# Defaults for the names we own (kept stable so reruns reconcile rather than duplicate).
AR_REPO="${ARTIFACT_REGISTRY_REPOSITORY:-mesh}"

if [ -f "${ENV_FILE}" ] && [ "${FORCE}" -eq 0 ]; then
  # Reuse what is already configured: load it, then regenerate so any NEW key introduced by a
  # later version of this script appears, without discarding existing choices.
  info "Reusing existing ${ENV_FILE} (pass --force to regenerate)"
  # Capture what the OPERATOR explicitly requested BEFORE sourcing the file overwrites it.
  _REQ_REGION="${GCP_REGION:-}"
  set -a
  # shellcheck disable=SC1090  # path is chosen at runtime (DEPLOY_ENV_FILE)
  . "${ENV_FILE}"
  set +a

 # STALE-STATE GUARD
  # generated.env is BOUND to the project (and region) that produced it. If the active gcloud
  # project - or an explicitly requested region - has since changed, reusing this file would
  # silently deploy to the OLD target. Reject it and make the operator rediscover ON PURPOSE,
  # rather than deploy somewhere they did not intend. `gcloud config get-value` is read from the
  # live session, INDEPENDENT of the sourced file, so it is a true "what is active now" signal.
  _ambient_project="$(gcloud config get-value project 2>/dev/null || true)"
  if [ -n "${_ambient_project}" ] && [ "${_ambient_project}" != "(unset)" ] \
     && [ -n "${GCP_PROJECT_ID:-}" ] && [ "${_ambient_project}" != "${GCP_PROJECT_ID}" ]; then
    die "${ENV_FILE} is bound to project '${GCP_PROJECT_ID}', but gcloud is now on '${_ambient_project}'.
   Refusing to deploy to a different project than the generated state was built for. Either:
     - switch back:      gcloud config set project ${GCP_PROJECT_ID}
     - or regenerate:    bash $(basename "${BASH_SOURCE[0]}") --force   (rediscovers for ${_ambient_project})"
  fi
  # Region: only an EXPLICIT request - the operator's own GCP_REGION (captured above) or a set
  # gcloud run/region - is a change signal; a discovered region has no ambient echo to compare to.
  _ambient_region="${_REQ_REGION:-$(gcloud config get-value run/region 2>/dev/null || true)}"
  if [ -n "${_ambient_region}" ] && [ "${_ambient_region}" != "(unset)" ] \
     && [ -n "${GCP_REGION:-}" ] && [ "${_ambient_region}" != "${GCP_REGION}" ]; then
    die "${ENV_FILE} is bound to region '${GCP_REGION}', but region '${_ambient_region}' was requested.
   Refusing to deploy to a different region. Regenerate for it: bash $(basename "${BASH_SOURCE[0]}") --force"
  fi

  AR_REPO="${ARTIFACT_REGISTRY_REPOSITORY:-${AR_REPO}}"
  discover
  # keep already-discovered/overridden values
  PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID}}"
  PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-${PROJECT_NUMBER}}"
  REGION="${GCP_REGION:-${REGION}}"
  MESH_JOB="${CLOUDRUN_MESH_JOB:-${MESH_JOB}}"
  MESH_BUCKET="${GCP_MESH_BUCKET:-${MESH_BUCKET}}"
  MESH_SA="${MESH_SERVICE_ACCOUNT:-${MESH_SA}}"
else
  info "Discovering the deployment environment from gcloud"
  discover
fi

emit_env > "${ENV_FILE}"
info "Wrote ${ENV_FILE}"
log "deployment id  ${DEPLOY_ID}"
log "project        ${PROJECT_ID} (#${PROJECT_NUMBER})"
log "region         ${REGION}"
log "mesh job       ${MESH_JOB}  (${MESH_JOB_DISPOSITION})"
log "mesh SA        ${MESH_SA}   (${MESH_SA_DISPOSITION})"
log "mesh bucket    gs://${MESH_BUCKET}  (${MESH_BUCKET_DISPOSITION})"
