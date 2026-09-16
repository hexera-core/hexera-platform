#!/usr/bin/env bash
# Responsibility: Put the first VALUE in each secret container a personal environment needs, once.
# Owns: which credentials are generated here and which are copied from an environment that has them.
# Boundaries: it never overwrites an enabled version, and it never prints a payload.
# Collaborates with: create-secrets.sh, which owns containers and IAM in the shared environments.

# Seed the credential VALUES for one deployment.
#
# WHY THIS EXISTS SEPARATELY FROM create-secrets.sh. That script creates containers and grants the
# runtime identities access to them, and it deliberately never adds a version - the comment at its
# head is explicit that a deploy which could read the credentials it deploys is a deploy that can
# exfiltrate them. That rule is right for the shared environments, where an owner seeds the values
# by hand once and they outlive every deploy. It is the wrong rule for a personal environment,
# because "by hand, once" happens EVERY TIME somebody creates one, and a step a person performs by
# hand on every new environment is a step that will be performed wrongly or skipped.
#
# So this script is the hand, automated - and it is still an OWNER's act, never CI's. It is run on
# a developer's machine under a developer's own credentials. The
# federated deploy identity holds no role that lets it read a payload (create-workload-identity.sh
# builds a custom Secret Manager role precisely to omit versions.access), so nothing here is
# reachable from a workflow run.
#
# TWO KINDS OF CREDENTIAL, and the difference is the whole design:
#
#   GENERATED  MESH_API_KEY, USER_TOKEN_SECRET, AUTH_SECRET.
#              These are randomness. Nothing outside the environment knows or needs to know them,
#              so a personal environment mints its own and they are isolated by construction -
#              a slug environment cannot sign a token another environment accepts.
#
#   COPIED     DEEPINFRA_API_KEY, DEEPSEEK_API_KEY.
#              These address a THIRD PARTY that issued them. They cannot be generated, and a
#              personal environment that does not have them cannot run a single job. They are read
#              from the environment named by SEED_SOURCE_PROJECT, under the operator's own
#              credentials, and written straight through - never logged, never written to a file.
#
# IDEMPOTENT, AND THAT IS THE SAFETY PROPERTY. A container that already holds an ENABLED version is
# left exactly as it is. Rerunning this after a partial failure fills only what is still empty; it
# can never rotate a credential a running service is using, and it can never replace a value an
# operator deliberately set by hand.
#
# INPUTS   SLUG (or GCP_PROJECT_ID + DEPLOYMENT_ID), SEED_SOURCE_PROJECT (default hexera-dev).
#          DEEPINFRA_API_KEY / DEEPSEEK_API_KEY in the environment override the copy.
# MUTATES  Secret Manager containers and their FIRST version. Nothing else.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

# WHICH ENVIRONMENT. A caller may export both, which is the common path. A person running it by
# hand to fill a key that could not be copied has only the slug - the same word they deploy with -
# so accept that and derive the rest, rather than
# making them recall a project id and a deployment id to repair one secret.
if [ -z "${GCP_PROJECT_ID:-}" ] && [ -n "${SLUG:-${1:-}}" ]; then
  GCP_PROJECT_ID="${PROJECT_PREFIX:-hexera-dev-}${SLUG:-$1}"
  DEPLOYMENT_ID="${DEPLOYMENT_ID:-dev}"
  export GCP_PROJECT_ID DEPLOYMENT_ID
fi
require_vars GCP_PROJECT_ID DEPLOYMENT_ID

# A slug that names no project stops here rather than at the first Secret Manager call, whose
# error would be about permissions on a project that does not exist.
gcloud projects describe "${GCP_PROJECT_ID}" >/dev/null 2>&1 \
  || die "no project ${GCP_PROJECT_ID}.
   Name the environment to seed:
     make seed-secrets SLUG=<slug>
   or list what exists:
     gcloud projects list --filter='labels.app=hexera AND labels.personal=true'"

# WHERE THE THIRD-PARTY KEYS COME FROM. Named rather than assumed, so copying out of an environment
# is always a stated choice - and so a future organisation whose provider keys live somewhere other
# than hexera-dev changes one variable instead of editing this file.
SEED_SOURCE_PROJECT="${SEED_SOURCE_PROJECT:-hexera-dev}"

