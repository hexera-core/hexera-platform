#!/usr/bin/env bash
# Responsibility: Provision the deployment's DATABASE and BROKER on private addresses, and the one credential they share.
# Owns: the private-services range, the durability settings of an instance this run creates, and the generated application password.
# Boundaries: it creates or validates; it never recreates, never deletes, and never patches an instance it did not create.

# Provision the DATA TIER - Cloud SQL for PostgreSQL and Memorystore for Redis, both reachable only
# over the VPC - plus the application database, its user, and that user's password.
#
#   bash deploy/gcp/scripts/create-data-tier.sh
#
# IT PRODUCES THE FIXED SHAPE, NOT THE ONE DEV HAS. docs/deployment/gcp-live-inventory.md records
# hexera-dev's database with backups disabled, no point-in-time recovery, deletion protection off,
# and a public IP that accepts unencrypted connections from anywhere - "one `gcloud sql instances
# delete` away from total loss". An instance created here has none of those properties: private IP
# only, scheduled backups with a stated retention, PITR, deletion protection, and TLS required.
#
#   EACH RESOURCE IS DECIDED ON ITS OWN, the way create-mesh-tier.sh decides the exchange bucket and
#   the mesh job: a project that already owns a database may still need its broker created, so
#   neither disposition speaks for the other.
#
#   AN EXISTING INSTANCE IS REUSED - VALIDATED AND LEFT UNTOUCHED. A database is stateful and this
#   run did not create it; a patch that flips a durability setting takes a maintenance action on the
#   instance the deploy is about to migrate. So a reused instance whose shape is wrong is REPORTED,
#   with the exact command that fixes it, and never silently repaired.
#
# THE PASSWORD IS GENERATED HERE AND NEVER LEAVES SECRET MANAGER. It is not echoed, not written to
# the deployment env, and never a command-line argument: it reaches gcloud through `--flags-file=-`
# on stdin, so it appears in no argv and in no `ps` output. If the secret already holds an enabled
# version, THAT version is the password and nothing is rotated - a deploy that minted a new
# credential on every run would lock out every runtime still holding the previous one.
#
# INPUTS   the deployment env (VPC_NETWORK, CLOUDSQL_*, REDIS_*, MIGRATE_DB_*, POSTGRES_PASSWORD_SECRET)
# MUTATES  one global address, one service-networking peering, the SQL instance, its database and
#          user, one Secret Manager secret, and the Redis instance
# OUTPUT   the resolved PRIVATE addresses, written back into the deployment env for the stages that
#          follow - run-migrations.sh reads MIGRATE_DB_HOST, the queue-depth publisher reads REDIS_URL
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

VPC_NETWORK="${VPC_NETWORK:-default}"
NETWORK_URI="projects/${GCP_PROJECT_ID}/global/networks/${VPC_NETWORK}"

# The reserved range private services access allocates out of. The name matches what Google's own
# console picks, and what hexera-dev already has, so a project that was clicked together before this
# script existed is recognised as already configured rather than given a second range.
PSA_RANGE_NAME="${PSA_RANGE_NAME:-google-managed-services-${VPC_NETWORK}}"
PSA_RANGE_PREFIX_LENGTH="${PSA_RANGE_PREFIX_LENGTH:-16}"
# Empty means Google chooses the block. That is the safe default: a hardcoded CIDR collides the day
# this runs in an organisation whose VPC already uses it, and the collision surfaces as a peering
# that cannot be established rather than as a clear message.
PSA_RANGE_ADDRESS="${PSA_RANGE_ADDRESS:-}"

CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-${DEPLOYMENT_ID}-pg}"
CLOUDSQL_DATABASE_VERSION="${CLOUDSQL_DATABASE_VERSION:-POSTGRES_16}"
CLOUDSQL_EDITION="${CLOUDSQL_EDITION:-enterprise}"
# THE SIZE IS THE ENVIRONMENT'S DECISION, not this script's - dev runs a shared-core instance and
# prod does not. Both the tier and the availability type are read from the deployment env so the
# two environments differ in configuration rather than in code.
CLOUDSQL_TIER="${CLOUDSQL_TIER:-db-g1-small}"
CLOUDSQL_AVAILABILITY_TYPE="${CLOUDSQL_AVAILABILITY_TYPE:-zonal}"
CLOUDSQL_STORAGE_SIZE_GB="${CLOUDSQL_STORAGE_SIZE_GB:-20}"
CLOUDSQL_BACKUP_START_TIME="${CLOUDSQL_BACKUP_START_TIME:-03:00}"
CLOUDSQL_RETAINED_BACKUPS="${CLOUDSQL_RETAINED_BACKUPS:-14}"
CLOUDSQL_RETAINED_TRANSACTION_LOG_DAYS="${CLOUDSQL_RETAINED_TRANSACTION_LOG_DAYS:-7}"

