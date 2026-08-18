#!/usr/bin/env bash
# Responsibility: Report what is configured, what is missing, and the exact next command to run.
# Owns: the prerequisite verdict - it inspects every check before deciding, and its exit code is that verdict.
# Boundaries: read-only; it names each setting's state without ever printing what that setting is set to.

# NEVER PROMPT. gcloud asks "API not enabled - enable and retry?" on a project whose APIs are
# off, and every probe below sends its output to /dev/null - so the question would be invisible
# and the doctor would hang on a terminal waiting for an answer nobody can see. A diagnosis must
# also never enable an API as a side effect of being run.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

# `make mesh-doctor` - read-only configuration diagnosis. Shows what is configured and what is not,
# WITHOUT revealing any value, and ends with the exact next command.
#
# EXIT CODE IS THE VERDICT, because this runs in installation scripts as well as in front of a
# person: 0 only when every prerequisite it claims to check is satisfied, 1 when something local is
# missing (a CLI, a setting, the credential file), 2 when the local side is complete but the remote
# session or a mesh resource is not usable. Every check runs before the exit is chosen - stopping at
# the first fault would send someone round the loop once per missing item.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

ok()   { printf '  \033[32mOK\033[0m      %s\n' "$*"; }
miss() { printf '  \033[31mMISSING\033[0m %s\n' "$*"; }
note() { printf '  \033[33m..\033[0m      %s\n' "$*"; }

MISSING_LOCAL=0     # a prerequisite on this machine: CLI, setting, credential file
MISSING_REMOTE=0    # the local side is complete, but the session or a resource is not usable
miss_local()  { miss "$*"; MISSING_LOCAL=$((MISSING_LOCAL + 1)); }
miss_remote() { miss "$*"; MISSING_REMOTE=$((MISSING_REMOTE + 1)); }

NEXT=""   # first unmet requirement wins
suggest() { [ -n "${NEXT}" ] || NEXT="$1"; }

#: True when the variable is set to something other than the placeholder .env.example ships.
isset() { local v="${!1-}"; [ -n "${v}" ] && [ "${v}" != "__set_me__" ]; }

info "Hexera configuration doctor (read-only; values are never shown)"

# A. local development config
echo; printf '\033[1mLocal development (Docker Compose)\033[0m\n'
if [ -f "${REPO_ROOT}/.env" ]; then
  ok "/.env exists - local stack configured (make dev-up)"
  # Presence only. The values are never printed, compared or logged.
  set -a; . "${REPO_ROOT}/.env" 2>/dev/null || true; set +a
else
  miss_local "/.env - local stack not configured (run: make setup)"
  suggest "make setup"
fi

# The settings the mesh executor cannot run without. dev-up enforces them; naming them here is
# what lets someone fix every one of them before starting anything.
# Naming the setting is not the same as telling someone what to put in it. These four identify
# resources that already exist, so the corrective command is the one that reads them off the
# project rather than an instruction to go and find them by hand. That command needs the CLI,
# though, so on a host without it the first thing to do is get the CLI - recommending a command
# that cannot run is the same dead end in a different sentence.
if command -v gcloud >/dev/null 2>&1; then HAVE_GCLOUD=1; else HAVE_GCLOUD=0; fi
for _setting in GCP_PROJECT_ID GCP_REGION CLOUDRUN_JOB GCP_MESH_BUCKET; do
  if isset "${_setting}"; then
    ok "${_setting} is set"
  else
    miss_local "${_setting} is not set in .env"
    if [ "${HAVE_GCLOUD}" = "1" ]; then
      suggest "make mesh-adopt   (reads these from the executor your session can see)"
    else
      suggest "install the Google Cloud CLI and sign in, then: make mesh-adopt"
    fi
  fi
done

ADC="${GOOGLE_ADC_FILE:-${REPO_ROOT}/secrets/gcp/application_default_credentials.json}"
if [ -f "${ADC}" ] && [ -s "${ADC}" ]; then
  ok "application default credentials file present"
else
  miss_local "no application default credentials file at the configured path"
  suggest "gcloud auth application-default login, then copy the file it names to secrets/gcp/application_default_credentials.json"
fi

# B. gcloud session
echo; printf '\033[1mHosted deployment (Google Cloud)\033[0m\n'
if [ "${HAVE_GCLOUD}" = "0" ]; then
  miss_local "gcloud CLI not installed"
  suggest "install the Google Cloud CLI, then: gcloud auth login && gcloud config set project <PROJECT_ID>"
else
  ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
  if [ -n "${ACCOUNT}" ]; then ok "human account: ${ACCOUNT}"
  else miss_remote "no authenticated gcloud account"; suggest "gcloud auth login"; fi
  IMPERSONATION="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
  if [ -n "${IMPERSONATION}" ] && [ "${IMPERSONATION}" != "(unset)" ]; then ok "impersonating: ${IMPERSONATION}"
  else note "impersonation target: (none) - deploying directly as ${ACCOUNT:-the active account}"; fi
  PROJECT="$(gcloud config get-value project 2>/dev/null)"
  if [ -n "${PROJECT}" ] && [ "${PROJECT}" != "(unset)" ]; then
    PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)' 2>/dev/null || true)"
    ok "project: ${PROJECT}${PROJECT_NUMBER:+ (#${PROJECT_NUMBER})}"
  else miss_remote "no GCP project selected"; suggest "gcloud config set project <PROJECT_ID>"; fi