PG_SECRET="${POSTGRES_PASSWORD_SECRET:-postgres-password}"
MESH_API_KEY_SECRET_NAME="${MESH_API_KEY_SECRET:-mesh-api-key}"
USER_TOKEN_SECRET_NAME="${USER_TOKEN_SECRET_SECRET:-user-token-secret}"
AUTH_SECRET_NAME="${AUTH_SECRET_SECRET:-console-auth-secret}"
DEEPINFRA_SECRET="${DEEPINFRA_API_KEY_SECRET:-deepinfra-api-key}"
DEEPSEEK_SECRET="${DEEPSEEK_API_KEY_SECRET:-deepseek-api-key}"

info "Seeding credential values for ${DEPLOYMENT_ID} in ${GCP_PROJECT_ID}"

# One read that separates "Secret Manager is off" from "this identity may not use it", before any
# per-secret call turns the same two causes into six identical failures. Same guard, and the same
# reasoning, as create-secrets.sh.
gc secrets list --limit=1 >/dev/null 2>&1 || die \
"cannot list Secret Manager secrets in ${GCP_PROJECT_ID}. Either the API is off:
     bash deploy/gcp/scripts/enable-apis.sh
   or this identity cannot use Secret Manager. Seeding is an OWNER's act - run it as yourself,
   not as a deploy identity."

# has_enabled_version NAME - true when the container exists AND holds at least one ENABLED version.
# The payload is never accessed: listing versions returns metadata only, and nothing here has a
# reason to call `versions access` on a secret it is about to fill.
has_enabled_version() {
  local n
  secret_exists "$1" || return 1
  n="$(gc secrets versions list "$1" --filter='state:ENABLED' --format='value(name)' 2>/dev/null \
       | grep -c . || true)"
  [ "${n:-0}" -gt 0 ]
}

ensure_container() {
  local name="$1"
  if secret_exists "${name}"; then return 0; fi
  gc secrets create "${name}" \
    --replication-policy=automatic \
    --labels "app=hexera,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" >/dev/null \
    || die "could not create secret ${name} in ${GCP_PROJECT_ID}. Creating containers needs
   secretmanager.admin, which is an owner's role - this script is an owner's act."
}

# add_version NAME - read the payload from STDIN and add it as a version. The value is NEVER an
# argument and never a log line: `--data-file=-` keeps it off the process table, which `echo
# "$secret"` in an argument list would not.
add_version() {
  gc secrets versions add "$1" --data-file=- >/dev/null \
    || die "could not add a version to ${1} in ${GCP_PROJECT_ID}"
}

SEEDED=()
KEPT=()
MISSING=()

# ---------------------------------------------------------------------------------------------
# generated credentials
#
# 32 bytes of CSPRNG output, base64'd. `openssl rand` rather than $RANDOM, which is neither
# cryptographically strong nor 32 bytes of anything. USER_TOKEN_SECRET and AUTH_SECRET are HMAC
# and cookie-signing keys, so their strength IS the isolation between this environment and any
# other; a weak one there is not a cosmetic problem.
gen_secret() { openssl rand -base64 32 | tr -d '\n'; }

# POSTGRES_PASSWORD IS NOT SEEDED HERE, and that is the point rather than an omission.
#
# create-data-tier.sh owns that credential end to end: it decides the user and the password
# TOGETHER, because they are one credential - "an account without the password its runtimes hold is
# no more usable than a password with no account". Its first-run path generates the password, stores
# it, and creates the user with it, in that order, needing only versions.add - which the deploy
# identity has.
#
# Seeding it here manufactured the one state that path cannot recover from: a stored version with no
# database user. That is read as "the runtimes already hold this password, so create the user WITH
# it" - which requires versions.access, which the deploy identity is deliberately never given. The
# result was a deploy that created Cloud SQL, created the database, and then died one step short of
# the user, needing an owner to intervene on every single new environment.
#
# The container is created below so the deploy finds one; the VALUE is the data tier's to write.
for entry in \
  "${MESH_API_KEY_SECRET_NAME}|the key the API presents when it submits a mesh job" \
  "${USER_TOKEN_SECRET_NAME}|the HMAC key user tokens are signed with" \
  "${AUTH_SECRET_NAME}|the key Auth.js signs console session cookies with"; do
  name="${entry%%|*}"
  purpose="${entry##*|}"
  ensure_container "${name}"
  if has_enabled_version "${name}"; then
    KEPT+=("${name}")
    log "${name}  (already holds a version - untouched)"
  else
    gen_secret | add_version "${name}"
    SEEDED+=("${name}")
    log "${name}  (generated - ${purpose})"
  fi
