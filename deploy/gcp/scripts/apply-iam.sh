#!/usr/bin/env bash
# Responsibility: Grant the least privilege the mesh executor needs - invoke the job, and exchange objects.
# Boundaries: every binding is scoped to a named job or bucket; the deployer's own roles are not granted here.

# Apply least-privilege IAM for the mesh executor. Idempotent: add-iam-policy-binding is a no-op
# when the binding already exists, and every binding is scoped to a named job or bucket rather
# than granted project-wide.
#
# There are exactly two principals and three operations. The LOCAL CALLER submits mesh jobs and
# trades workspaces through the exchange bucket; the MESH JOB reads its inputs from that bucket
# and writes its outputs back. Nothing here touches a database, a cache, a model key or an
# application bucket - those are local and never leave the operator's machine.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION CLOUDRUN_MESH_JOB GCP_MESH_BUCKET MESH_SERVICE_ACCOUNT

grant_job() {   # job member role
  gc run jobs add-iam-policy-binding "$1" --region "${GCP_REGION}" \
    --member "$2" --role "$3" >/dev/null
  log "job/$1 += $3 -> $2"
}
grant_bucket() {  # bucket member role
  gc storage buckets add-iam-policy-binding "gs://$1" \
    --member "$2" --role "$3" >/dev/null
  log "gs://$1 += $3 -> $2"
}

# THE LOCAL CALLER. `MESH_INVOKER` is whatever principal the operator's machine authenticates as -
# a user account under `gcloud auth application-default login`, or a service account. It submits
# executions with argument overrides (the job id) and exchanges workspace objects.
#
# objectUser, not objectAdmin: the adapter creates, reads, lists and deletes objects and never
# manages object ACLs or per-object IAM, so the admin role would grant authority nothing calls.
MESH_INVOKER="${MESH_INVOKER:-}"
if [ -n "${MESH_INVOKER}" ]; then
  info "Local caller: ${MESH_INVOKER}"
  grant_job "${CLOUDRUN_MESH_JOB}" "${MESH_INVOKER}" "roles/run.jobsExecutorWithOverrides"
  grant_bucket "${GCP_MESH_BUCKET}" "${MESH_INVOKER}" "roles/storage.objectUser"
else
  info "MESH_INVOKER unset - skipping the local caller's bindings."
  info "Set it to the principal your machine authenticates as, e.g."
  info "  MESH_INVOKER=user:you@example.com   or   MESH_INVOKER=serviceAccount:x@proj.iam.gserviceaccount.com"
fi

# THE MESH JOB'S RUNTIME IDENTITY. Compute-only: it reads its input workspace and writes results
# back to the same bucket. No secret, no Cloud Run administration, no project-level role.
#
# The binding is on the exchange bucket, so it follows the identity that will open that bucket -
# not the flag describing how the account came to exist. Gating it on MESH_SA_DISPOSITION skipped
# the grant whenever the account was reused, and bootstrap marks it reused the moment the job
# exists, so a run that pointed an existing job at a NEW bucket produced a job that could not read
# its own input. That state is invisible: the caller's own grant on the same bucket is applied
# either way, so every operator-facing check still passed.
#
# The job is the authority on which identity it runs as. The configured account is the fallback
# for the provisioning order, where IAM is applied against a job this run is still creating.
RUNTIME_SA="$(gc run jobs describe "${CLOUDRUN_MESH_JOB}" --region "${GCP_REGION}" \
  --format='value(spec.template.spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
RUNTIME_SA="${RUNTIME_SA:-${MESH_SA_EMAIL}}"
info "Mesh runtime identity: ${RUNTIME_SA} (compute-only)"
grant_bucket "${GCP_MESH_BUCKET}" "serviceAccount:${RUNTIME_SA}" "roles/storage.objectUser"
log "done - the deployer's own prerequisites (run.admin, iam.serviceAccountUser on the mesh SA)"
log "are listed in docs/deployment/overview.md and are NOT granted by this script"