fi

# C. generated deployment state
GEN="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
if [ -f "${GEN}" ]; then
  load_env
  ok "generated state: deploy/gcp/generated.env - BOUND TO project ${GCP_PROJECT_ID:-?} / region ${GCP_REGION:-?}"
  # STALE-STATE: the bound project/region vs the currently active gcloud session.
  if [ -n "${PROJECT:-}" ] && [ "${PROJECT}" != "(unset)" ] \
     && [ -n "${GCP_PROJECT_ID:-}" ] && [ "${PROJECT}" != "${GCP_PROJECT_ID}" ]; then
    miss_remote "STALE STATE: generated.env is bound to '${GCP_PROJECT_ID}' but gcloud is on '${PROJECT}' - a deploy would be refused"
    suggest "regenerate for the active project: bash deploy/gcp/scripts/bootstrap-env.sh --force"
  fi
else
  # A DISCOVERY ARTIFACT, not an application prerequisite. It is written by provisioning, so an
  # operator running against a job and bucket that already exist will never have one - and their
  # stack must still start. The declared resources are checked directly below instead.
  note "no generated deployment state - provisioning has not run here (that is fine for"
  note "  existing resources; 'make mesh-setup' creates it)"
fi

# THE DECLARED RESOURCES, checked from .env rather than from discovery, because .env is what
# the application will actually dispatch to. This is the only resource check an existing-resource
# operator ever gets, and it is the one that matters to them.
if command -v gcloud >/dev/null 2>&1 && [ -n "${ACCOUNT:-}" ] && [ -n "${GCP_PROJECT_ID:-}" ]; then
  echo; printf '\033[1mDeclared mesh resources\033[0m\n'
  if [ -n "${CLOUDRUN_JOB:-}" ]; then
    if gcloud --project "${GCP_PROJECT_ID}" run jobs describe "${CLOUDRUN_JOB}" \
         --region "${GCP_REGION:-us-central1}" >/dev/null 2>&1; then
      ok "Cloud Run job ${CLOUDRUN_JOB} exists in ${GCP_REGION:-us-central1}"
    else
      miss_remote "Cloud Run job ${CLOUDRUN_JOB} not found in ${GCP_REGION:-us-central1}"
      suggest "create it: make mesh-setup   (or correct CLOUDRUN_JOB in .env)"
    fi
  fi
  if [ -n "${GCP_MESH_BUCKET:-}" ]; then
    if gcloud storage buckets describe "gs://${GCP_MESH_BUCKET}" >/dev/null 2>&1; then
      ok "exchange bucket gs://${GCP_MESH_BUCKET} exists"
    else
      miss_remote "exchange bucket gs://${GCP_MESH_BUCKET} not found"
      suggest "create it: make mesh-setup   (or correct GCP_MESH_BUCKET in .env)"
    fi
  fi

  # THE JOB'S OWN ACCESS. Every other check here describes what the OPERATOR can reach. The
  # account that actually opens the exchange bucket is the one the job runs as, and nothing above
  # says anything about it - so a deployment could satisfy every check and still fail on its first
  # dispatch, with the mesh already billing. Both policies are consulted because a project-level
  # grant is as sufficient as a bucket one, and reporting only the bucket would raise a fault
  # against a deployment that works.
  if [ -n "${CLOUDRUN_JOB:-}" ] && [ -n "${GCP_MESH_BUCKET:-}" ]; then
    _runtime_sa="$(gcloud --project "${GCP_PROJECT_ID}" run jobs describe "${CLOUDRUN_JOB}" \
      --region "${GCP_REGION:-us-central1}" \
      --format='value(spec.template.spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
    _bucket_policy="$(gcloud storage buckets get-iam-policy "gs://${GCP_MESH_BUCKET}" --format=json 2>/dev/null || true)"
    _project_policy="$(gcloud projects get-iam-policy "${GCP_PROJECT_ID}" --format=json 2>/dev/null || true)"
    if [ -z "${_runtime_sa}" ]; then
      note "mesh job declares no runtime identity - it runs as the project's default compute account"
    elif [ -z "${_bucket_policy}" ] && [ -z "${_project_policy}" ]; then
      # Reading a policy is itself a permission. Someone joining a project they do not administer
      # can be entitled to dispatch meshes and still not be allowed to see the bindings that prove
      # it, and an unreadable policy is not evidence of a missing one.
      note "cannot read the IAM policies that would show whether the mesh runtime identity reaches the bucket"
    elif python3 - "${_runtime_sa}" "${_bucket_policy}" "${_project_policy}" <<'PY'
