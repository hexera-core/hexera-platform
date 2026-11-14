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

# THE RIGHT TO LEAVE, for the BIG-MACHINE route. A mesh too large for the Cloud Run job's 32 GiB
# runs on a VM of its own (adapters/mesh_execution/gce_vm), started by a worker and running as
# this same identity, and the last line of its startup script is the box deleting itself. That
# call needs compute.instances.delete on the project, which nothing has ever granted: the header
# of this file says "no project-level role" and meant it, and the VM route was added later.
#
# WHAT THAT COST, measured 2026-11-13 in hexera-dev. From the instance, a GET on its own resource
# answered 403 - "Required 'compute.instances.list' permission" - so every self-delete this route
# has ever issued was refused. Job b6762d5c's n2-highmem-64 finished its mesh and uploaded its
# 12 GB result at 22:36 UTC; its `maxRunDuration` (12.42 h, with instanceTerminationAction DELETE)
# was due to remove it at 07:58. 9.4 hours of 64 cores and 512 GiB doing nothing, per big mesh.
# The lifetime cap is the backstop and it does work, so nothing leaks forever and no job was ever
# lost - what leaks is the money between the end of the mesh and the end of the cap.
#
# TWO PERMISSIONS, NOT roles/compute.instanceAdmin.v1, which would let every mesh VM create and
# delete any instance in the project. `.get` is here so a refusal can be told from a VM that has
# already gone, which is the only other way that call returns an error.
#
# A DEPLOY IDENTITY USUALLY CANNOT GRANT IT - creating a role needs iam.roles.create and binding
# it needs the project's setIamPolicy, which a deployer is deliberately not given - so it is
# attempted and a refusal prints the one command an owner runs ONCE. Without the grant nothing
# breaks and no mesh is lost: the VM says so on its own serial console and Compute Engine removes
# it at the cap. The printed command joins its halves with `;` and not `&&`, because after a
# half-finished earlier attempt the role exists, its create fails, and the binding must still be
# made.
MESH_SELF_DELETE_ROLE="${MESH_SELF_DELETE_ROLE:-hexeraMeshSelfDelete}"
MESH_SELF_DELETE_PERMS="compute.instances.delete,compute.instances.get"
MESH_SELF_DELETE_REF="projects/${GCP_PROJECT_ID}/roles/${MESH_SELF_DELETE_ROLE}"
MESH_SELF_DELETE_CMD="gcloud iam roles create ${MESH_SELF_DELETE_ROLE} --project ${GCP_PROJECT_ID} --title 'Hexera mesh VM self-delete' --permissions ${MESH_SELF_DELETE_PERMS} --stage GA ; gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} --member serviceAccount:${RUNTIME_SA} --role ${MESH_SELF_DELETE_REF} --condition None"
gc iam roles create "${MESH_SELF_DELETE_ROLE}" \
    --title "Hexera mesh VM self-delete" \
    --description "Lets a big-machine mesh VM remove itself when its mesh is done" \
    --permissions "${MESH_SELF_DELETE_PERMS}" --stage GA >/dev/null 2>&1 \
  || gc iam roles update "${MESH_SELF_DELETE_ROLE}" \
    --permissions "${MESH_SELF_DELETE_PERMS}" >/dev/null 2>&1 \
  || true
if gc projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
     --member "serviceAccount:${RUNTIME_SA}" \
     --role "${MESH_SELF_DELETE_REF}" --condition None >/dev/null 2>&1; then
  log "project += ${MESH_SELF_DELETE_REF} -> ${RUNTIME_SA}"
else
  warn "could not grant ${RUNTIME_SA} the right to delete its own big-machine mesh VM
       (${MESH_SELF_DELETE_PERMS}). This deploy identity cannot change project IAM. If an owner
       has already granted it, nothing more is needed. If not, every VM this route starts runs on
       after its mesh is finished until its lifetime cap expires - measured at 9.4 hours of an
       n2-highmem-64 on one run - and says so on its serial console. An owner runs, ONCE:
         ${MESH_SELF_DELETE_CMD}"
fi
log "done - the deployer's own prerequisites (run.admin, iam.serviceAccountUser on the mesh SA)"
log "are listed in docs/deployment/overview.md and are NOT granted by this script"
