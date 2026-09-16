#!/usr/bin/env bash
# Responsibility: Delete one personal development environment's resources from the host project, by the label they carry.
# Owns: the refusal to touch a shared environment, and the honest account of what deleting by label cannot reach.
# Boundaries: it deletes resources INSIDE one project; it never deletes a project, and never the shared data tier.
# Collaborates with: the deploy workflow, whose DEPLOYMENT_ID is the label this script selects on.

# `make destroy-env SLUG=pranav` - the counterpart to deploying with `-f slug=pranav`.
#
# WHY THIS IS LONGER THAN IT USED TO BE, AND WHY THAT IS A REAL COST.
#
# A personal environment used to BE a project, and this script was 80 lines that deleted it: one
# call that removed the Cloud SQL instance, the broker, the buckets, the service accounts, the
# secrets, the images, the IAM, the peering, and the thing somebody created by hand last Tuesday
# that no script had ever heard of. Completeness was free.
#
# A personal environment is now a set of resources inside hexera-dev, sharing that project with
# shared dev and with every other developer. Deleting the project is therefore not an option, and
# completeness stops being free: this script can only delete what it can FIND, and what it can find
# is what carries the `deployment-id` label every create-*.sh stamps on what it makes.
#
# SO IT DOES NOT CLAIM COMPLETENESS. A resource created by hand in the console, carrying no label,
# survives this script - and the run says so at the end rather than leaving you to discover it on
# a bill. That is the honest version of the trade that was made when personal environments moved
# inside the shared project; pretending otherwise would be worse than the gap itself.
#
# WHAT IS NEVER TOUCHED: the Cloud SQL instance and the Memorystore instance. They are shared with
# shared dev and with every other personal environment, and deleting one to tear down a sandbox
# would take everybody with it. Only the slug's own DATABASE goes.
#
# WHAT DELETION MEANS HERE. Unlike a project, none of this is recoverable after the fact - there is
# no 30-day window and no undelete. The database goes, and with it whatever was in it. That is why
# this confirms before the first mutation.
#
# INPUTS   SLUG (required). PERSONAL_ENV_PROJECT to name a host other than hexera-dev; the
#          ambient GCP_PROJECT_ID is deliberately NOT read - see the pin below.
# MUTATES  the Cloud Run services and jobs, scheduler job, MIG, instance template, buckets,
#          database, HMAC key, secret and service accounts belonging to ONE deployment id.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SLUG="${SLUG:-${1:-}}"

# ---------------------------------------------------------------------------------------------
# 1) the slug, and the two things it may not be
#
# This string becomes a resource-name prefix in a project that also holds shared dev. The whole
# risk is concentrated in one question - is this prefix a personal environment, or is it shared
# dev? - and everything here exists to answer it, and to refuse when the answer is not a clear yes.
[ -n "${SLUG}" ] || die "no slug.
   Name the environment to destroy:
     make destroy-env SLUG=pranav      ->  deletes dev-pranav-* from hexera-dev"

case "${SLUG}" in
  *[!a-z0-9-]*|[!a-z]*|*-)
    die "slug '${SLUG}' is not a valid environment name.
   Lowercase letters, digits and hyphens; must start with a letter and must not end with one." ;;
esac

# `dev` would make DEPLOYMENT_ID=dev-dev, which is not shared dev - but `dev` typed here by
# somebody who believes they are naming shared dev is the mistake worth refusing outright. The
# production names go with it for the same reason.
case "${SLUG}" in
  dev|prod|production|shared|main|staging)
    die "slug '${SLUG}' is reserved - it names or resembles a shared environment.
   Personal environments are named after a person. There is no command here that deletes shared
   dev or production, and this is not it." ;;
esac

DEPLOYMENT_ID="dev-${SLUG}"

