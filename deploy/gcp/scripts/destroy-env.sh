#!/usr/bin/env bash
# Responsibility: Delete one personal development environment completely, by deleting the project that IS it.
# Owns: the refusal to touch a shared environment, and the proof that what is being deleted is personal.
# Boundaries: it deletes a project; it never deletes resources inside one.
# Collaborates with: new-env.sh, whose project labels are the evidence this script acts on.

# `make destroy-env SLUG=pranav` - the counterpart to `make new-env`.
#
# WHY THIS IS SHORT, AND WHY THAT IS THE POINT. mesh-destroy.sh is 161 lines because it deletes
# resources INSIDE a project and therefore has to prove, per resource, that this deployment created
# it - a resource it merely reused belongs to somebody else. That proof is expensive and it is
# never complete: it can only cover resources the deployment record names, and a project accretes
# things nobody recorded. A personal environment does not have that problem, because the project
# IS the environment. Deleting it removes the Cloud SQL instance, the Memorystore broker, the
# buckets, the service accounts, the secrets, the images, the IAM, the peering, and the thing
# somebody created by hand last Tuesday that no script has ever heard of.
#
# THE WHOLE RISK IS THEREFORE CONCENTRATED IN ONE QUESTION: is this project a personal environment,
# or is it somebody's production? Everything below exists to answer that, and to refuse when the
# answer is not a clear yes.
#
# WHAT DELETION ACTUALLY DOES. `projects delete` schedules deletion; the project spends ~30 days
# in DELETE_REQUESTED, during which resources stop serving and stop billing and `projects undelete`
# brings it all back. That window is why this is recoverable and why the project id is NOT
# immediately reusable - creating hexera-dev-<slug> again before it expires fails.
#
# INPUTS   SLUG (required), or PROJECT_ID to name the project directly.
# MUTATES  exactly one thing: the lifecycle state of one project.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SLUG="${SLUG:-${1:-}}"
PROJECT_PREFIX="${PROJECT_PREFIX:-hexera-dev-}"

if [ -n "${PROJECT_ID:-}" ]; then
  TARGET="${PROJECT_ID}"
else
  [ -n "${SLUG}" ] || die "no slug.
   Name the environment to destroy:
     make destroy-env SLUG=pranav      ->  ${PROJECT_PREFIX}pranav"
  TARGET="${PROJECT_PREFIX}${SLUG}"
fi

# ---------------------------------------------------------------------------------------------
# 1) the refusals
#
# THE FIRST ONE IS A LITERAL LIST, and it is first for a reason: every check after this reads the
# live project, and a check that depends on a successful API call can be defeated by the API call
# failing. A name match cannot. These two names are never deletable by this script under any
# circumstances, whatever their labels say and whether or not they can be described.
case "${TARGET}" in
  hexera-dev|hexera-prod|hexera)
    die "refusing to delete ${TARGET} - it is a SHARED environment.
   This script destroys personal environments only. Nothing that serves anybody but you is
   reachable from it." ;;
esac

# The prefix rule, second. A project that does not carry the personal-environment prefix was not
# created by new-env.sh, so this script has no business deleting it - including a prod project in
# another organisation that somebody pasted in by mistake.
case "${TARGET}" in
  "${PROJECT_PREFIX}"?*) ;;
  *) die "refusing to delete ${TARGET} - it does not start with '${PROJECT_PREFIX}', so it is not a
   personal environment created by new-env.sh.
   If it genuinely is one under a different prefix, say so explicitly:
     PROJECT_PREFIX=<prefix> make destroy-env SLUG=<slug>" ;;
esac

gcloud projects describe "${TARGET}" >/dev/null 2>&1 \
  || die "no project ${TARGET}.
   Nothing was deleted. List what exists:
     gcloud projects list --filter='labels.app=hexera AND labels.personal=true'"

_state="$(gcloud projects describe "${TARGET}" --format='value(lifecycleState)' 2>/dev/null || true)"
if [ "${_state}" = "DELETE_REQUESTED" ]; then
  info "${TARGET} is already scheduled for deletion - nothing to do."
  log "it stops billing now and is purged in ~30 days. To bring it back:"
  log "  gcloud projects undelete ${TARGET}"
  exit 0
fi

# THE LABEL, third and last. new-env.sh stamps every project it creates with personal=true, so
# this is the positive evidence that the target is what this script is for - as opposed to the
# two checks above, which only establish that it is not obviously something else. A project that
# merely happens to carry the prefix, created by hand for some other purpose, stops here.
_personal="$(gcloud projects describe "${TARGET}" --format='value(labels.personal)' 2>/dev/null || true)"
[ "${_personal}" = "true" ] || die "refusing to delete ${TARGET} - it does not carry the label
   personal=true that new-env.sh stamps on every environment it creates. Its labels are:
     $(gcloud projects describe "${TARGET}" --format='value(labels)' 2>/dev/null || echo '<none>')
   Something else made this project. Delete it deliberately, in the console, or:
     gcloud projects delete ${TARGET}"

