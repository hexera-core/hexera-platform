#!/usr/bin/env bash
# Responsibility: Check what must already be true - session, project, permissions, region, reused resources.
# Boundaries: read-only; a resource this deployment creates is reported as pending, never as a failure.

# READ-ONLY preflight. Modifies NOTHING.
#
# It validates only what must ALREADY be true: an authenticated session, a reachable project, the
# IAM permissions the deploy actually needs, the region, and the pre-existing mesh job/bucket this
# deployment reuses. Resources the deployment CREATES (Artifact Registry, secrets, data bucket,
# images) are reported as pending, never as failures - the previous version hard-failed on them,
# which made preflight impossible to pass on a fresh project and left `make preflight` a dead gate.
#
#   ASSUME_YES=1     skip the interactive confirm (the orchestrator sets this)
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION \
  ARTIFACT_REGISTRY_REPOSITORY \
  CLOUDRUN_MESH_JOB GCP_MESH_BUCKET

FAIL=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
pend() { printf '  \033[33m..\033[0m   %s\n' "$*"; }

info "Preflight for the hosted deployment on ${GCP_PROJECT_ID}/${GCP_REGION} (read-only)"

# session + project (hard preconditions)
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
if [ -n "${ACCOUNT}" ]; then ok "gcloud authenticated as ${ACCOUNT}"
else bad "gcloud not authenticated (run: gcloud auth login)"; fi

if gc projects describe "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  ok "project ${GCP_PROJECT_ID} reachable"
else
  bad "project ${GCP_PROJECT_ID} not found/accessible"
fi

billing="$(gcloud billing projects describe "${GCP_PROJECT_ID}" \
  --format='value(billingEnabled)' 2>/dev/null || true)"
case "${billing}" in
  True|true)   ok "billing enabled" ;;
  False|false) bad "billing NOT enabled on ${GCP_PROJECT_ID} - Cloud Run and Cloud Build need it" ;;
  *)           pend "billing state not readable (Cloud Billing API off) - verify in the console" ;;
esac

# IAM permissions the deploy genuinely needs (hard precondition)
# Checked explicitly because the most expensive way to find a missing permission is three stages
# into a deploy. run.services.setIamPolicy matters most: roles/editor does NOT include it, and
# without it the service deploys but can never be made public - the one thing this deployment is for.
PERM_LIST='"run.services.create","run.services.update","run.services.setIamPolicy","run.jobs.create","run.jobs.run","artifactregistry.repositories.create","secretmanager.secrets.create","secretmanager.versions.add","iam.serviceAccounts.create","iam.serviceAccounts.actAs","resourcemanager.projects.setIamPolicy","storage.buckets.create","serviceusage.services.enable"'
if TOKEN="$(gcloud auth print-access-token 2>/dev/null)" && command -v curl >/dev/null 2>&1; then
  GRANTED="$(curl -sS -X POST \
    "https://cloudresourcemanager.googleapis.com/v1/projects/${GCP_PROJECT_ID}:testIamPermissions" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d "{\"permissions\":[${PERM_LIST}]}" 2>/dev/null || true)"
  for perm in run.services.setIamPolicy run.services.create run.jobs.create \
              artifactregistry.repositories.create secretmanager.secrets.create \
              storage.buckets.create serviceusage.services.enable \
              resourcemanager.projects.setIamPolicy iam.serviceAccounts.create; do
    if printf '%s' "${GRANTED}" | grep -q "\"${perm}\""; then
      ok "permission: ${perm}"
    elif [ "${perm}" = "run.services.setIamPolicy" ]; then
      bad "permission MISSING: ${perm} - without it the API service cannot be made public. Grant roles/run.admin to ${ACCOUNT} (roles/editor does NOT include setIamPolicy)."
    elif [ "${perm}" = "resourcemanager.projects.setIamPolicy" ]; then
      bad "permission MISSING: ${perm} - IAM between the API/pipeline/mesh identities cannot be applied. Grant roles/resourcemanager.projectIamAdmin to ${ACCOUNT}."
    else
      bad "permission MISSING: ${perm}"
    fi
  done
else
  pend "cannot test IAM permissions (no token or curl) - the account needs run.admin, projectIamAdmin, artifactregistry.admin, secretmanager.admin, storage.admin"
fi

# APIs (enabled by the deployment itself; informational here)
for api in run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
           storage.googleapis.com iam.googleapis.com iamcredentials.googleapis.com; do
  if gc services list --enabled --filter="config.name=${api}" --format='value(config.name)' \
        2>/dev/null | grep -q "${api}"; then
    ok "API enabled: ${api}"
  else
    pend "API not enabled yet: ${api} (the deploy enables it)"
  fi
done

# The mesh resources, judged against what discovery decided THIS run will do with them. Absence
# is only a fault for a resource being REUSED: for one this run creates, absence is the premise,
# and the steps after this one exist to make it so.
if run_job_exists "${CLOUDRUN_MESH_JOB}"; then
  ok "existing mesh job ${CLOUDRUN_MESH_JOB} found in ${GCP_REGION} (reused, never recreated)"
elif [ "${MESH_JOB_DISPOSITION:-reused}" = "created" ]; then
  pend "mesh job ${CLOUDRUN_MESH_JOB} will be created in ${GCP_REGION}"
else
  bad "mesh job ${CLOUDRUN_MESH_JOB} not found in ${GCP_REGION} - wrong region or name?"
fi

if bucket_exists "${GCP_MESH_BUCKET}"; then
  mregion="$(gcloud storage buckets describe "gs://${GCP_MESH_BUCKET}" \
    --format='value(location)' 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  ok "existing mesh exchange bucket gs://${GCP_MESH_BUCKET} (location ${mregion}) - reused"
elif [ "${MESH_BUCKET_DISPOSITION:-reused}" = "created" ]; then
  pend "mesh exchange bucket gs://${GCP_MESH_BUCKET} will be created"
else
  bad "mesh exchange bucket gs://${GCP_MESH_BUCKET} not found"
fi

# resources the deployment CREATES (pending, never fatal)
if ar_repo_exists; then ok "Artifact Registry repo ${ARTIFACT_REGISTRY_REPOSITORY} exists"
else pend "Artifact Registry repo ${ARTIFACT_REGISTRY_REPOSITORY} will be created"; fi

if [ -n "${MESH_IMAGE:-}" ] && resolve_digest "${MESH_IMAGE}" >/dev/null 2>&1; then
  ok "mesh image already published: ${MESH_IMAGE}"
else
  pend "mesh image will be built and pushed from this repo"
fi

echo
info "Deployment summary"
cat <<SUMMARY
  Project:                ${GCP_PROJECT_ID}
  Region:                 ${GCP_REGION}
  Mesh job:               ${CLOUDRUN_MESH_JOB}      (private; the only deployed workload)
  Mesh exchange bucket:   gs://${GCP_MESH_BUCKET}
  Mesh runtime identity:  ${MESH_SA_EMAIL}
  The API, the pipeline and every data store run locally and are not deployed.
SUMMARY

[ "${FAIL}" -eq 0 ] || die "preflight found blocking problems - fix them before deploying"
confirm "Preflight passed. Proceed to provision the mesh executor?"
log "preflight OK"
