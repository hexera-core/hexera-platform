#!/usr/bin/env bash
# Responsibility: Provision the artifacts bucket and the ONE interoperability credential the application reads it with.
# Owns: the bucket's access posture, the object-store identity's two bucket-scoped grants, and the HMAC key's lifetime.
# Boundaries: it creates or validates; it never deletes a bucket, never deactivates a key, and never mints a second one.

# Provision OBJECT STORAGE for the application - the artifacts bucket, and the HMAC credential its
# object-store adapter speaks to that bucket with.
#
#   bash deploy/gcp/scripts/create-object-storage.sh
#
# WHY AN HMAC KEY AT ALL, ON GOOGLE CLOUD STORAGE. src/meshpipeline/adapters/object_storage/minio.py
# is the deployed adapter and it is an S3 client (minio-py): it signs SigV4 with an access-id/secret
# pair against a host it is given. Google Cloud Storage serves exactly that protocol on its
# S3-INTEROPERABILITY endpoint, storage.googleapis.com, and an HMAC key for a service account is the
# credential it accepts. The alternative - rewriting the adapter onto the GCS client - is a change to
# the application, not to its deployment, and this is the deployment.
#
# MINIO_SECURE MUST BE TRUE. The setting defaults to false because the local compose stack serves
# plain HTTP on the same host; the interoperability endpoint serves TLS only and refuses a
# plain-HTTP request. It is written back below rather than left to the operator, because the failure
# it causes is a connection reset with no mention of TLS.
#
# THE KEY IS MINTED ONCE AND THEN REUSED. GCS returns an HMAC secret only at creation, so a script
# that created one per deploy would leave a trail of live credentials nobody can account for, and
# would exhaust the five-active-keys-per-account limit in five deploys. An existing ACTIVE key whose
# access id this deployment already records, paired with a secret that still holds a version, is
# reused untouched.
#
# NO SECRET VALUE IS PRINTED OR WRITTEN TO THE DEPLOYMENT ENV. The access id is not a credential on
# its own and is recorded as MINIO_ACCESS_KEY; the secret goes straight from gcloud's stdout into
# Secret Manager, and only the CONTAINER NAME is written down.
#
# INPUTS   the deployment env (GCP_ARTIFACTS_BUCKET, OBJECT_STORE_SERVICE_ACCOUNT, MINIO_*)
# MUTATES  the bucket, the object-store identity, two bucket-scoped IAM bindings, one HMAC key, one secret
# OUTPUT   the MINIO_* settings the application needs, written back into the deployment env
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_PROJECT_NUMBER GCP_REGION DEPLOYMENT_ID

# The project number is in the name for the same reason the exchange bucket carries it: bucket names
# are globally unique, so a name without it is a name some other organisation has already taken.
ARTIFACTS_BUCKET="${GCP_ARTIFACTS_BUCKET:-${DEPLOYMENT_ID}-artifacts-${GCP_PROJECT_NUMBER}}"
# THE IDENTITY THE KEY BELONGS TO. An HMAC key is owned by one service account, and every process
# that presents that key authenticates AS that account - so this is not a list. It defaults to the
# API identity (create-api-service.sh owns the same default) because the API is the deployment's
# primary artifact writer. The worker fleet reads the same MINIO_ACCESS_KEY and the same
# MINIO_SECRET_KEY container, so it authenticates as this account too and needs no binding of its
# own; a grant to WORKER_SERVICE_ACCOUNT here would authorise a path nothing takes.
# It is NOT the mesh identity: the mesh job is compute-only, holds no secret, and trades through
# the exchange bucket - a property create-service-accounts.sh and apply-iam.sh exist to preserve.
OBJECT_STORE_SA="${OBJECT_STORE_SERVICE_ACCOUNT:-${API_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-api}}"
OBJECT_STORE_SA_EMAIL="${OBJECT_STORE_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
# CREATED HERE IF ABSENT, because this stage runs BEFORE the API stage that otherwise owns this
# identity - the object store has to exist before a service that reads it, and granting a bucket
# role to an account that does not exist yet fails with "Service account ... does not exist" on a
# first deploy. Creating it is safe and idempotent: the API stage reconciles the same name rather
# than making a second one, and an account with no bindings can do nothing.
if ! sa_exists "${OBJECT_STORE_SA_EMAIL}"; then
  info "Creating ${OBJECT_STORE_SA_EMAIL} (the identity the object-store key belongs to)"
  gc iam service-accounts create "${OBJECT_STORE_SA}" \
    --display-name "Hexera ${DEPLOYMENT_ID} API" \
    --description "Runtime identity for the ${DEPLOYMENT_ID} API service; owns the object-store HMAC key" \
    >/dev/null 2>&1 || true
  # IAM reports a just-created account as missing for a few seconds. Wait for it to be readable
  # rather than letting the first binding below fail on a propagation delay.
  for _ in 1 2 3 4 5 6; do sa_exists "${OBJECT_STORE_SA_EMAIL}" && break; sleep 5; done
  sa_exists "${OBJECT_STORE_SA_EMAIL}" \
    || die "could not create or see ${OBJECT_STORE_SA_EMAIL}. Creating a service account needs
  iam.serviceAccounts.create, which a federated deploy identity does not hold - run
  'make env-bootstrap' as an owner once per project, or create this account by hand."
  log "identity ${OBJECT_STORE_SA_EMAIL}  (created)"