done

# ---------------------------------------------------------------------------------------------
# copied credentials
#
# THE ONE THING THAT CANNOT BE GENERATED. A provider key is issued by DeepInfra or DeepSeek to an
# account; minting randomness here would produce an environment that provisions cleanly and fails
# on its first model call, which is the worst of both outcomes - the failure would surface hours
# later, in an agent run, as an authentication error nobody would connect to environment creation.
#
# So: taken from the process environment if the operator supplied it, else copied from
# SEED_SOURCE_PROJECT, else REPORTED AS MISSING and the container left empty. It is never faked.
copy_secret() {  # copy_secret <container> <override-var> <provider>
  local name="$1" override_var="$2" provider="$3" value=""
  ensure_container "${name}"
  if has_enabled_version "${name}"; then
    KEPT+=("${name}")
    log "${name}  (already holds a version - untouched)"
    return 0
  fi
  value="${!override_var:-}"
  if [ -n "${value}" ]; then
    printf '%s' "${value}" | add_version "${name}"
    SEEDED+=("${name}")
    log "${name}  (from ${override_var} in the environment - ${provider})"
    unset value
    return 0
  fi
  # `|| true` on the read, then a test on the result: a refused read and an absent secret both
  # mean "this cannot be copied", and both must leave the container empty and REPORTED rather
  # than seeded with an error message.
  value="$(gcloud --project "${SEED_SOURCE_PROJECT}" secrets versions access latest \
             --secret="${name}" 2>/dev/null || true)"
  if [ -n "${value}" ]; then
    printf '%s' "${value}" | add_version "${name}"
    SEEDED+=("${name}")
    log "${name}  (copied from ${SEED_SOURCE_PROJECT} - ${provider})"
    unset value
    return 0
  fi
  MISSING+=("${name}")
  warn "${name} is EMPTY - could not read it from ${SEED_SOURCE_PROJECT} and ${override_var} is unset."
}

copy_secret "${DEEPINFRA_SECRET}" DEEPINFRA_API_KEY "the model provider the builder and reviewer call"
copy_secret "${DEEPSEEK_SECRET}"  DEEPSEEK_API_KEY  "the model provider intake and search call"

# The object store's HMAC secret container is created here but deliberately left EMPTY: the value
# is one half of a key pair that create-object-storage.sh mints during the deploy, and only that
# script knows the access id that goes with it. Creating the container now means the deploy finds
# it rather than needing container-creation authority at that moment.
ensure_container "${MINIO_SECRET_KEY_SECRET:-minio-secret-key}"
# Likewise the database password's container - see the note above the generated block. Empty here;
# create-data-tier.sh writes the version at the moment it creates the user that answers to it.
ensure_container "${PG_SECRET}"

# ---------------------------------------------------------------------------------------------
info "Seeding summary for ${GCP_PROJECT_ID}"
log "seeded now      ${#SEEDED[@]}"
log "already present ${#KEPT[@]}"
if [ "${#MISSING[@]}" -gt 0 ]; then
  log "STILL EMPTY     ${#MISSING[@]}"
  warn "This environment cannot run a job until these hold a value:
     ${MISSING[*]}
   Supply them either by exporting the key and rerunning:
     DEEPINFRA_API_KEY=... DEEPSEEK_API_KEY=... bash deploy/gcp/scripts/seed-secrets.sh
   or directly:
     printf %s '<key>' | gcloud secrets versions add <container> --project ${GCP_PROJECT_ID} --data-file=-"
  exit 3
fi
log "every credential this environment needs holds a value"