# The database, user and port are the SAME names run-migrations.sh and the Cloud Run manifests
# already read. The data tier creates exactly what the rest of the deployment expects to find;
# a second set of names here would be a second source of truth.
DB_NAME="${MIGRATE_DB_NAME:-meshpipeline}"
DB_USER="${MIGRATE_DB_USER:-meshpipeline}"
DB_PORT="${MIGRATE_DB_PORT:-5432}"
# The same default create-secrets.sh uses, so the container this fills and the container that one
# creates and grants are the same container. Two defaults would mean two secrets, one of them empty.
DB_PASSWORD_SECRET="${POSTGRES_PASSWORD_SECRET:-postgres-password}"

REDIS_INSTANCE="${REDIS_INSTANCE:-${DEPLOYMENT_ID}-redis}"
REDIS_SIZE_GB="${REDIS_SIZE_GB:-1}"
# Redis is asymmetric about case, in both directions: `instances create --redis-version` accepts
# only the lowercase spelling, while `instances describe` reports the uppercase one. The default
# here is the spelling a describe returns, so a value copied out of gcloud round-trips, and the
# create call lowercases it at the point of use.
REDIS_VERSION="${REDIS_VERSION:-REDIS_7_0}"
REDIS_DB_INDEX="${REDIS_DB_INDEX:-0}"
# BASIC/STANDARD_HA are the API's spelling and basic/standard are the flag's. Both are accepted
# because an operator reading `gcloud redis instances describe` sees the first and would otherwise
# copy a value this script rejects.
case "$(printf '%s' "${REDIS_TIER:-basic}" | tr '[:upper:]' '[:lower:]')" in
  basic)                REDIS_TIER_FLAG=basic ;;
  standard|standard_ha) REDIS_TIER_FLAG=standard ;;
  *) die "REDIS_TIER='${REDIS_TIER:-}' is not a Memorystore tier - use BASIC (no replica) or STANDARD_HA (replicated)" ;;
esac

info "provisioning the data tier on ${VPC_NETWORK} (${GCP_PROJECT_ID}/${GCP_REGION})"

# The APIs this tier needs, enabled here rather than in enable-apis.sh: that list is what EVERY
# deployment needs, and the mesh-only deployment this tooling started as has neither a database nor
# a broker. Enabling an already-enabled service is a no-op.
gc services enable sqladmin.googleapis.com redis.googleapis.com \
    servicenetworking.googleapis.com compute.googleapis.com \
  || warn "could not enable the Cloud SQL / Memorystore / Service Networking APIs - if they are
       already on, the steps below still work; if they are not, they fail and this is the command:
         gcloud services enable sqladmin.googleapis.com redis.googleapis.com \\
           servicenetworking.googleapis.com compute.googleapis.com --project ${GCP_PROJECT_ID}"

# ---------------------------------------------------------------------------------------------
# 1) the private-services range and the peering that carries it.
#
# Both Cloud SQL and Memorystore below are addressed out of this one allocation. Without it, a
# private-IP instance cannot be created at all - and the failure names neither the range nor the
# peering, so it is established first and reported as its own resource.
if gc compute addresses describe "${PSA_RANGE_NAME}" --global >/dev/null 2>&1; then
  RANGE_DISPOSITION=reused
  RANGE_CIDR="$(gc compute addresses describe "${PSA_RANGE_NAME}" --global \
    --format='value(address,prefixLength)' | tr '\t' '/')"
  log "peering range   ${PSA_RANGE_NAME}  ${RANGE_CIDR}  (reused - untouched)"