fi
# The same default create-secrets.sh uses, so the container this fills and the container that one
# creates and grants to the API and the worker are the same container.
HMAC_SECRET_NAME="${MINIO_SECRET_KEY_SECRET:-minio-secret-key}"
# The S3-interoperability endpoint, as a bare host - minio-py takes a host, not a URL. It is a
# variable because Google also serves regional interoperability endpoints, and a deployment pinned
# to one data boundary has to name it rather than the global address.
S3_ENDPOINT="${GCS_S3_ENDPOINT:-storage.googleapis.com}"

info "provisioning object storage for ${DEPLOYMENT_ID} (${GCP_PROJECT_ID}/${GCP_REGION})"

# ---------------------------------------------------------------------------------------------
# 1) the artifacts bucket.
#
# A bucket this run creates is hardened: uniform bucket-level access, so a stray object ACL cannot
# widen it, and public access prevention ENFORCED rather than the "inherited" the live inventory
# found on hexera-dev's artifacts bucket - inherited means the organisation policy decides, and a
# bucket holding user artifacts should not depend on that.
if bucket_exists "${ARTIFACTS_BUCKET}"; then
  BUCKET_DISPOSITION=reused
  IFS='|' read -r BUCKET_UBLA BUCKET_PAP BUCKET_LOCATION <<<"$(gcloud storage buckets describe \
    "gs://${ARTIFACTS_BUCKET}" --project "${GCP_PROJECT_ID}" \
    --format='value[separator="|"](uniform_bucket_level_access,public_access_prevention,location)')"
  log "artifacts bucket gs://${ARTIFACTS_BUCKET}  ${BUCKET_LOCATION}  (reused - untouched)"
  bucket_gaps=()
  [ "${BUCKET_UBLA}" = "True" ] || bucket_gaps+=("uniform bucket-level access is OFF - per-object ACLs can widen it")
  [ "${BUCKET_PAP}" = "enforced" ] || bucket_gaps+=("public access prevention is '${BUCKET_PAP:-unset}', not enforced")
  if [ ${#bucket_gaps[@]} -gt 0 ]; then
    warn "the reused bucket gs://${ARTIFACTS_BUCKET} is more open than one created here:"
    for gap in "${bucket_gaps[@]}"; do printf '       - %s\n' "${gap}" >&2; done
    warn "it was NOT changed - tightening a bucket this deployment does not own can break a reader
       nothing here knows about. Apply it deliberately:
         gcloud storage buckets update gs://${ARTIFACTS_BUCKET} --project ${GCP_PROJECT_ID} \\
           --uniform-bucket-level-access --public-access-prevention"
  fi
else
  BUCKET_DISPOSITION=created
  info "Creating artifacts bucket gs://${ARTIFACTS_BUCKET} in ${GCP_REGION}"
  gc storage buckets create "gs://${ARTIFACTS_BUCKET}" \
    --location "${GCP_REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
  gc storage buckets update "gs://${ARTIFACTS_BUCKET}" \
    --update-labels "app=hexera,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" >/dev/null 2>&1 || true
  log "artifacts bucket gs://${ARTIFACTS_BUCKET}  ${GCP_REGION}  (created - UBLA, public access prevention enforced)"
  BUCKET_LOCATION="${GCP_REGION}"
fi

# ---------------------------------------------------------------------------------------------
# 2) the object-store identity.
#
# An HMAC key belongs to a service account, so the account has to exist before the key can. It is
# created here when it is absent for the same reason run-migrations.sh and
# create-queue-depth-publisher.sh create theirs: the stage that needs an identity is the stage that
# knows what it is for. create-api-service.sh reconciles the same account under the same default, so
# whichever runs first creates it and the other finds it. It receives no project-level role - its
# whole authority is the two bucket-scoped grants below.
if sa_exists "${OBJECT_STORE_SA_EMAIL}"; then
  SA_DISPOSITION=reused
  log "object-store identity ${OBJECT_STORE_SA_EMAIL} (exists)"
else
  SA_DISPOSITION=created
  info "Creating object-store identity ${OBJECT_STORE_SA_EMAIL}"
  gc iam service-accounts create "${OBJECT_STORE_SA}" \
    --display-name "Hexera application runtime (artifacts)" \
    || die "could not create ${OBJECT_STORE_SA_EMAIL}. Creating identities needs iam.serviceAccountAdmin,
   which a DEPLOY identity is deliberately not given. Create it once, as an owner:
     gcloud iam service-accounts create ${OBJECT_STORE_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera application runtime (artifacts)'"
fi

# ---------------------------------------------------------------------------------------------
# 3) the two grants, scoped to this bucket rather than to the project.
#
# objectAdmin covers what the adapter does to OBJECTS: put, get, stat, delete, and the signed URLs
# it hands to a browser.
#
# legacyBucketReader is not decoration. `MinioStore._ensure_bucket` calls `bucket_exists`, which over
# the S3 API is a HEAD on the bucket itself - storage.buckets.get, a permission no object-level role
# carries. Without it every upload logs "bucket check failed" and the first real failure is
# indistinguishable from the noise. legacyBucketReader is the narrowest role that grants it: bucket
# metadata and nothing else.
grant_bucket() {  # member role
  gc storage buckets add-iam-policy-binding "gs://${ARTIFACTS_BUCKET}" \
    --member "$1" --role "$2" >/dev/null \
    || die "could not grant $2 on gs://${ARTIFACTS_BUCKET} to $1.
   Setting bucket IAM needs storage.admin on the bucket, which a deploy identity may not hold. Run
   it once, as an owner:
     gcloud storage buckets add-iam-policy-binding gs://${ARTIFACTS_BUCKET} \\
       --project ${GCP_PROJECT_ID} --member $1 --role $2"
  log "gs://${ARTIFACTS_BUCKET} += $2 -> $1"
}
grant_bucket "serviceAccount:${OBJECT_STORE_SA_EMAIL}" "roles/storage.objectAdmin"
grant_bucket "serviceAccount:${OBJECT_STORE_SA_EMAIL}" "roles/storage.legacyBucketReader"

# ---------------------------------------------------------------------------------------------
# 4) the HMAC key.
#
#   recorded access id is still ACTIVE + the secret holds a version  -> reuse. Nothing is minted.
#   anything else                                                    -> mint once, store the secret,
#                                                                       and record the access id.
#
# The recorded id is what makes reuse decidable. GCS will not say which secret belongs to which key,
# so a key that is active but whose id this deployment never recorded cannot be paired with a stored
# secret - and guessing produces a runtime that authenticates against nothing. That is a repair, and
# it is announced; the old key is left ACTIVE, because deleting a credential something might still be
# holding is not this script's decision.
RECORDED_ACCESS_ID="${MINIO_ACCESS_KEY:-}"
ACTIVE_IDS="$(gc storage hmac list --service-account="${OBJECT_STORE_SA_EMAIL}" \
  --filter='state=ACTIVE' --format='value(accessId)' 2>/dev/null || true)"
ACTIVE_COUNT="$(printf '%s' "${ACTIVE_IDS}" | grep -c . || true)"

SECRET_HAS_VERSION=0
if secret_exists "${HMAC_SECRET_NAME}" && [ -n "$(gc secrets versions list "${HMAC_SECRET_NAME}" \
     --filter='state:ENABLED' --limit=1 --format='value(name)' 2>/dev/null)" ]; then
  SECRET_HAS_VERSION=1
fi

RECORDED_IS_ACTIVE=0
if [ -n "${RECORDED_ACCESS_ID}" ] && printf '%s\n' "${ACTIVE_IDS}" | grep -Fqx "${RECORDED_ACCESS_ID}"; then
  RECORDED_IS_ACTIVE=1
fi

# TRACING OFF for the block that handles the value, and it must stay off: under `bash -x` every
# expansion is echoed, which would republish into a deploy log exactly what Secret Manager exists to
# keep out of one. deploy/gcp/worker/startup.sh guards the same way for the same reason.
_XTRACE_WAS_ON=0; case "$-" in *x*) _XTRACE_WAS_ON=1 ;; esac
set +x

if [ "${RECORDED_IS_ACTIVE}" = "1" ] && [ "${SECRET_HAS_VERSION}" = "1" ]; then
  HMAC_DISPOSITION=reused
  ACCESS_ID="${RECORDED_ACCESS_ID}"
  SECRET_DISPOSITION=reused
else
  HMAC_DISPOSITION=created
  SECRET_DISPOSITION=created
  if [ "${ACTIVE_COUNT}" -ge 5 ]; then
    die "${OBJECT_STORE_SA_EMAIL} already has ${ACTIVE_COUNT} ACTIVE HMAC keys, which is the per-account
   limit, and none of them is paired with a secret this deployment can use. This script never
   deletes a key. Retire the ones nothing holds, as an owner:
     gcloud storage hmac list --service-account=${OBJECT_STORE_SA_EMAIL} --project ${GCP_PROJECT_ID}
     gcloud storage hmac update <ACCESS_ID> --deactivate --project ${GCP_PROJECT_ID}
     gcloud storage hmac delete <ACCESS_ID> --project ${GCP_PROJECT_ID}"
  fi
  if [ "${SECRET_HAS_VERSION}" = "1" ]; then
    warn "secret '${HMAC_SECRET_NAME}' holds a version, but no ACTIVE HMAC key matches the recorded
       MINIO_ACCESS_KEY='${RECORDED_ACCESS_ID:-<unset>}'. GCS never reveals which secret belongs to
       which key, so the stored one cannot be paired with a live key. A new key has been minted and
       added as a new version. Any runtime still holding the old pair must be restarted."
  fi
  info "Minting an HMAC key for ${OBJECT_STORE_SA_EMAIL} (the secret is returned once, and only once)"
  # The secret is captured, never displayed: command substitution keeps gcloud's stdout out of the
  # terminal, and it goes straight into Secret Manager on stdin - never as an argument.
  HMAC_OUT="$(gc storage hmac create "${OBJECT_STORE_SA_EMAIL}" \
    --format='value[separator="|"](metadata.accessId,secret)')" \
    || die "could not mint an HMAC key for ${OBJECT_STORE_SA_EMAIL}. This needs storage.hmacKeyAdmin on
   the project, which a deploy identity is not given. Run it once, as an owner, and store the secret:
     gcloud storage hmac create ${OBJECT_STORE_SA_EMAIL} --project ${GCP_PROJECT_ID}"
  # The separator is checked before the split, because without one `%%|*` and `#*|` both return the
  # WHOLE string - so a one-field response would pass a non-empty test twice and be recorded as a
  # key whose access id is its own secret.
  case "${HMAC_OUT}" in
    *"|"*) ACCESS_ID="${HMAC_OUT%%|*}"; HMAC_SECRET="${HMAC_OUT#*|}" ;;
    *) die "gcloud returned an HMAC key that is not an access id and a secret - refusing to record half a credential" ;;
  esac
  unset HMAC_OUT
  [ -n "${ACCESS_ID}" ] && [ -n "${HMAC_SECRET}" ] \
    || die "gcloud returned an HMAC key with an empty access id or an empty secret - refusing to record half a credential"
  if secret_exists "${HMAC_SECRET_NAME}"; then
    printf '%s' "${HMAC_SECRET}" | gc secrets versions add "${HMAC_SECRET_NAME}" --data-file=- >/dev/null
  else
    printf '%s' "${HMAC_SECRET}" | gc secrets create "${HMAC_SECRET_NAME}" \
      --replication-policy=automatic --data-file=- \
      --labels="app=hexera,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" >/dev/null
  fi
  unset HMAC_SECRET
