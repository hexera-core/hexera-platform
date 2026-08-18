#!/usr/bin/env bash
# Responsibility: Provision the mesh execution tier - the exchange bucket and the digest-pinned mesh job.
# Boundaries: the image comes from the validated release record, never built here; a supplied tier is validated.

# Provision the MESH EXECUTION TIER - the exchange bucket, the mesh image, and the mesh Cloud Run
# job. This is the piece a fresh organisation used to have to build by hand.
#
#   EACH RESOURCE IS DECIDED ON ITS OWN, the way discovery decided it: a project that already owns
#   a mesh job may still need its exchange bucket created, so neither disposition speaks for the
#   other. `reused` means validated and left untouched - never replaced, never reconfigured.
#
# Idempotent: an existing exchange bucket / mesh image / mesh job is reconciled, not duplicated.
# The mesh job holds NO database/Redis/model secrets (compute-only).
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION ARTIFACT_REGISTRY_REPOSITORY \
  CLOUDRUN_MESH_JOB MESH_SERVICE_ACCOUNT GCP_MESH_BUCKET \
  MESH_CPU MESH_MEMORY MESH_TIMEOUT_SECONDS DEPLOYMENT_ID
MESH_JOB_DISPOSITION="${MESH_JOB_DISPOSITION:-reused}"
MESH_BUCKET_DISPOSITION="${MESH_BUCKET_DISPOSITION:-reused}"

info "provisioning the mesh tier"

# 1) exchange bucket - hardened + lifecycle, DISTINCT from the data bucket. A bucket the operator
#    supplied is validated and left exactly as it is; only one this deployment creates is hardened
#    and labelled, because reconfiguring somebody else's bucket is not ours to do.
if [ "${MESH_BUCKET_DISPOSITION}" = "reused" ]; then
  bucket_exists "${GCP_MESH_BUCKET}" \
    || die "supplied exchange bucket gs://${GCP_MESH_BUCKET} not found. Create it or set GCP_MESH_BUCKET"
  log "exchange bucket gs://${GCP_MESH_BUCKET}  (reused - untouched)"
else
  if bucket_exists "${GCP_MESH_BUCKET}"; then
    log "exchange bucket gs://${GCP_MESH_BUCKET} already exists - reconciling settings"
  else
    info "Creating exchange bucket gs://${GCP_MESH_BUCKET} in ${GCP_REGION}"
    # Versioning is not a create-time flag - a new bucket has it off, and the update below
    # asserts that explicitly. Passing it to create is rejected by gcloud.
    gc storage buckets create "gs://${GCP_MESH_BUCKET}" \
      --location "${GCP_REGION}" \
      --uniform-bucket-level-access \
      --public-access-prevention
  fi
  gc storage buckets update "gs://${GCP_MESH_BUCKET}" \
    --uniform-bucket-level-access --public-access-prevention --no-versioning
# Transient exchange objects: an input bundle and its result, deleted a day or two after the mesh
# that used them. The rule document carries no commentary of its own - gcloud rejects a lifecycle
# file with any key it does not recognise, so the explanation lives here instead.
  LIFECYCLE="${DEPLOY_DIR}/storage/exchange-bucket-lifecycle.json"
  [ -f "${LIFECYCLE}" ] && gc storage buckets update "gs://${GCP_MESH_BUCKET}" --lifecycle-file "${LIFECYCLE}"
  gc storage buckets update "gs://${GCP_MESH_BUCKET}" \
    --update-labels "app=hexera,version=0-0-1,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" >/dev/null 2>&1 || true
  log "exchange bucket gs://${GCP_MESH_BUCKET}  (created)"
fi

# 2) mesh image - taken from the VALIDATED release record, never built here.
#
# This used to submit a Cloud Build and then resolve the resulting tag to a digest, which meant
# the mesh image that ran off-box was built from source at deploy time and had never been through
# the native tiers. scripts/promote-release.sh writes MESH_IMAGE into the deployment env as the
# digest of the exact image Gate C validated; this step only checks that it did.
# Only a job this run applies consumes the image, so a deployment reusing a supplied job is not
# asked to have one.

# 3) mesh job - create/replace from the manifest, pinned to the digest, compute-only.
if [ "${MESH_JOB_DISPOSITION}" = "reused" ]; then
  run_job_exists "${CLOUDRUN_MESH_JOB}" \
    || die "supplied mesh job ${CLOUDRUN_MESH_JOB} not found in ${GCP_REGION}. Set CLOUDRUN_MESH_JOB"
  log "mesh job        ${CLOUDRUN_MESH_JOB}      (reused - untouched)"
else
  require_digest_reference MESH_IMAGE "${MESH_IMAGE:-}"
  log "mesh image (validated digest): ${MESH_IMAGE}"
  export MESH_SA_EMAIL MESH_IMAGE
  rendered="$(render_manifest "${DEPLOY_DIR}/cloud-run/mesh-job.yaml")"
  info "Applying mesh job ${CLOUDRUN_MESH_JOB} (gcloud run jobs replace, digest-pinned)"
  gc run jobs replace "${rendered}" --region "${GCP_REGION}"
  log "mesh job        ${CLOUDRUN_MESH_JOB}  (created - SA ${MESH_SA_EMAIL}, maxRetries=0, task=1)"
fi

[ "${MESH_JOB_DISPOSITION}" = "reused" ] || log "mesh image      ${MESH_IMAGE}"
log "done"
