#!/usr/bin/env bash
# Responsibility: Bring the deployment's database to the shipped schema ONCE, before anything serves it.
# Owns: the migration job's identity and its single credential grant; the exit code is the deploy's verdict.
# Boundaries: it applies the migrations the promoted image ships; it never edits a schema by hand and never skips silently in automation.

# Run the pre-deploy MIGRATION. Idempotent: the job is replaced from the manifest on every run and
# `upgrade head` on a database already at head does nothing.
#
#   bash deploy/gcp/scripts/run-migrations.sh      (also stage 10 of deploy.sh)
#
# WHY THE DEPLOY OWNS THIS. deploy/docker/entrypoint.sh applies the same migrations under a Postgres
# advisory lock when the API starts, and it stays - it is the safety net for every path that does
# not come through a deploy. But relying on it makes the schema move whenever N cold-starting
# instances race for a lock, on a service that is already public, with a refusal showing up as
# instances that will not start. Here the schema reaches head once, before the promoted image runs
# anywhere, and a refusal stops the deploy with the message migrate.py wrote.
#
# INPUTS   the deployment env (APP_IMAGE, MIGRATE_DB_*, POSTGRES_PASSWORD_SECRET)
# MUTATES  the migration service account, one secret binding, the migration job, and the SCHEMA
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

MIGRATE_JOB="${CLOUDRUN_MIGRATE_JOB:-${DEPLOYMENT_ID}-migrate}"
MIGRATE_SA="${MIGRATE_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-migrate}"
MIGRATE_SA_EMAIL="${MIGRATE_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# A deployment may genuinely have no hosted database - the local Compose stack migrates itself
# through the same entrypoint, and the mesh-only deployment this tooling started as has no database
# at all. That is a skip, and it is stated.
#
# AUTOMATION MAY NOT SKIP IT. Under DEPLOY_NONINTERACTIVE the whole point is that every target was
# pinned deliberately; a missing one there is a configuration mistake, and skipping the schema step
# on the strength of it is how a deploy ships code against a database nobody migrated.
if [ -z "${MIGRATE_DB_HOST:-}" ] || [ -z "${POSTGRES_PASSWORD_SECRET:-}" ]; then
  if [ "${DEPLOY_NONINTERACTIVE:-0}" = "1" ]; then
    die "no migration target configured, and automation must not skip the schema step.
   Set MIGRATE_DB_HOST (the deployment's database) and POSTGRES_PASSWORD_SECRET (its Secret
   Manager container name), or set MIGRATE_SKIP=1 to state that this deployment has no database."
  fi
  info "No migration target configured - skipping the pre-deploy migration"
  log "set MIGRATE_DB_HOST and POSTGRES_PASSWORD_SECRET to migrate a hosted database from the deploy"
  exit 0
fi
if [ "${MIGRATE_SKIP:-0}" = "1" ]; then
  info "MIGRATE_SKIP=1 - the pre-deploy migration was skipped deliberately"
  exit 0
fi

# The same hosts migrate.py refuses in production, refused one layer earlier and for every
# environment: a deploy that migrated `localhost` migrated the machine it ran on, which in CI is a
# runner that is about to be deleted, and reported success.
case "${MIGRATE_DB_HOST}" in
  localhost|127.0.0.1|::1|postgres|db|host.docker.internal)
    die "MIGRATE_DB_HOST=${MIGRATE_DB_HOST} names a LOCAL database. A deploy migrates the
   deployment's own database - set it to the hosted address (Cloud SQL's private IP)." ;;
esac

# The image is the APPLICATION image, by digest: the same bytes that will read the schema. It is
# already a digest here - promote-release.sh wrote it from the validated release record - and this
# refuses a tag rather than re-resolving one.
require_digest_reference APP_IMAGE "${APP_IMAGE:-}"

info "Migrating ${MIGRATE_DB_USER:-meshpipeline}@${MIGRATE_DB_HOST}:${MIGRATE_DB_PORT:-5432}/${MIGRATE_DB_NAME:-meshpipeline}"
log "image (validated digest): ${APP_IMAGE}"

# 1) the migration identity. It exists for one operation, holds no project-level role, and its only
#    grant is read access to the one secret below.
if sa_exists "${MIGRATE_SA_EMAIL}"; then
  log "migration identity ${MIGRATE_SA_EMAIL} (exists)"
