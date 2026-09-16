#!/usr/bin/env bash
# Responsibility: Create or validate the one deployed identity - the account the mesh job runs as.
# Boundaries: compute-only and never granted a secret; a supplied account is validated, never altered.

# Create or validate the mesh job's runtime identity (idempotent).
#
# There is exactly one deployed identity: the account the Cloud Run mesh job runs as. It is
# compute-only - its sole access is the exchange bucket (see apply-iam.sh) and it is never granted
# a secret. The local caller is whatever principal the operator's machine already authenticates
# as, so this tooling does not create an identity for it.
#
# MESH_SA_DISPOSITION (from bootstrap) decides whether the account is created here or supplied.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID MESH_SERVICE_ACCOUNT
MESH_SA_DISPOSITION="${MESH_SA_DISPOSITION:-reused}"

if [ "${MESH_SA_DISPOSITION}" = "created" ]; then
  if sa_exists "${MESH_SA_EMAIL}"; then
    log "service account ${MESH_SA_EMAIL} already exists - skipping"
  else
    info "Creating mesh runtime identity ${MESH_SA_EMAIL} (compute-only)"
    gc iam service-accounts create "${MESH_SERVICE_ACCOUNT}" \
      --display-name "Hexera mesh runner (compute-only)"
  fi
else
  info "Validating the SUPPLIED mesh runtime identity (not created here)"
  if sa_exists "${MESH_SA_EMAIL}"; then
    log "mesh SA ${MESH_SA_EMAIL} found"
  else
    warn "mesh SA ${MESH_SA_EMAIL} not found - set MESH_SERVICE_ACCOUNT to the mesh job's"
    warn "actual runtime identity (find it with: gcloud run jobs describe ${CLOUDRUN_MESH_JOB} \\"
    warn "  --region ${GCP_REGION} --format='value(spec.template.spec.template.spec.serviceAccountName)')"
    die "mesh service account mismatch - refusing to guess. Set MESH_SA_DISPOSITION=created to create it."
  fi
fi

# ---------------------------------------------------------------------------------------------
# THE REST OF THE RUNTIME ROSTER, ESTABLISHED HERE RATHER THAN MOMENTS BEFORE EACH IS USED.
#
# WHY THIS EXISTS AT ALL. Every later stage creates the identity it needs if it is missing, so on
# paper this is redundant. In practice it is what makes a FIRST deploy work, because IAM does not
# make a new service account usable the instant it is created:
#
#   Permission 'iam.serviceaccounts.actAs' denied on service account
#   dev-pranav-migrate@hexera-dev.iam.gserviceaccount.com (or it may not exist)
#
# That is the migration stage creating its identity and then, seconds later, deploying a Cloud Run
# job that runs AS it. The deployer holds iam.serviceAccountUser project-wide, so the permission is
# genuinely granted - the account simply is not visible to the actAs check yet. The parenthesis in
# Google's own message ("or it may not exist") is the tell.
#
# Creating them all at stage 6 puts MINUTES between creation and first use instead of seconds, for
# every identity at once. This is what new-env.sh used to do as an owner's act before any deploy
# ran; the reason it worked was never that an owner did it, it was that it happened early.
#
# NOT A REPLACEMENT for the per-stage creation, which stays: a deployment that adds a tier later
# still gets its identity, and an account deleted by hand is still recreated by the stage that
# needs it. This makes the common path fast, not the uncommon path impossible.
#
# ONLY WHAT THIS DEPLOYMENT DECLARES. The names are resolved with the same defaults the consuming
# stages use, so this cannot create an account under a name nothing will look for. The admin
# console is deliberately absent - it is not deployed in every environment, and create-admin-
# service.sh makes its identity when it is.
info "Runtime identities the later stages will run workloads as"
for _spec in \
  "${API_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-api}|runs the API service" \
  "${CONSOLE_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-console}|runs the console" \
  "${MIGRATE_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-migrate}|runs the schema migration job" \
  "${QUEUE_DEPTH_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-queue-depth}|publishes queue depth for the autoscaler" \
  "${WORKER_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-workers}|runs the celery worker fleet"; do
  _sa="${_spec%%|*}"
  _purpose="${_spec##*|}"
  [ -n "${_sa}" ] || continue
  _email="${_sa}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  if sa_exists "${_email}"; then
    log "identity        ${_sa}  (exists)"
    continue
  fi
  # Failure is NOT fatal here. This stage is an optimisation of timing, not the authority on these
  # accounts - the stage that needs one still creates it. Dying here would turn "could not create
  # an identity early" into "could not deploy at all", which is a worse trade than a slow path.
  if gc iam service-accounts create "${_sa}" --display-name "Hexera ${_purpose}" >/dev/null 2>&1; then
    log "identity        ${_sa}  (created - ${_purpose})"
  else
    warn "could not create ${_email} here; the stage that needs it will try again.
       If that stage then fails on iam.serviceaccounts.actAs, this is why - grant
       roles/iam.serviceAccountAdmin on ${GCP_PROJECT_ID} to the deploy identity."
  fi
done

log "done"
