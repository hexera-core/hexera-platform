#!/usr/bin/env bash
# Responsibility: Create the Secret Manager CONTAINERS this deployment references, and grant each one to the single identity that reads it.
# Owns: the roster of secrets a deployment needs, and the rule that a value is never invented, printed or passed as an argument here.
# Boundaries: containers and per-secret bindings only - it adds no version, reads no payload, and creates no identity.

# Create or reconcile the deployment's SECRET CONTAINERS, WITHOUT values.
#
#   bash deploy/gcp/scripts/create-secrets.sh
#
# WHY NO VALUES. A provisioner that generated a credential would be asserting something it cannot
# verify: it does not know the password the database was actually created with, the API key the
# provider actually issued, or whether another provisioner already owns the value. So this creates
# the CONTAINER, states whether it holds a version, and prints the one command an operator runs to
# add one. Where a value IS generated elsewhere - a database password, an HMAC signing secret - the
# container is created only if absent and its versions are left completely alone, so running this
# after that provisioner cannot fight it.
#
# WHY PER SECRET. docs/deployment/gcp-live-inventory.md records the property to preserve:
# `dev-mesh` holds NO project-level roles at all, and every grant in this project is scoped to the
# one resource the identity actually opens. roles/secretmanager.secretAccessor granted project-wide
# would give one identity every credential in the project, including the ones added next year. Each
# binding below names one secret and one service account.
#
# WHO RUNS IT. An owner, once per project, and again whenever the roster changes. NOT the CI
# deployer: that identity holds four narrow roles (create-workload-identity.sh) and none of them is
# a Secret Manager role, deliberately - a deploy that could read the credentials it deploys is a
# deploy that can exfiltrate them.
#
# INPUTS   the deployment env (DEPLOYMENT_ID, the *_SECRET container names, the reader identities)
# MUTATES  Secret Manager containers and their per-secret IAM. It never adds or touches a VERSION.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID DEPLOYMENT_ID

# THE CONTAINER NAMES, from variables so dev and prod differ. POSTGRES_PASSWORD_SECRET is already
# the deployment's own vocabulary - bootstrap-env.sh writes it, validate-config.sh checks it,
# migrate-job.yaml references it and .github/workflows/deploy.yml pins it - so the rest follow the
# same <SETTING>_SECRET spelling rather than inventing a second convention beside it. That the
# setting USER_TOKEN_SECRET then yields USER_TOKEN_SECRET_SECRET is ugly and is not worth a special
# case: one rule that reads oddly once beats two rules.
PG_SECRET="${POSTGRES_PASSWORD_SECRET:-postgres-password}"
MINIO_SECRET="${MINIO_SECRET_KEY_SECRET:-minio-secret-key}"
DEEPINFRA_SECRET="${DEEPINFRA_API_KEY_SECRET:-deepinfra-api-key}"
DEEPSEEK_SECRET="${DEEPSEEK_API_KEY_SECRET:-deepseek-api-key}"
MESH_API_KEY_SECRET_NAME="${MESH_API_KEY_SECRET:-mesh-api-key}"
USER_TOKEN_SECRET_NAME="${USER_TOKEN_SECRET_SECRET:-user-token-secret}"
AUTH_SECRET_NAME="${AUTH_SECRET_SECRET:-console-auth-secret}"
CONSOLE_AUTH_USERS_NAME="${CONSOLE_AUTH_USERS_SECRET:-console-auth-users}"

# THE READERS: MIGRATE_SERVICE_ACCOUNT (written by bootstrap-env.sh), API_SERVICE_ACCOUNT,
# WORKER_SERVICE_ACCOUNT and CONSOLE_SERVICE_ACCOUNT. Each is a service-account ID; the email is
# derived. They are named per role rather than collected into one list because a secret is granted
# to the identity that opens it and to nothing else - the whole point of the per-secret rule - and
# because a deployment with no worker fleet has no worker identity to grant anything to. The roster
# below names the variables and each is resolved at grant time, so declaring one later needs no
# edit here.
#
# API_SERVICE_ACCOUNT and WORKER_SERVICE_ACCOUNT are UNSET by default and that is not an oversight.
# Today hexera-dev's API carries these credentials as plaintext environment values and the worker
# fleet runs as the default compute account with cloud-platform scope; both are findings in the
# inventory, not identities to bind secrets to. An unset reader is reported as ungranted, never
# quietly replaced by the default compute account - which would hand every VM in the project every
# credential named here.
#
# CONSOLE_SERVICE_ACCOUNT reads two of the API's own containers (MESH_API_KEY, USER_TOKEN_SECRET)
# alongside its own two: the console's /api/v1 proxy signs X-User-Id and presents X-API-Key, so it
# genuinely needs both, granted beside API_SERVICE_ACCOUNT rather than instead of it.