# THE HOST PROJECT IS PINNED, NOT INHERITED - and this is a safety property, not a preference.
#
# This script deletes by NAME. `dev-<slug>-api` is a perfectly plausible name in more than one
# project, so reading the target from the ambient environment means an exported GCP_PROJECT_ID left
# over from some earlier command decides where the deletions land - while the confirmation prompt
# below shows the operator the `dev-<slug>` teardown they asked for. They would be answering a
# question about one project and authorising it against another.
#
# An override is still possible, because destroying a personal environment in a different host is
# a real thing to want one day; it just has to be TYPED at this script rather than inherited, and
# it is echoed in the plan so the confirmation is about the project that will actually be touched.
PERSONAL_ENV_PROJECT="${PERSONAL_ENV_PROJECT:-hexera-dev}"
PROJECT_ID="${PERSONAL_ENV_PROJECT}"
GCP_REGION="${GCP_REGION:-us-central1}"
WORKER_ZONE="${WORKER_MIG_ZONE:-us-central1-a}"
export GCP_PROJECT_ID="${PROJECT_ID}" GCP_REGION

# THE PREFIX GUARD. Every name below is built from DEPLOYMENT_ID, and DEPLOYMENT_ID is built from a
# slug that cannot be `dev`. Restated as an assertion anyway, because every deletion in this file
# trusts it: if this prefix were ever exactly `dev`, this script would delete shared dev.
case "${DEPLOYMENT_ID}" in
  dev-?*) ;;
  *) die "refusing to act on deployment id '${DEPLOYMENT_ID}' - it is not a personal environment" ;;
esac

# THE NAME THE DEPLOY ACTUALLY CREATED. The picker emits migrate_db_name=meshpipeline_<slug>, so
# deriving it from DEPLOYMENT_ID instead produced `dev_<slug>` - a database that has never existed.
# Every teardown then reported it absent and left the real one, with its data, in place: the one
# failure mode of this script that loses nothing visibly and keeps a developer's data forever.
DB_NAME="${MIGRATE_DB_NAME:-meshpipeline_${SLUG}}"
# Pinned for the same reason the project is: this names the target of an IRREVERSIBLE delete, and
# an inherited value would point it at another instance holding a database of the same name.
CLOUDSQL_INSTANCE="hexera-dev-pg"

ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
[ -n "${ACCOUNT}" ] || die "no active gcloud account - run: gcloud auth login"

# ---------------------------------------------------------------------------------------------
# 2) what is about to go, stated in full before anything is deleted
info "Personal development environment: ${SLUG}"
cat <<PLAN

  Project            ${PROJECT_ID}      (NOT deleted - it is shared)
  Deployment id      ${DEPLOYMENT_ID}
  Region             ${GCP_REGION}
  Owner performing   ${ACCOUNT}

  DELETED: every resource labelled deployment-id=${DEPLOYMENT_ID} - the API and console services,
  the mesh, migrate and queue-depth jobs, the scheduler, the worker group and its template, the
  exchange, artifacts and transfer buckets, the object-store key and its secret, and the six
  runtime service accounts. Plus the database ${DB_NAME} on ${CLOUDSQL_INSTANCE}, and
  everything in it.

  NEVER TOUCHED: the Cloud SQL instance and the Memorystore instance. They are shared with shared
  dev and with every other personal environment.

  NOT RECOVERABLE. Deleting a project used to leave a 30-day undelete window. Deleting resources
  inside one does not - the database and its contents go for good.

  WHAT THIS CANNOT REACH: anything created by hand that carries no deployment-id label. This run
  lists what it found and what it skipped; read that list before assuming the environment is gone.

PLAN
confirm "Delete everything named ${DEPLOYMENT_ID}-* in ${PROJECT_ID}?"

