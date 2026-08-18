#!/usr/bin/env bash
# Responsibility: Remove exactly the cloud resources this deployment's record says it created.
# Owns: the destroy plan, the ownership decision, and the confirmations that gate each deletion.
# Boundaries: it deletes; it provisions nothing and never edits .env or the deployment record.
# Collaborates with: write-deployment-state.sh, whose per-resource disposition is its only ownership evidence.

# `make mesh-destroy` - the counterpart to `make mesh-setup`.
#
# THE RULE: a resource is deleted only when the deployment record says this deployment CREATED it.
# `reused` means the operator pointed us at something that already existed, and deleting it would
# destroy someone else's resource that merely happens to be named in our configuration. There is no
# name pattern, no wildcard and no discovery here - if the record does not prove ownership, the
# resource is left alone and reported.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

RECORD="${DEPLOY_DIR}/output/deployment.json"
DRY_RUN=1
for arg in "$@"; do
  case "${arg}" in
    --apply) DRY_RUN=0 ;;
    --dry-run) DRY_RUN=1 ;;
    *) die "unknown argument: ${arg} (use --apply to delete; the default is a dry run)" ;;
  esac
done

if [ ! -f "${RECORD}" ]; then
  info "No deployment record at ${RECORD}."
  info "Nothing is known to have been created here, so there is nothing to destroy."
  info "A record is written by 'make mesh-setup'; without one this script will not guess."
  exit 0
fi

# Read the record through python rather than grep: the disposition decides whether a real resource
# is deleted, and that decision must not depend on how a line happens to be formatted.
read -r REC_PROJECT REC_REGION <<EOF
$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(d.get("project",""), d.get("region",""))' "${RECORD}")
EOF
[ -n "${REC_PROJECT}" ] || die "the deployment record names no project - refusing to act on it"

# The record is bound to one project. If the configured project differs, the operator is pointing
# at a different deployment than the one this record describes, and deleting by it would be wrong.
if [ -n "${GCP_PROJECT_ID:-}" ] && [ "${GCP_PROJECT_ID}" != "${REC_PROJECT}" ]; then
  die "the record describes project '${REC_PROJECT}' but GCP_PROJECT_ID is '${GCP_PROJECT_ID}' -
       refusing to destroy resources recorded for a different project"
fi
GCP_PROJECT_ID="${REC_PROJECT}"
GCP_REGION="${REC_REGION:-${GCP_REGION:-}}"
export GCP_PROJECT_ID GCP_REGION

created_name() {
  python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
r=(d.get("resources") or {}).get(sys.argv[2]) or {}
print(r.get("name","") if r.get("disposition")=="created" else "")' "${RECORD}" "$1"
}
recorded_name() {
  python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
r=(d.get("resources") or {}).get(sys.argv[2]) or {}
print(r.get("name",""), r.get("disposition",""))' "${RECORD}" "$1"
}

JOB="$(created_name mesh_job)"
BUCKET="$(created_name exchange_bucket)"
REPO="$(created_name artifact_registry)"
SA="$(created_name mesh_service_account)"

echo
info "Destroy plan for project ${GCP_PROJECT_ID} (region ${GCP_REGION:-unset})"
echo
for key in mesh_job exchange_bucket artifact_registry mesh_service_account; do
  read -r name disposition <<EOF
$(recorded_name "${key}")
EOF
  if [ -z "${name}" ]; then
    printf '  %-22s (not in the record)\n' "${key}"
  elif [ "${disposition}" = "created" ]; then
    printf '  %-22s DELETE   %s\n' "${key}" "${name}"
  else
    printf '  %-22s keep     %s  (disposition=%s - not created by this deployment)\n' \
      "${key}" "${name}" "${disposition}"
  fi
done
echo

if [ "${DRY_RUN}" = "1" ]; then
  info "Dry run - nothing was deleted. Re-run with --apply to carry out the plan above."
  exit 0
fi

# Typing the project id is the last gate before anything is removed. It is the same confirmation
# provisioning asks for, and for the same reason: a destroy aimed at the wrong project is not
# recoverable.
if [ "${ASSUME_YES:-0}" != "1" ]; then
  printf '  Type the project id to destroy the resources marked DELETE: '
  read -r reply
  [ "${reply}" = "${GCP_PROJECT_ID}" ] || { info "Cancelled - nothing was deleted."; exit 1; }
fi

# Each deletion is guarded by its own existence check, so a partially provisioned deployment and a
# second run of this script both reach the same end state without an error.
if [ -n "${JOB}" ]; then
  if run_job_exists "${JOB}"; then
    gc run jobs delete "${JOB}" --region "${GCP_REGION}" --quiet
    info "deleted Cloud Run job ${JOB}"
  else
    info "Cloud Run job ${JOB} is already gone"
  fi
fi

if [ -n "${BUCKET}" ]; then
  if bucket_exists "${BUCKET}"; then
    # A non-empty exchange bucket may hold a mesh somebody is still waiting on, so emptying it is
    # a separate decision from removing the deployment.
    if [ -n "$(gcloud storage ls "gs://${BUCKET}/**" --limit 1 2>/dev/null || true)" ]; then
      if [ "${ASSUME_YES:-0}" != "1" ]; then
        printf '  gs://%s is NOT empty. Delete it and every object in it? [y/N] ' "${BUCKET}"
        read -r reply
        case "${reply}" in y|Y|yes|YES) ;; *) info "kept gs://${BUCKET}"; BUCKET="" ;; esac
      fi
    fi
    if [ -n "${BUCKET}" ]; then
      gcloud storage rm -r "gs://${BUCKET}" --quiet
      info "deleted exchange bucket gs://${BUCKET}"
    fi
  else
    info "exchange bucket gs://${BUCKET} is already gone"
  fi
fi

if [ -n "${REPO}" ]; then
  if gc artifacts repositories describe "${REPO}" --location "${GCP_REGION}" >/dev/null 2>&1; then
    gc artifacts repositories delete "${REPO}" --location "${GCP_REGION}" --quiet
    info "deleted Artifact Registry repository ${REPO}"
  else
    info "Artifact Registry repository ${REPO} is already gone"
  fi
fi

# The service account goes last: its IAM bindings on the job and bucket disappear with those
# resources, so removing it earlier would leave bindings referring to a principal that is gone.
if [ -n "${SA}" ]; then
  if gc iam service-accounts describe "${SA}" >/dev/null 2>&1; then
    gc iam service-accounts delete "${SA}" --quiet
    info "deleted service account ${SA}"
  else
    info "service account ${SA} is already gone"
  fi
fi

echo
info "Done. Resources recorded as 'reused' were left untouched, and the project was not deleted."
info "The deployment record is kept as the account of what existed; delete it yourself if you"
info "want a clean slate: ${RECORD}"