# The mesh runtime identity is granted NOTHING here, and that is a property to preserve rather than
# an omission: the mesh job is compute-only, its only access is the exchange bucket, and it holds
# no project-level role at all (create-service-accounts.sh, apply-iam.sh, and the live inventory).
# A model key or a database password reaching it would end that.

# A secret is REQUIRED when a spec this deployment actually applies references it. An empty
# container behind a secretKeyRef is not a warning - Cloud Run refuses the revision, and the deploy
# fails on a credential that was missing long before anyone ran it. Everything else is reported so
# the operator learns it now rather than during a rollout.
PG_REQUIRED=optional
if [ -n "${MIGRATE_DB_HOST:-}" ]; then
  # cloud-run/migrate-job.yaml resolves POSTGRES_PASSWORD from this container, and run-migrations.sh
  # refuses to skip the schema step under DEPLOY_NONINTERACTIVE.
  PG_REQUIRED=required
fi

# setting | container | required? | reader variables, in order | what reads it
SECRETS=(
  "POSTGRES_PASSWORD|${PG_SECRET}|${PG_REQUIRED}|MIGRATE_SERVICE_ACCOUNT API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT|the pre-deploy migration and every runtime that opens the database"
  "MINIO_SECRET_KEY|${MINIO_SECRET}|optional|API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT|object storage for workspaces and results"
  "DEEPINFRA_API_KEY|${DEEPINFRA_SECRET}|optional|API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT|the model provider the agents call"
  "DEEPSEEK_API_KEY|${DEEPSEEK_SECRET}|optional|API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT|the model provider the agents call"
  "MESH_API_KEY|${MESH_API_KEY_SECRET_NAME}|optional|API_SERVICE_ACCOUNT CONSOLE_SERVICE_ACCOUNT|the key the API presents when it submits a mesh job"
  "USER_TOKEN_SECRET|${USER_TOKEN_SECRET_NAME}|optional|API_SERVICE_ACCOUNT CONSOLE_SERVICE_ACCOUNT|the HMAC key user tokens are signed with - generated elsewhere, never here"
  "AUTH_SECRET|${AUTH_SECRET_NAME}|optional|CONSOLE_SERVICE_ACCOUNT|the key Auth.js signs console session cookies with - generated elsewhere, never here"
  "CONSOLE_AUTH_USERS|${CONSOLE_AUTH_USERS_NAME}|optional|CONSOLE_SERVICE_ACCOUNT|the console's email/password users and their scrypt hashes - replaced by database accounts in sub-project B"
)

info "Secret containers for ${DEPLOYMENT_ID} in ${GCP_PROJECT_ID} (${#SECRETS[@]} secrets, no values)"

# One read that separates "Secret Manager is off" from "this identity may not read it" before any
# per-secret call turns the same two causes into five identical failures.
gc secrets list --limit=1 >/dev/null 2>&1 || die \
"cannot list Secret Manager secrets in ${GCP_PROJECT_ID}. Either the API is off:
     bash deploy/gcp/scripts/enable-apis.sh
   or this identity cannot read Secret Manager. This script is an OWNER's act - the CI deployer
   holds no Secret Manager role by design."

# Enabled versions, or '?' when the count itself could not be read - an unknown is never reported
# as a zero, because "it has no version" and "I could not look" lead an operator to different work.
# The payload is NEVER accessed: listing versions returns metadata, `versions access` returns the
# credential, and nothing here has a reason to call the second one.
count_versions() {
  local out
  if out="$(gc secrets versions list "$1" --filter='state:ENABLED' --format='value(name)' 2>/dev/null)"; then
    printf '%s' "$(printf '%s' "${out}" | grep -c . || true)"
  else
    printf '?'
  fi
}

REPORT=()
MISSING_REQUIRED=()
EMPTY_SECRETS=()

