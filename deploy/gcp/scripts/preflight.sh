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
  # WHICH permissions this run actually needs. A permission to CREATE something is required only
  # when this run will create it, and discovery has already decided that: bootstrap-env.sh records
  # a disposition of `created` or `reused` per resource. Asserting the create permissions
  # unconditionally demanded effective project-ownership from every caller, including an automation
  # identity whose whole point is to hold less than that - and it failed a reuse-run for a
  # permission the run would never exercise. Required-ness is derived, not listed.
  _needed() {  # _needed <permission> -> 0 if this run needs it
    case "$1" in
      # Always: the deploy updates the service and its IAM, and acts as the runtime identity.
      run.services.setIamPolicy|run.services.create|run.services.update|iam.serviceAccounts.actAs) return 0 ;;
      # enable-apis.sh runs every time and is idempotent, but only mutates when one is off.
      serviceusage.services.enable) [ -n "${_APIS_MISSING:-}" ] && return 0 || return 1 ;;
      run.jobs.create)                       [ "${MESH_JOB_DISPOSITION:-created}"    = created ] ;;
      storage.buckets.create)                [ "${MESH_BUCKET_DISPOSITION:-created}" = created ] ;;
      iam.serviceAccounts.create)            [ "${MESH_SA_DISPOSITION:-created}"     = created ] ;;
      artifactregistry.repositories.create)  [ -z "${_AR_EXISTS:-}" ] ;;
      # apply-iam.sh grants project-level bindings only when it has an invoker to bind.
      resourcemanager.projects.setIamPolicy) [ -n "${MESH_INVOKER:-}" ] ;;
      *) return 0 ;;
    esac
  }
  # Facts the derivation above reads, gathered once.
  _AR_EXISTS="$(gc artifacts repositories describe "${ARTIFACT_REGISTRY_REPOSITORY:-mesh}" \
      --location "${GCP_REGION}" --format='value(name)' 2>/dev/null || true)"
  _APIS_MISSING=""
  for _api in run.googleapis.com artifactregistry.googleapis.com storage.googleapis.com \
              iam.googleapis.com iamcredentials.googleapis.com; do
    gc services list --enabled --filter="config.name=${_api}" --format='value(config.name)' \
      2>/dev/null | grep -q "${_api}" || _APIS_MISSING="yes"
  done

  for perm in run.services.setIamPolicy run.services.create run.jobs.create \
              artifactregistry.repositories.create \
              storage.buckets.create serviceusage.services.enable \
              resourcemanager.projects.setIamPolicy iam.serviceAccounts.create; do
    if ! _needed "${perm}"; then
      pend "permission not required for this run: ${perm}"
    elif printf '%s' "${GRANTED}" | grep -q "\"${perm}\""; then
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

# The two OPTIONAL tiers, reported from configuration alone - no gcloud call, because what matters
# here is whether this deployment DECLARED them. An undeclared tier is skipped by its stage, and a
# skip an operator did not expect is the failure mode worth surfacing before the mutation gate.
if [ -n "${MIGRATE_DB_HOST:-}" ]; then
  ok "schema will be migrated on ${MIGRATE_DB_HOST}/${MIGRATE_DB_NAME:-meshpipeline} before anything runs the new image"
else
  pend "no hosted database declared - the pre-deploy migration is skipped (the API still migrates itself on start)"
fi
if [ -n "${WORKER_MIG:-}" ]; then
  ok "queue depth will be published for ${WORKER_MIG} (${WORKER_MIG_ZONE:-zone unset})"
else
  pend "no worker fleet declared - the queue-depth publisher is skipped"
fi

echo
info "Deployment summary"
cat <<SUMMARY
  Project:                ${GCP_PROJECT_ID}
  Region:                 ${GCP_REGION}
  Mesh job:               ${CLOUDRUN_MESH_JOB}      (private; the only deployed workload)
  Mesh exchange bucket:   gs://${GCP_MESH_BUCKET}
  Mesh runtime identity:  ${MESH_SA_EMAIL}
  Schema migration:       ${MIGRATE_DB_HOST:-not declared - skipped}
  Queue-depth publisher:  ${WORKER_MIG:-not declared - skipped}
  The API, the pipeline and every data store run wherever this deployment says they do; only
  what is named above is provisioned here.
SUMMARY

[ "${FAIL}" -eq 0 ] || die "preflight found blocking problems - fix them before deploying"
confirm "Preflight passed. Proceed to provision the mesh executor?"
log "preflight OK"