# ---------------------------------------------------------------------------------------------
# 3) the deletions, in dependency order
#
# Each step reports what it did. `_gone` and `_skipped` accumulate the summary that closes the run,
# because "it printed no error" is not the same claim as "it deleted the thing", and only one of
# those is worth telling somebody who is about to stop paying attention to this environment.
_gone=""
_skipped=""
_note()    { _gone="${_gone}
  deleted   $1"; }
_absent()  { _skipped="${_skipped}
  absent    $1"; }
_failed()  { _skipped="${_skipped}
  FAILED    $1"; }

# `gone_or_note NAME COMMAND...` - run a deletion, and record which of the three outcomes happened.
# A failure is recorded rather than fatal: stopping at the first error would strand every resource
# after it, which on a teardown is the opposite of useful. The summary carries the failures.
_delete() {
  local what="$1"; shift
  if "$@" >/dev/null 2>&1; then _note "${what}"; else
    # Distinguish "not there" from "could not". The describe is the same authority the delete used.
    _failed "${what}"
  fi
}

# The SCHEDULER first: it triggers the queue-depth job, and a schedule firing at a job that has
# just been deleted logs an error every minute until somebody notices.
info "Scheduler and the queue-depth publisher"
if gcloud scheduler jobs describe "${DEPLOYMENT_ID}-queue-depth" \
     --location "${GCP_REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  _delete "scheduler ${DEPLOYMENT_ID}-queue-depth" \
    gcloud scheduler jobs delete "${DEPLOYMENT_ID}-queue-depth" \
      --location "${GCP_REGION}" --project "${PROJECT_ID}" --quiet
else
  _absent "scheduler ${DEPLOYMENT_ID}-queue-depth"
fi

# THE AUTOSCALER BEFORE THE GROUP. An autoscaler whose group has gone is an orphan Google will not
# always tidy, and deleting the group first makes the autoscaler undeletable by name.
info "Worker fleet"
if gcloud compute instance-groups managed describe "${DEPLOYMENT_ID}-workers" \
     --zone "${WORKER_ZONE}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  _delete "autoscaler ${DEPLOYMENT_ID}-workers" \
    gcloud compute instance-groups managed stop-autoscaling "${DEPLOYMENT_ID}-workers" \
      --zone "${WORKER_ZONE}" --project "${PROJECT_ID}" --quiet
  _delete "instance group ${DEPLOYMENT_ID}-workers" \
    gcloud compute instance-groups managed delete "${DEPLOYMENT_ID}-workers" \
      --zone "${WORKER_ZONE}" --project "${PROJECT_ID}" --quiet
else
  _absent "instance group ${DEPLOYMENT_ID}-workers"
fi

# The templates are versioned (`<mig>-tpl-<digest>-<suffix>` - named after the GROUP, not the
# deployment id, which an earlier filter here got wrong and so matched nothing), so there is no
# single name to delete -
# they are selected by prefix, which is the one place this script does match on a name rather than
# a label. A template still referenced by a group refuses deletion, which is why the group went first.
for _tpl in $(gcloud compute instance-templates list --project "${PROJECT_ID}" \
                --filter="name~^${DEPLOYMENT_ID}-workers-tpl-" --format='value(name)' 2>/dev/null); do
  _delete "instance template ${_tpl}" \
    gcloud compute instance-templates delete "${_tpl}" --project "${PROJECT_ID}" --quiet
done

info "Cloud Run services and jobs"
for _svc in "${DEPLOYMENT_ID}-api" "${DEPLOYMENT_ID}-console"; do
  if gcloud run services describe "${_svc}" --region "${GCP_REGION}" \
       --project "${PROJECT_ID}" >/dev/null 2>&1; then
    _delete "service ${_svc}" \
      gcloud run services delete "${_svc}" --region "${GCP_REGION}" \
        --project "${PROJECT_ID}" --quiet
  else
    _absent "service ${_svc}"
  fi
done
for _job in "${DEPLOYMENT_ID}-mesh" "${DEPLOYMENT_ID}-migrate" "${DEPLOYMENT_ID}-queue-depth"; do
  if gcloud run jobs describe "${_job}" --region "${GCP_REGION}" \
       --project "${PROJECT_ID}" >/dev/null 2>&1; then
    _delete "job ${_job}" \
      gcloud run jobs delete "${_job}" --region "${GCP_REGION}" \
        --project "${PROJECT_ID}" --quiet
  else
    _absent "job ${_job}"
  fi
done

# THE DATABASE, and ONLY the database. The INSTANCE is shared - see the header.
info "Database (the instance it lives on is shared and is left alone)"
if gcloud sql databases describe "${DB_NAME}" --instance "${CLOUDSQL_INSTANCE}" \
     --project "${PROJECT_ID}" >/dev/null 2>&1; then
  _delete "database ${DB_NAME} on ${CLOUDSQL_INSTANCE}" \
    gcloud sql databases delete "${DB_NAME}" --instance "${CLOUDSQL_INSTANCE}" \
      --project "${PROJECT_ID}" --quiet
else
  _absent "database ${DB_NAME} on ${CLOUDSQL_INSTANCE}"
fi

# THE OBJECT-STORE KEY BEFORE ITS SERVICE ACCOUNT. An HMAC key must be DEACTIVATED before it can be
# deleted, and a service account cannot be deleted while it still owns one - so this order is not
# cosmetic. Deleting the account first leaves a key that nothing can name.
info "Object-store key"
API_SA_EMAIL="${DEPLOYMENT_ID}-api@${PROJECT_ID}.iam.gserviceaccount.com"
for _key in $(gcloud storage hmac list --service-account="${API_SA_EMAIL}" \
                --project "${PROJECT_ID}" --format='value(accessId)' 2>/dev/null); do
  gcloud storage hmac update "${_key}" --deactivate --project "${PROJECT_ID}" >/dev/null 2>&1 || true
  _delete "hmac key ${_key}" \
    gcloud storage hmac delete "${_key}" --project "${PROJECT_ID}" --quiet
done

info "Buckets"
GCP_PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-$(gcloud projects describe "${PROJECT_ID}" \
  --format='value(projectNumber)' 2>/dev/null || true)}"
for _suffix in exchange artifacts transfer; do
  _bucket="${DEPLOYMENT_ID}-${_suffix}-${GCP_PROJECT_NUMBER}"
  if gcloud storage buckets describe "gs://${_bucket}" >/dev/null 2>&1; then
    # --recursive: a bucket with objects in it refuses deletion, and an artifacts bucket always
    # has objects in it.
    _delete "bucket ${_bucket}" \
      gcloud storage rm --recursive "gs://${_bucket}" --project "${PROJECT_ID}" --quiet
  else
    _absent "bucket ${_bucket}"
  fi
done

info "Secret"
HMAC_SECRET="minio-secret-key-${SLUG}"
if gcloud secrets describe "${HMAC_SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  _delete "secret ${HMAC_SECRET}" \
    gcloud secrets delete "${HMAC_SECRET}" --project "${PROJECT_ID}" --quiet
else
  _absent "secret ${HMAC_SECRET}"
fi

# THE IDENTITIES LAST. Everything above may run as one of them, and Cloud Run will not delete a
# revision cleanly while its identity is disappearing underneath it.
info "Runtime identities"
for _sa in api console mesh migrate queue-depth workers; do
  _email="${DEPLOYMENT_ID}-${_sa}@${PROJECT_ID}.iam.gserviceaccount.com"
  if sa_exists "${_email}"; then
    _delete "identity ${DEPLOYMENT_ID}-${_sa}" \
      gcloud iam service-accounts delete "${_email}" --project "${PROJECT_ID}" --quiet
  else
    _absent "identity ${DEPLOYMENT_ID}-${_sa}"
  fi
done

# ---------------------------------------------------------------------------------------------
# 4) what happened, including what did not
#
# THE LABEL SWEEP IS THE HONEST PART OF THIS SCRIPT. Everything above deletes resources this
# tooling knows the NAMES of. Anything else this environment owns - a resource added by hand, or
# one created by a stage added after this script was last updated - is found here by its label and
# reported, not deleted: acting on a resource this script cannot name is how a teardown becomes a
# different kind of incident.
info "Sweeping for anything else labelled deployment-id=${DEPLOYMENT_ID}"
_stragglers="$(gcloud asset search-all-resources --scope="projects/${PROJECT_ID}" \
  --query="labels.deployment-id=${DEPLOYMENT_ID}" --format='value(name)' 2>/dev/null || true)"

info "Environment ${SLUG}"
if [ -n "${_gone}" ]; then
  printf '\n  DELETED%s\n' "${_gone}"
else
  printf '\n  DELETED  nothing - no resource of this environment was found\n'
fi
if [ -n "${_skipped}" ]; then
  printf '\n  NOT DELETED%s\n' "${_skipped}"
  printf '\n  A FAILED line above is still billing. Re-run this script - every step is idempotent.\n'
fi
if [ -n "${_stragglers}" ]; then
  printf '\n  STILL LABELLED %s - found by label, NOT deleted, because this script cannot name them:\n' "${DEPLOYMENT_ID}"
  printf '%s\n' "${_stragglers}" | sed 's/^/    /'
  printf '  Delete these by hand, or they keep billing.\n'
fi
printf '\n  The %s instance and the Memorystore broker were NOT touched - they are shared.\n' "${CLOUDSQL_INSTANCE}"
printf '  Anything created by hand WITHOUT a deployment-id label is not covered by either list above.\n\n'
log "done"