else
  RANGE_DISPOSITION=created
  info "Reserving the private-services range ${PSA_RANGE_NAME} (/${PSA_RANGE_PREFIX_LENGTH})"
  addr_args=(--global --purpose=VPC_PEERING --network="${VPC_NETWORK}"
             --prefix-length="${PSA_RANGE_PREFIX_LENGTH}"
             --description="Private services access for ${DEPLOYMENT_ID} (Cloud SQL, Memorystore)")
  if [ -n "${PSA_RANGE_ADDRESS}" ]; then addr_args+=(--addresses "${PSA_RANGE_ADDRESS}"); fi
  gc compute addresses create "${PSA_RANGE_NAME}" "${addr_args[@]}"
  RANGE_CIDR="$(gc compute addresses describe "${PSA_RANGE_NAME}" --global \
    --format='value(address,prefixLength)' | tr '\t' '/')"
  log "peering range   ${PSA_RANGE_NAME}  ${RANGE_CIDR}  (created)"
fi

# `vpc-peerings connect` on a network that already has the connection is an error, not a no-op, so
# the peering is probed first. The probe asks whether ANY servicenetworking connection exists rather
# than which ranges it carries: a connection is per network-and-service, and connecting a second
# time to add a range is `update`, which is not this script's to do.
#
# TWO PROBES, BECAUSE A REFUSED READ IS NOT AN ABSENT RESOURCE. `services vpc-peerings list` needs
# servicenetworking.services.get, which the federated deploy identity does not hold; discarding its
# stderr turned "you may not ask" into "there is none", and the run went on to `connect` a peering
# that had been ACTIVE on hexera-dev since 2026-08-30. That failed with `Permission denied to add
# peering`, which names neither the real problem nor the account. So the second probe reads the
# peering from the NETWORK side - `compute.networks.get`, which the deployer does hold as part of
# compute.instanceAdmin.v1 - and only a network that genuinely carries no servicenetworking peering
# reaches the connect below.
peerings_seen=""
if peerings_seen="$(gc services vpc-peerings list --network="${VPC_NETWORK}" \
     --service=servicenetworking.googleapis.com --format='value(peering)' 2>/dev/null)"; then
  :
else
  peerings_seen="$(gc compute networks describe "${VPC_NETWORK}" \
    --format='value(peerings[].name)' 2>/dev/null | tr ';,' '\n' \
    | grep -i servicenetworking || true)"
fi
if printf '%s' "${peerings_seen}" | grep -q .; then
  PEERING_DISPOSITION=reused
  log "peering         servicenetworking <-> ${VPC_NETWORK}  (reused - untouched)"
else
  PEERING_DISPOSITION=created
  info "Connecting servicenetworking to ${VPC_NETWORK} over ${PSA_RANGE_NAME}"
  gc services vpc-peerings connect --network="${VPC_NETWORK}" \
    --service=servicenetworking.googleapis.com --ranges="${PSA_RANGE_NAME}"
  log "peering         servicenetworking <-> ${VPC_NETWORK}  (created)"
fi