fi

if [ "${_XTRACE_WAS_ON}" = "1" ]; then set -x; fi

log "hmac key        ${ACCESS_ID}  (${HMAC_DISPOSITION} - for ${OBJECT_STORE_SA_EMAIL})"
log "hmac secret     ${HMAC_SECRET_NAME}  (${SECRET_DISPOSITION} - name only; the value is never printed or written to the env)"

# The runtime reads the secret by reference, so it needs the accessor role on this one secret -
# granted per secret, never project-wide, which is the rule item 9 of the build-out plan states.
# A DEPLOY IDENTITY MAY NOT BE ABLE TO GRANT IT: setting IAM on a secret is an owner's act. The
# grant is attempted and a failure is reported with the command that fixes it, because the verdict
# comes later - a runtime that cannot read this secret fails on its first upload.
if gc secrets add-iam-policy-binding "${HMAC_SECRET_NAME}" \
     --member "serviceAccount:${OBJECT_STORE_SA_EMAIL}" \
     --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then
  log "secret/${HMAC_SECRET_NAME} += roles/secretmanager.secretAccessor -> ${OBJECT_STORE_SA_EMAIL}"
else
  warn "could not set IAM on secret ${HMAC_SECRET_NAME} (this identity may not hold
       secretmanager.admin). If the binding is already in place the runtime still reads it; if it is
       not, the first upload fails and this is the command:
         gcloud secrets add-iam-policy-binding ${HMAC_SECRET_NAME} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${OBJECT_STORE_SA_EMAIL} --role roles/secretmanager.secretAccessor"