for entry in "${SECRETS[@]}"; do
  IFS='|' read -r setting name required readers purpose <<<"${entry}"

  # 1) the container. Created only if absent; an existing one is reused untouched - its replication
  #    policy, its labels and above all its versions are somebody else's decisions.
  if secret_exists "${name}"; then
    disposition=reused
  else
    info "Creating secret ${name}  (${setting} - ${purpose})"
    # Automatic replication: these are read by Cloud Run and Compute in one region today, and a
    # user-managed policy is a location decision that cannot be changed after creation. Labels
    # match what the exchange bucket and the Cloud Run jobs already carry, so a secret is
    # attributable to the deployment that made it.
    gc secrets create "${name}" \
      --replication-policy=automatic \
      --labels "app=hexera,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" >/dev/null \
      || die "could not create secret ${name}. Creating secrets needs secretmanager.admin, which
   the CI deployer deliberately does not hold. Create it once, as an owner:
     gcloud secrets create ${name} --project ${GCP_PROJECT_ID} --replication-policy=automatic"
    disposition=created
  fi

  # 2) the readers, one binding per secret per identity. A reader this deployment has not declared
  #    is reported, not defaulted: guessing an identity here is how a credential ends up readable by
  #    something that never needed it.
  granted=()
  for var in ${readers}; do
    sa_id="${!var:-}"
    if [ -z "${sa_id}" ]; then
      continue
    fi
    sa_email="${sa_id}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
    if ! sa_exists "${sa_email}"; then
      warn "${var}=${sa_id} does not exist yet, so ${name} was not granted to it. The identity is
       created by the stage that uses it (run-migrations.sh, create-service-accounts.sh); rerun
       this afterwards."
      continue
    fi
    if gc secrets add-iam-policy-binding "${name}" \
         --member "serviceAccount:${sa_email}" \
         --role roles/secretmanager.secretAccessor --condition=None >/dev/null 2>&1; then
      log "secret/${name} += roles/secretmanager.secretAccessor -> ${sa_email}"
      granted+=("${sa_id}")
    else
      # Setting IAM on a secret is an owner's act. Reporting the exact command beats stopping: the
      # runtime that cannot read its credential fails loudly on its own, so a missing binding can
      # never pass as a successful deploy (the same contract run-migrations.sh states).
      warn "could not grant ${name} to ${sa_email} (this identity may not hold secretmanager.admin):
         gcloud secrets add-iam-policy-binding ${name} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${sa_email} --role roles/secretmanager.secretAccessor"
    fi
  done

  # 3) does it HAVE a version. This is the question an operator needs answered BEFORE a deploy: an
  #    empty container satisfies every existence check in this tooling and still fails the rollout
  #    that references it.
  versions="$(count_versions "${name}")"
  case "${versions}" in
    0) state="NO VERSION"; EMPTY_SECRETS+=("${name}")
       if [ "${required}" = "required" ]; then MISSING_REQUIRED+=("${name}"); fi ;;
    \?) state="versions unreadable" ;;
    *) state="${versions} version(s)" ;;
  esac

  if [ ${#granted[@]} -eq 0 ]; then
    readers_state="no reader declared"
  else
    readers_state="${granted[*]}"
  fi
  REPORT+=("$(printf '%-22s %-8s %-18s %s' "${name}" "${disposition}" "${state}" "${readers_state}")")
done

printf '\n\033[1m━━━ secret containers ━━━\033[0m\n\n'
printf '  %-22s %-8s %-18s %s\n' "SECRET" "STATE" "VERSIONS" "READERS (secretAccessor)"
for line in "${REPORT[@]}"; do printf '  %s\n' "${line}"; done

cat <<'NOTE'

  Nothing above carries a value. Add one by NAME, reading the value from stdin so it is never a
  command-line argument (visible in `ps` and in your shell history) and never a log line:

    gcloud secrets versions add <SECRET> --project <PROJECT> --data-file=-
      <paste the value, then Ctrl-D>

  Rotating is the same command: a new version becomes `latest`, and every spec that references
  the container picks it up on its next revision. Nothing here disables the old version - that is
  a decision to make once the new one is confirmed working.
NOTE

# A reader this deployment never declared is stated once. Silence here would read as "granted",
# and a credential nothing can open is a deploy that fails on the first request rather than at
# provisioning time.
UNDECLARED=()
[ -n "${API_SERVICE_ACCOUNT:-}" ]    || UNDECLARED+=("API_SERVICE_ACCOUNT")
[ -n "${WORKER_SERVICE_ACCOUNT:-}" ] || UNDECLARED+=("WORKER_SERVICE_ACCOUNT")
[ -n "${MIGRATE_SERVICE_ACCOUNT:-}" ] || UNDECLARED+=("MIGRATE_SERVICE_ACCOUNT")
if [ ${#UNDECLARED[@]} -gt 0 ]; then
  warn "no identity was declared for: ${UNDECLARED[*]} - the secrets above were not granted to a
       runtime that needs them. Set each to the service-account ID that reads the credential and
       rerun; this script will not guess one, and it will never fall back to the default compute
       account, which every VM in the project runs as."
fi
if [ ${#EMPTY_SECRETS[@]} -gt 0 ]; then
  warn "these containers exist but hold no version: ${EMPTY_SECRETS[*]}"
fi
if [ ${#MISSING_REQUIRED[@]} -gt 0 ]; then
  die "a secret this deployment REFERENCES has no version: ${MISSING_REQUIRED[*]}.
   MIGRATE_DB_HOST=${MIGRATE_DB_HOST:-} declares a database, so cloud-run/migrate-job.yaml resolves
   POSTGRES_PASSWORD from Secret Manager and Cloud Run refuses the job while the container is empty.
   Add the version before deploying - finding this now is the point of this script. If the value is
   GENERATED by another provisioner (a database password is), run that one first: this script
   creates the container and will never invent a credential it cannot verify."
fi
log "done - containers and per-secret bindings only; no version was created or read"