# ---------------------------------------------------------------------------------------------
# 2) the Cloud SQL instance.
if gc sql instances describe "${CLOUDSQL_INSTANCE}" >/dev/null 2>&1; then
  SQL_DISPOSITION=reused
  # A reused instance is not reconfigured, but it IS held to the same standard, because the whole
  # point of the fixed shape is that an operator can tell whether they have it. Each property is
  # read once and reported with the patch that would correct it.
  # separator="|" rather than the default tab, and IFS to match. An unset boolean renders as an
  # EMPTY field, and bash collapses runs of whitespace delimiters - so with tabs, an instance with
  # PITR unset silently shifts every later field left and the check reports the wrong property.
  IFS='|' read -r HAS_BACKUP HAS_PITR HAS_DELPROT HAS_PUBLIC_IP SQL_SSL_MODE SQL_TIER_NOW <<<"$(
    gc sql instances describe "${CLOUDSQL_INSTANCE}" --format='value[separator="|"](
      settings.backupConfiguration.enabled,
      settings.backupConfiguration.pointInTimeRecoveryEnabled,
      settings.deletionProtectionEnabled,
      settings.ipConfiguration.ipv4Enabled,
      settings.ipConfiguration.sslMode,
      settings.tier)')"
  log "cloud sql       ${CLOUDSQL_INSTANCE}  ${SQL_TIER_NOW}  (reused - untouched)"
  sql_gaps=()
  [ "${HAS_BACKUP}" = "True" ]     || sql_gaps+=("backups are DISABLED - there is no restore point")
  [ "${HAS_PITR}" = "True" ]       || sql_gaps+=("point-in-time recovery is OFF - recovery is only to a backup boundary")
  [ "${HAS_DELPROT}" = "True" ]    || sql_gaps+=("deletion protection is OFF - one delete destroys the data")
  [ "${HAS_PUBLIC_IP}" != "True" ] || sql_gaps+=("a PUBLIC IP is enabled - the instance is reachable off the VPC")
  [ "${SQL_SSL_MODE}" = "ENCRYPTED_ONLY" ] || [ "${SQL_SSL_MODE}" = "TRUSTED_CLIENT_CERTIFICATE_REQUIRED" ] \
    || sql_gaps+=("sslMode=${SQL_SSL_MODE:-unset} - unencrypted connections are accepted")
  if [ ${#sql_gaps[@]} -gt 0 ]; then
    warn "the reused instance ${CLOUDSQL_INSTANCE} does not have the shape this script creates:"
    for gap in "${sql_gaps[@]}"; do printf '       - %s\n' "${gap}" >&2; done
    warn "it was NOT patched - changing durability settings acts on the instance this deploy is
       about to migrate. Apply it deliberately, when a restart is acceptable:
         gcloud sql instances patch ${CLOUDSQL_INSTANCE} --project ${GCP_PROJECT_ID} \\
           --backup --backup-start-time=${CLOUDSQL_BACKUP_START_TIME} \\
           --retained-backups-count=${CLOUDSQL_RETAINED_BACKUPS} \\
           --retained-transaction-log-days=${CLOUDSQL_RETAINED_TRANSACTION_LOG_DAYS} \\
           --enable-point-in-time-recovery --deletion-protection \\
           --no-assign-ip --ssl-mode=ENCRYPTED_ONLY"
  fi
else
  SQL_DISPOSITION=created
  info "Creating Cloud SQL ${CLOUDSQL_INSTANCE} (${CLOUDSQL_DATABASE_VERSION}, ${CLOUDSQL_TIER}, private IP only)"
  log "this takes several minutes - the instance is built before it answers"
  # --no-assign-ip is the flag the live inventory's worst finding is about: no public address means
  # the authorized-networks list cannot be forgotten, because there is no surface for it to guard.
  # connector enforcement is deliberately left NOT_REQUIRED: every consumer here dials the private
  # address directly over the VPC, and REQUIRED would refuse exactly those connections.
  gc sql instances create "${CLOUDSQL_INSTANCE}" \
    --database-version="${CLOUDSQL_DATABASE_VERSION}" \
    --edition="${CLOUDSQL_EDITION}" \
    --tier="${CLOUDSQL_TIER}" \
    --region="${GCP_REGION}" \
    --availability-type="${CLOUDSQL_AVAILABILITY_TYPE}" \
    --storage-type=SSD \
    --storage-size="${CLOUDSQL_STORAGE_SIZE_GB}" \
    --storage-auto-increase \
    --network="${NETWORK_URI}" \
    --no-assign-ip \
    --ssl-mode=ENCRYPTED_ONLY \
    --backup \
    --backup-start-time="${CLOUDSQL_BACKUP_START_TIME}" \
    --retained-backups-count="${CLOUDSQL_RETAINED_BACKUPS}" \
    --retained-transaction-log-days="${CLOUDSQL_RETAINED_TRANSACTION_LOG_DAYS}" \
    --enable-point-in-time-recovery \
    --deletion-protection \
    --maintenance-window-day=SUN \
    --maintenance-window-hour=4
  log "cloud sql       ${CLOUDSQL_INSTANCE}  ${CLOUDSQL_TIER}  (created - private IP, backups ${CLOUDSQL_RETAINED_BACKUPS}d, PITR, deletion protection)"
fi

# No --root-password is given on create, so the built-in `postgres` superuser has no password and
# cannot authenticate. The application never uses it; leaving it unset means there is no second
# credential to store, rotate or leak.
SQL_PRIVATE_IP="$(gc sql instances describe "${CLOUDSQL_INSTANCE}" --flatten='ipAddresses[]' \
  --format='value(ipAddresses.type,ipAddresses.ipAddress)' | awk '$1=="PRIVATE"{print $2; exit}')"
[ -n "${SQL_PRIVATE_IP}" ] || die "${CLOUDSQL_INSTANCE} has no PRIVATE address.
   Every consumer of this database reaches it over the VPC, so an instance without one cannot be
   used by this deployment. Give it one (this does not remove a public address):
     gcloud sql instances patch ${CLOUDSQL_INSTANCE} --project ${GCP_PROJECT_ID} --network ${NETWORK_URI}"
SQL_CONNECTION_NAME="$(gc sql instances describe "${CLOUDSQL_INSTANCE}" --format='value(connectionName)')"

# ---------------------------------------------------------------------------------------------
# 3) the application database.
if gc sql databases describe "${DB_NAME}" --instance="${CLOUDSQL_INSTANCE}" >/dev/null 2>&1; then
  DB_DISPOSITION=reused
  log "database        ${DB_NAME}  (reused - untouched)"
else
  DB_DISPOSITION=created
  info "Creating database ${DB_NAME} on ${CLOUDSQL_INSTANCE}"
  gc sql databases create "${DB_NAME}" --instance="${CLOUDSQL_INSTANCE}"
  log "database        ${DB_NAME}  (created - empty; run-migrations.sh brings it to head)"
fi

# ---------------------------------------------------------------------------------------------
# 4) the application user and its password.
#
# The two facts are decided together because they are one credential: an account without the
# password its runtimes hold is no more usable than a password with no account.
#
#   secret has a version + user exists  -> reuse both. NOTHING is rotated.
#   secret has a version + no user      -> create the user WITH the stored password. The runtimes
#                                          already hold it; minting a new one would lock them out.
#   no version + user exists            -> the live password is unrecoverable (Cloud SQL cannot read
#                                          one back), so it is replaced and that is announced.
#   neither                             -> the first run: generate, store, create.
DB_PASSWORD=""
SECRET_HAS_VERSION=0
if secret_exists "${DB_PASSWORD_SECRET}" && [ -n "$(gc secrets versions list "${DB_PASSWORD_SECRET}" \
     --filter='state:ENABLED' --limit=1 --format='value(name)' 2>/dev/null)" ]; then
  SECRET_HAS_VERSION=1
fi
DB_USER_EXISTS=0
if gc sql users list --instance="${CLOUDSQL_INSTANCE}" --format='value(name)' 2>/dev/null \
     | grep -qx "${DB_USER}"; then
  DB_USER_EXISTS=1
fi

# TRACING OFF for the block that handles the value, and it must stay off: under `bash -x` every
# expansion below is echoed, which would republish into a deploy log exactly what Secret Manager
# exists to keep out of one. deploy/gcp/worker/startup.sh guards the same way for the same reason.
_XTRACE_WAS_ON=0; case "$-" in *x*) _XTRACE_WAS_ON=1 ;; esac
set +x

if [ "${SECRET_HAS_VERSION}" = "1" ] && [ "${DB_USER_EXISTS}" = "1" ]; then
  USER_DISPOSITION=reused
  SECRET_DISPOSITION=reused
elif [ "${SECRET_HAS_VERSION}" = "1" ]; then
  USER_DISPOSITION=created
  SECRET_DISPOSITION=reused
  DB_PASSWORD="$(gc secrets versions access latest --secret="${DB_PASSWORD_SECRET}" 2>/dev/null)" \
    || die "secret '${DB_PASSWORD_SECRET}' holds a version this identity may not read, so the
   database user cannot be created with the password the runtimes already hold. Grant yourself the
   accessor role, or run this as an owner:
     gcloud secrets add-iam-policy-binding ${DB_PASSWORD_SECRET} --project ${GCP_PROJECT_ID} \\
       --member user:\$(gcloud config get-value account) --role roles/secretmanager.secretAccessor"
else
  USER_DISPOSITION="$([ "${DB_USER_EXISTS}" = "1" ] && printf 'reused' || printf 'created')"
  SECRET_DISPOSITION=created
  # Alphanumeric by construction, and 32 characters of it - about 190 bits. The alphabet excludes
  # punctuation deliberately: this value is carried in a libpq DSN and in a YAML document on stdin,
  # and a character that means something to either of those turns a credential into a parse error
  # that nobody can debug without printing the credential.
  DB_PASSWORD="$(python3 -c 'import secrets,string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(32)), end="")')"
  if secret_exists "${DB_PASSWORD_SECRET}"; then
    printf '%s' "${DB_PASSWORD}" | gc secrets versions add "${DB_PASSWORD_SECRET}" --data-file=- >/dev/null
  else
    printf '%s' "${DB_PASSWORD}" | gc secrets create "${DB_PASSWORD_SECRET}" \
      --replication-policy=automatic --data-file=- \
      --labels="app=hexera,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" >/dev/null
  fi
fi

# The value reaches gcloud through `--flags-file=-`, which gcloud reads from STDIN and expands into
# its own argument list in-process. `--password=<value>` therefore never exists in this process's
# argv, so it is absent from `ps`, from the shell's history, and from any log that records a command
# line. `--prompt-for-password` is NOT the alternative it appears to be: lib.sh exports
# CLOUDSDK_CORE_DISABLE_PROMPTS=1, under which gcloud's password prompt returns nothing at all.
if [ "${USER_DISPOSITION}" = "created" ] && [ "${DB_USER_EXISTS}" = "0" ]; then
  info "Creating database user ${DB_USER} (password generated, stored in secret/${DB_PASSWORD_SECRET})"
  printf -- "--password: '%s'\n" "${DB_PASSWORD}" \
    | gc sql users create "${DB_USER}" --instance="${CLOUDSQL_INSTANCE}" --flags-file=- >/dev/null
elif [ -n "${DB_PASSWORD}" ]; then
  warn "secret '${DB_PASSWORD_SECRET}' held no enabled version, but the user ${DB_USER} exists.
       Cloud SQL cannot read an existing password back, so the only recoverable action is to REPLACE
       it. A new password has been generated and stored. Every runtime still holding the old one -
       the API service, the worker fleet - must be restarted to pick it up."
  printf -- "--password: '%s'\n" "${DB_PASSWORD}" \
    | gc sql users set-password "${DB_USER}" --instance="${CLOUDSQL_INSTANCE}" --flags-file=- >/dev/null
fi

unset DB_PASSWORD
if [ "${_XTRACE_WAS_ON}" = "1" ]; then set -x; fi

log "database user   ${DB_USER}  (${USER_DISPOSITION})"
log "password secret ${DB_PASSWORD_SECRET}  (${SECRET_DISPOSITION} - name only; the value is never printed or written to the env)"

# ---------------------------------------------------------------------------------------------
# 5) Memorystore Redis.
#
# PRIVATE_SERVICE_ACCESS, not the DIRECT_PEERING hexera-dev was built with: direct peering creates a
# second, instance-owned peering (`redis-peer-…` in the live inventory) beside the servicenetworking
# one, so the project ends up with two peerings and two reserved blocks for one private network.
# Private service access reuses the allocation established above.
#
# --reserved-ip-range is deliberately not passed: with private service access, servicenetworking
# allocates out of whichever range the connection already carries. Naming one here would fail the
# day a project's connection carries a different range than this script assumes.
if gc redis instances describe "${REDIS_INSTANCE}" --region "${GCP_REGION}" >/dev/null 2>&1; then
  REDIS_DISPOSITION=reused
  IFS='|' read -r REDIS_TIER_NOW REDIS_CONNECT_MODE <<<"$(gc redis instances describe "${REDIS_INSTANCE}" \
    --region "${GCP_REGION}" --format='value[separator="|"](tier,connectMode)')"
  log "redis           ${REDIS_INSTANCE}  ${REDIS_TIER_NOW}  (reused - untouched)"
  [ "${REDIS_CONNECT_MODE}" = "PRIVATE_SERVICE_ACCESS" ] \
    || warn "${REDIS_INSTANCE} uses connectMode=${REDIS_CONNECT_MODE}, not PRIVATE_SERVICE_ACCESS.
       It works, but it holds a peering and a reserved block of its own beside the servicenetworking
       one. The connect mode cannot be changed in place - it is a property of a new instance."
else
  REDIS_DISPOSITION=created
  info "Creating Memorystore ${REDIS_INSTANCE} (${REDIS_TIER_FLAG}, ${REDIS_SIZE_GB}GB, private service access)"
  gc redis instances create "${REDIS_INSTANCE}" \
    --region "${GCP_REGION}" \
    --network "${NETWORK_URI}" \
    --connect-mode=private-service-access \
    --tier "${REDIS_TIER_FLAG}" \
    --size "${REDIS_SIZE_GB}" \
    --redis-version "$(printf '%s' "${REDIS_VERSION}" | tr '[:upper:]' '[:lower:]')" \
    --display-name "Hexera broker (${DEPLOYMENT_ID})" \
    --labels "app=hexera,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
  log "redis           ${REDIS_INSTANCE}  ${REDIS_TIER_FLAG}  (created)"
fi

IFS='|' read -r REDIS_HOST REDIS_PORT <<<"$(gc redis instances describe "${REDIS_INSTANCE}" \
  --region "${GCP_REGION}" --format='value[separator="|"](host,port)')"
[ -n "${REDIS_HOST}" ] || die "${REDIS_INSTANCE} reports no host address - it is not ready yet.
   Watch it reach READY, then rerun this script:
     gcloud redis instances describe ${REDIS_INSTANCE} --region ${GCP_REGION} --project ${GCP_PROJECT_ID}"
REDIS_URL="redis://${REDIS_HOST}:${REDIS_PORT}/${REDIS_DB_INDEX}"

# ---------------------------------------------------------------------------------------------
# 6) hand the resolved addresses to the stages that follow.
#
# These are ADDRESSES and NAMES, never credentials: POSTGRES_PASSWORD_SECRET is a Secret Manager
# container name and REDIS_URL carries no AUTH string, because Memorystore AUTH is not enabled here
# (it would be a second credential, and the broker is only reachable from inside the VPC).
# devtools/quality/check_deploy_secrets.py enforces that rule over this exact file.
ENV_TARGET="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
python3 - "${ENV_TARGET}" \
  "CLOUDSQL_INSTANCE=${CLOUDSQL_INSTANCE}" \
  "CLOUDSQL_CONNECTION_NAME=${SQL_CONNECTION_NAME}" \
  "CLOUDSQL_TIER=${CLOUDSQL_TIER}" \
  "MIGRATE_DB_HOST=${SQL_PRIVATE_IP}" \
  "MIGRATE_DB_PORT=${DB_PORT}" \
  "MIGRATE_DB_NAME=${DB_NAME}" \
  "MIGRATE_DB_USER=${DB_USER}" \
  "POSTGRES_PASSWORD_SECRET=${DB_PASSWORD_SECRET}" \
  "REDIS_INSTANCE=${REDIS_INSTANCE}" \
  "REDIS_TIER=${REDIS_TIER_FLAG}" \
  "REDIS_URL=${REDIS_URL}" <<'PY'
import pathlib, re, sys
path, pairs = sys.argv[1], sys.argv[2:]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""
for pair in pairs:
    key, _, value = pair.partition("=")
    # A lambda replacement, not a template: re.sub reads backslashes and \1 in the REPLACEMENT
    # string, and a value that happened to contain one would be silently rewritten.
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", lambda _m: f"{key}={value}", text, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + f"{key}={value}\n"
p.write_text(text)
PY

printf '\n'
info "data tier ready"
log "peering range   ${PSA_RANGE_NAME} ${RANGE_CIDR} (${RANGE_DISPOSITION}), peering (${PEERING_DISPOSITION})"
log "cloud sql       ${CLOUDSQL_INSTANCE} (${SQL_DISPOSITION})  ${SQL_CONNECTION_NAME}"
log "  private addr  ${SQL_PRIVATE_IP}:${DB_PORT}   -> MIGRATE_DB_HOST"
log "  database      ${DB_NAME} (${DB_DISPOSITION}), user ${DB_USER} (${USER_DISPOSITION})"
log "  password      secret/${DB_PASSWORD_SECRET} (${SECRET_DISPOSITION})"
log "redis           ${REDIS_INSTANCE} (${REDIS_DISPOSITION})  ${REDIS_URL}   -> REDIS_URL"
log "written to      ${ENV_TARGET}"
log "done"