else
  info "Creating migration identity ${MIGRATE_SA_EMAIL}"
  gc iam service-accounts create "${MIGRATE_SA}" \
    --display-name "Hexera schema migration (database-only)" \
    || die "could not create ${MIGRATE_SA_EMAIL}. Creating identities needs
   iam.serviceAccountAdmin, which a DEPLOY identity is deliberately not given - the deployer holds
   four narrow roles and none of them is that one. Create it once, as an owner:
     gcloud iam service-accounts create ${MIGRATE_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera schema migration (database-only)'"
fi

# 2) the ONE credential, granted PER SECRET rather than project-wide - the rule item 9 of the
#    build-out plan states, applied to the identity that actually opens the database.
#    The secret is not asserted to exist first. `secrets describe` needs a Secret Manager read this
#    identity may not hold either, so its absence would be indistinguishable from a missing
#    permission - and reporting the wrong one of those costs an operator an hour. `jobs replace`
#    below refuses a reference it cannot resolve, and says which.
secret_exists "${POSTGRES_PASSWORD_SECRET}" \
  || warn "cannot confirm secret '${POSTGRES_PASSWORD_SECRET}' exists in ${GCP_PROJECT_ID} - it may
       be absent, or this identity may not be allowed to read Secret Manager. If it is absent:
         gcloud secrets create ${POSTGRES_PASSWORD_SECRET} --project ${GCP_PROJECT_ID} --replication-policy=automatic"
#
#    A DEPLOY IDENTITY MAY NOT BE ABLE TO GRANT IT, and that is not a reason to stop: setting IAM on
#    a secret is an owner's act, and the four roles the CI deployer holds do not include it. The
#    grant is attempted, a failure is reported with the command that fixes it, and the EXECUTION
#    below is the verdict - a migration that cannot read the password fails there, loudly, so a
#    missing binding can never pass as a successful deploy.
if gc secrets add-iam-policy-binding "${POSTGRES_PASSWORD_SECRET}" \
     --member "serviceAccount:${MIGRATE_SA_EMAIL}" \
     --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then
  log "secret/${POSTGRES_PASSWORD_SECRET} += roles/secretmanager.secretAccessor -> ${MIGRATE_SA_EMAIL}"
else
  warn "could not set IAM on secret ${POSTGRES_PASSWORD_SECRET} (this identity may not hold
       secretmanager.admin). If the binding is already in place the migration below still runs;
       if it is not, that run fails and this is the command:
         gcloud secrets add-iam-policy-binding ${POSTGRES_PASSWORD_SECRET} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${MIGRATE_SA_EMAIL} --role roles/secretmanager.secretAccessor"
fi

# 3) the job, replaced from the manifest so its image, its database and its identity are whatever
#    this deployment says they are - never what a previous one left behind.
export MIGRATE_SA_EMAIL APP_IMAGE
export CLOUDRUN_MIGRATE_JOB="${MIGRATE_JOB}"
export MIGRATE_DB_PORT="${MIGRATE_DB_PORT:-5432}"
export MIGRATE_DB_NAME="${MIGRATE_DB_NAME:-meshpipeline}"
export MIGRATE_DB_USER="${MIGRATE_DB_USER:-meshpipeline}"
export VPC_NETWORK="${VPC_NETWORK:-default}"
export VPC_SUBNET="${VPC_SUBNET:-default}"
rendered="$(render_manifest "${DEPLOY_DIR}/cloud-run/migrate-job.yaml")"
gc run jobs replace "${rendered}" --region "${GCP_REGION}"

# 4) run it, and WAIT. The exit code is the deploy's: a refusal from migrate.py (unmanaged schema,
#    unknown revision, incomplete schema) stops the deploy here, with the database untouched, rather
#    than becoming instances that will not start after the rollout.
info "Executing ${MIGRATE_JOB} (waiting for completion)"
if ! gc run jobs execute "${MIGRATE_JOB}" --region "${GCP_REGION}" --wait; then
  die "the migration job failed - the schema was NOT advanced and nothing should be rolled onto
   ${APP_IMAGE}. Read what it refused:
     gcloud run jobs executions list --job ${MIGRATE_JOB} --region ${GCP_REGION} --project ${GCP_PROJECT_ID}
     gcloud beta run jobs executions logs read <execution> --region ${GCP_REGION} --project ${GCP_PROJECT_ID}"
fi
log "schema is at head - the API's own advisory-locked migration will find nothing to do"