# ---------------------------------------------------------------------------------------------
# 2) what is about to be lost, read from the live project rather than assumed
#
# An operator deciding whether to type "yes" needs to know whether this environment currently
# holds anything. Reported as a count per tier, and NEVER as a blocker: a personal environment
# exists to be thrown away, and a script that argued with that would be one people work around.
# Each read is `|| true` - an API that cannot answer must not stop a deletion, it just means the
# summary says less.
info "About to delete ${TARGET}"
_sql="$(gcloud sql instances list --project "${TARGET}" --format='value(name)' 2>/dev/null | grep -c . || true)"
_redis="$(gcloud redis instances list --project "${TARGET}" --region "${GCP_REGION:-us-central1}" \
           --format='value(name)' 2>/dev/null | grep -c . || true)"
_run="$(gcloud run services list --project "${TARGET}" --format='value(metadata.name)' 2>/dev/null | grep -c . || true)"
_buckets="$(gcloud storage buckets list --project "${TARGET}" --format='value(name)' 2>/dev/null | grep -c . || true)"

cat <<PLAN

  Project            ${TARGET}
  Cloud SQL          ${_sql:-?} instance(s)      - DATABASES AND THEIR CONTENTS
  Memorystore        ${_redis:-?} instance(s)
  Cloud Run          ${_run:-?} service(s)
  Buckets            ${_buckets:-?}                  - INCLUDING EVERY UPLOADED GEOMETRY AND RESULT

  Everything in this project goes, including anything created by hand that no script knows about.

  This is RECOVERABLE for about 30 days (gcloud projects undelete ${TARGET}), during which the
  project serves nothing and bills nothing. After that it is permanent, and the project id
  ${TARGET} cannot be reused until the window closes.

PLAN

confirm "Delete ${TARGET} and everything in it?"

# ---------------------------------------------------------------------------------------------
# 3) the one mutation
info "Deleting ${TARGET}"
gcloud projects delete "${TARGET}" --quiet \
  || die "could not delete ${TARGET}. This needs resourcemanager.projects.delete - an owner's
   role, held by a person rather than by any deploy identity.
   A project with LIEN on it also refuses; list them with:
     gcloud alpha resource-manager liens list --project ${TARGET}"

# ---------------------------------------------------------------------------------------------
# 4) the register
#
# Deregistered AFTER the project is gone, never before: a run that removed the entry and then
# failed to delete would leave a billable project that no `make destroy-env` could find again,
# because the slug would no longer resolve to anything.
#
# Best effort, for the same reason new-env.sh's registration is - a missing `gh` must not turn a
# completed deletion into a failed run. The consequence of a stale entry is bounded and loud: the
# slug resolves to a project that no longer exists, and the deploy fails when it authenticates.
if [ -n "${SLUG}" ]; then
  VAR_NAME="HEXERA_PERSONAL_ENVS"
  GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"
  if [ -z "${GITHUB_REPOSITORY}" ]; then
    _origin="$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
    case "${_origin}" in
      *github.com[:/]*)
        GITHUB_REPOSITORY="${_origin#*github.com}"
        GITHUB_REPOSITORY="${GITHUB_REPOSITORY#[:/]}"
        GITHUB_REPOSITORY="${GITHUB_REPOSITORY%.git}" ;;
    esac
  fi
  _deregistered=0
  if [ -n "${GITHUB_REPOSITORY}" ] && command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    _current="$(gh variable get "${VAR_NAME}" --repo "${GITHUB_REPOSITORY}" 2>/dev/null || true)"
    _remaining="$(printf '%s' "${_current}" | tr ', ' '\n' | grep -v "^${SLUG}=" | grep . || true)"
    if printf '%s' "${_remaining}" | gh variable set "${VAR_NAME}" \
         --repo "${GITHUB_REPOSITORY}" --body-file - 2>/dev/null; then
      _deregistered=1
      log "${VAR_NAME}   ${SLUG} removed"
    fi
  fi
  [ "${_deregistered}" = "1" ] || warn "could not update the ${VAR_NAME} repository variable.
       Remove the '${SLUG}=' line by hand so the slug stops resolving:
         https://github.com/${GITHUB_REPOSITORY:-<repo>}/settings/variables/actions"
fi

info "${TARGET} is scheduled for deletion"
log "it stops serving and stops billing now"
log "recover it within ~30 days with:  gcloud projects undelete ${TARGET}"
log "note: re-creating the id needs that window to close, or an undelete first"