fi

# ---------------------------------------------------------------------------------------------
# 5) hand the settings to the stages that follow.
#
# MINIO_ACCESS_KEY is written because it is NOT a credential on its own - the settings catalogue
# marks only MINIO_SECRET_KEY secret - and because recording it is what makes the reuse decision
# above possible on the next run. MINIO_SECRET_KEY_SECRET is a Secret Manager container NAME.
# devtools/quality/check_deploy_secrets.py enforces that distinction over this exact file.
#
# MINIO_REGION is the BUCKET's location, lowercased: it is signed into the SigV4 credential scope,
# and minio-py is given it explicitly so it never issues the GetBucketLocation call it would
# otherwise need to discover it.
MINIO_REGION_VALUE="$(printf '%s' "${BUCKET_LOCATION:-${GCP_REGION}}" | tr '[:upper:]' '[:lower:]')"
ENV_TARGET="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
python3 - "${ENV_TARGET}" \
  "GCP_ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}" \
  "OBJECT_STORE_SERVICE_ACCOUNT=${OBJECT_STORE_SA}" \
  "MINIO_BUCKET=${ARTIFACTS_BUCKET}" \
  "MINIO_ENDPOINT=${S3_ENDPOINT}" \
  "MINIO_PUBLIC_ENDPOINT=${S3_ENDPOINT}" \
  "MINIO_REGION=${MINIO_REGION_VALUE}" \
  "MINIO_SECURE=true" \
  "MINIO_ACCESS_KEY=${ACCESS_ID}" \
  "MINIO_SECRET_KEY_SECRET=${HMAC_SECRET_NAME}" <<'PY'
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
info "object storage ready"
log "bucket          gs://${ARTIFACTS_BUCKET} (${BUCKET_DISPOSITION})  ${BUCKET_LOCATION:-${GCP_REGION}}"
log "identity        ${OBJECT_STORE_SA_EMAIL} (${SA_DISPOSITION})  objectAdmin + legacyBucketReader on that bucket"
log "hmac key        ${ACCESS_ID} (${HMAC_DISPOSITION})   -> MINIO_ACCESS_KEY"
log "hmac secret     secret/${HMAC_SECRET_NAME} (${SECRET_DISPOSITION})   -> MINIO_SECRET_KEY_SECRET"
log "endpoint        ${S3_ENDPOINT}  TLS required (MINIO_SECURE=true), region ${MINIO_REGION_VALUE}"
log "written to      ${ENV_TARGET}"
log "done"