import json, sys
member = "serviceAccount:" + sys.argv[1]
# Every role that lets a principal read AND write objects. objectViewer is absent deliberately:
# the job writes its result back, so read-only access is a fault, not a pass.
sufficient = {"roles/storage.objectUser", "roles/storage.objectAdmin", "roles/storage.admin",
              "roles/storage.legacyObjectOwner", "roles/owner", "roles/editor"}
for document in sys.argv[2:]:
    try:
        policy = json.loads(document or "{}")
    except ValueError:
        continue
    for binding in policy.get("bindings") or []:
        if binding.get("role") in sufficient and member in (binding.get("members") or []):
            sys.exit(0)
sys.exit(1)
PY
    then
      ok "mesh runtime identity can exchange objects on gs://${GCP_MESH_BUCKET}"
    else
      miss_remote "the mesh job's runtime identity (${_runtime_sa}) cannot read or write gs://${GCP_MESH_BUCKET} - dispatched meshes will fail after they start"
      suggest "bash deploy/gcp/scripts/apply-iam.sh   (grants the runtime identity storage.objectUser on the exchange bucket)"
    fi
  fi
fi

# D. hosted secrets + discovered resources (need a live session + generated env)
if [ -f "${GEN}" ] && command -v gcloud >/dev/null 2>&1 && [ -n "${ACCOUNT:-}" ]; then
  # Required deploy permissions - a read-only testIamPermissions probe (mutates nothing).
  echo; printf '\033[1mRequired deploy permissions (read-only probe on %s)\033[0m\n' "${GCP_PROJECT_ID}"
  if TOKEN="$(gcloud auth print-access-token 2>/dev/null)" && [ -n "${TOKEN}" ] && command -v curl >/dev/null 2>&1; then
    _PERMS='"run.services.setIamPolicy","resourcemanager.projects.setIamPolicy","run.services.create","run.jobs.create","iam.serviceAccounts.create","secretmanager.secrets.create","storage.buckets.create","artifactregistry.repositories.create","serviceusage.services.enable"'
    _GRANTED="$(curl -sS -X POST \
      "https://cloudresourcemanager.googleapis.com/v1/projects/${GCP_PROJECT_ID}:testIamPermissions" \
      -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
      -d "{\"permissions\":[${_PERMS}]}" 2>/dev/null || true)"
    for _perm in run.services.setIamPolicy resourcemanager.projects.setIamPolicy run.services.create \
                 run.jobs.create iam.serviceAccounts.create secretmanager.secrets.create \
                 storage.buckets.create artifactregistry.repositories.create \
                 serviceusage.services.enable; do
      if printf '%s' "${_GRANTED}" | grep -q "\"${_perm}\""; then ok "perm: ${_perm}"
      else miss_remote "perm MISSING: ${_perm}"; suggest "grant the deployer the missing roles (see docs/deployment/overview.md → IAM)"; fi
    done
  else
    note "cannot probe permissions (no token/curl) - the deployer needs run.admin, resourcemanager.projectIamAdmin, artifactregistry.admin, secretmanager.admin, storage.admin"
  fi


  echo; printf '\033[1mMesh resources\033[0m\n'
  if run_job_exists "${CLOUDRUN_MESH_JOB}"; then ok "mesh job: ${CLOUDRUN_MESH_JOB} (${GCP_REGION})"
  else miss_remote "mesh job ${CLOUDRUN_MESH_JOB} not found in ${GCP_REGION}"
       suggest "create it: make mesh-deploy"; fi
  if bucket_exists "${GCP_MESH_BUCKET}"; then ok "mesh exchange bucket: gs://${GCP_MESH_BUCKET}"
  else miss_remote "mesh exchange bucket gs://${GCP_MESH_BUCKET} not found"; fi

fi

# E. the exact next command, and the verdict
# Only a failing check calls suggest(), so an empty NEXT here means nothing is unmet. Defaulting
# that case to a provisioning command told a healthy machine to reconcile a deployment it may not
# own; when everything is satisfied the next command is to start the stack. A fault that reported
# no suggestion of its own still falls back to provisioning, which is what such a fault needs.
if [ -z "${NEXT}" ]; then
  if [ "${MISSING_LOCAL}" -gt 0 ] || [ "${MISSING_REMOTE}" -gt 0 ]; then NEXT="make mesh-deploy"
  else NEXT="make dev-up"; fi
fi
echo
printf '\033[1mNext command:\033[0m  %s\n' "${NEXT}"

if [ "${MISSING_LOCAL}" -gt 0 ]; then
  printf '\n%s missing local prerequisite(s) - nothing on this machine was changed.\n' "${MISSING_LOCAL}"
  exit 1
fi
if [ "${MISSING_REMOTE}" -gt 0 ]; then
  printf '\n%s cloud prerequisite(s) unusable - nothing was created or modified.\n' "${MISSING_REMOTE}"
  exit 2
fi
printf '\nAll checked prerequisites are satisfied.\n'
