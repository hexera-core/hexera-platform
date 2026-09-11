#!/usr/bin/env bash
# Responsibility: Provision the Cloud Run admin console service - reachable only by named Google
# identities, never by the public.
# Owns: the admin runtime identity, its scaling, and the IAP + invoker policy that gates it.
# Boundaries: every image reference is digest-qualified; VPC egress and a database connection
# belong to the next sub-project, not this one.

# Provision the CLOUD RUN ADMIN SERVICE. Idempotent and RECONCILING: an existing service is
# updated in place - a new revision on the promoted digest - and one already running that digest is
# reported as reused.
#
#   bash deploy/gcp/scripts/create-admin-service.sh
#
# WHY THIS SERVICE IS THE INVERSE OF THE CONSOLE'S. The public console is allUsers-invokable
# because its sign-in page must be reachable before anyone can authenticate. The admin console has
# no public entry point: its gate is IAP, which authenticates a Google identity before the request
# ever reaches this container.
#
# IAP AND THE INVOKER POLICY ARE SEPARATE GATES. A service with IAP enabled AND an allUsers
# invoker binding is not protected - the binding admits the request regardless, and the symptom is
# silence, because the page loads and looks fine. That is why this script never grants allUsers,
# actively removes one it finds, and has no setting to turn the behaviour off. See
# docs/deployment/admin-console-access.md.
#
# THE SECRETS IT DOES HOLD, and why it holds any. IAP is this service's gate and it keeps no
# session of its own, so nothing here authenticates a USER. What it does need is credentials for
# the things it talks to on the operator's behalf: the outreach engine's Google OAuth client, its
# database role, and the model and enrichment providers. Each is a Secret Manager REFERENCE under
# the `<SETTING>_SECRET` convention; a name that is unset is skipped rather than bound empty, so a
# deployment that runs no outreach binds nothing and this loop stays empty exactly as before.
#
# VPC EGRESS AND A DATABASE CONNECTION, and why they were not here before. Everything this console
# showed until now read Google's own APIs, which are reachable from Cloud Run's default egress -
# so a VPC route and a database credential would have been an unused network path and an unused
# secret. Outreach is the first thing that queries a table, and the data tier is private-IP only,
# so both arrive with it.
#
# PRIVATE-RANGES-ONLY, matching the API tier: RFC1918 traffic goes through the VPC, everything else
# keeps taking the default route. Sending all egress through the VPC would put Google's own APIs
# behind Cloud NAT for no benefit and a per-byte charge.
#
# INPUTS   the deployment env (ADMIN_IMAGE, CLOUDRUN_ADMIN_SERVICE, ADMIN_*, GCP_PROJECT_NUMBER)
# MUTATES  the admin runtime identity, the Cloud Run service, and its invoker policy. It deletes
#          nothing except a public invoker binding it finds - see above.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

ADMIN_SERVICE="${CLOUDRUN_ADMIN_SERVICE:-}"
ADMIN_SA="${ADMIN_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-admin}"
ADMIN_SA_EMAIL="${ADMIN_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# A deployment may genuinely serve no admin console - not every deployment promotes one. That is
# a skip, and it is stated, exactly as the console and API tiers state their own.
if [ -z "${ADMIN_SERVICE}" ]; then
  info "No Cloud Run admin service configured - skipping the admin console tier"
  log "set CLOUDRUN_ADMIN_SERVICE to deploy the admin console from this deployment"
  exit 0
fi

# GCP_PROJECT_NUMBER is dereferenced unguarded below, to build the IAP service agent's address.
# Stated here - after the skip, since a deployment with no admin service need not carry it, but
# before step 1 creates anything - so a deployment missing it dies with a clear refusal rather
# than an `unbound variable` after a service account and IAM bindings already exist.
require_vars GCP_PROJECT_NUMBER

# The image is the ADMIN image, BY DIGEST. promote-release.sh wrote it from the validated release
# record; a tag is refused rather than re-resolved, because a tag can be moved between the
# validation that approved the bytes and the rollout that ships them.
require_digest_reference ADMIN_IMAGE "${ADMIN_IMAGE:-}"

ADMIN_CPU="${ADMIN_CPU:-1}"
ADMIN_MEMORY="${ADMIN_MEMORY:-512Mi}"
ADMIN_CONCURRENCY="${ADMIN_CONCURRENCY:-80}"
ADMIN_TIMEOUT_SECONDS="${ADMIN_TIMEOUT_SECONDS:-300}"
ADMIN_MIN_INSTANCES="${ADMIN_MIN_INSTANCES:-0}"
ADMIN_MAX_INSTANCES="${ADMIN_MAX_INSTANCES:-3}"
ADMIN_INGRESS="${ADMIN_INGRESS:-all}"
APP_ENV="${APP_ENV:-dev}"
ADMIN_DISPOSITION=created

info "Admin console service ${ADMIN_SERVICE} in ${GCP_REGION} (${GCP_PROJECT_ID})"
log "image (validated digest): ${ADMIN_IMAGE}"

# 1) the admin runtime identity - a named account, not the default compute service account.
# 0) THE CREDENTIALS, as references. `RUNTIME_VAR:ENV_VAR_HOLDING_THE_SECRET_NAME` - the same
#    shape the console tier and the worker startup script read theirs from. Resolved before
#    anything is mutated, so a misconfiguration is caught before an identity or a binding exists.
ADMIN_SECRET_BINDINGS=()
ADMIN_SECRET_NAMES=()
for pair in "GOOGLE_CLIENT_SECRET:GOOGLE_CLIENT_SECRET_SECRET" \
            "OUTREACH_DB_PASSWORD:OUTREACH_DB_PASSWORD_SECRET" \
            "ANTHROPIC_API_KEY:ANTHROPIC_API_KEY_SECRET" \
            "APOLLO_API_KEY:APOLLO_API_KEY_SECRET" \
            "VERIFIER_API_KEY:VERIFIER_API_KEY_SECRET"; do
  runtime_var="${pair%%:*}"
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  ADMIN_SECRET_BINDINGS+=("${runtime_var}=${secret_name}:latest")
  ADMIN_SECRET_NAMES+=("${secret_name}")
done

if sa_exists "${ADMIN_SA_EMAIL}"; then
  log "admin identity ${ADMIN_SA_EMAIL} (exists)"
else
  info "Creating admin runtime identity ${ADMIN_SA_EMAIL}"
  gc iam service-accounts create "${ADMIN_SA}" \
    --display-name "Hexera admin console service" \
    || die "could not create ${ADMIN_SA_EMAIL}. Creating identities needs
   iam.serviceAccountAdmin, which a DEPLOY identity is deliberately not given. Create it once, as
   an owner:
     gcloud iam service-accounts create ${ADMIN_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera admin console service'"
fi

# 1a) THE APIS THE CONSOLE READS. A role grants permission to call an API; it does not turn the
#     API on, and a disabled API answers PERMISSION_DENIED with a message about enablement that
#     reads exactly like a missing role. The first real deploy produced precisely that confusion:
#     the Costs page reported a permission error while every binding it named was correct, because
#     cloudbilling was simply never enabled on the project.
#
#     enable-apis.sh carries what EVERY deployment needs; these two are needed only where an admin
#     console runs, which is why they are enabled here - the same reasoning create-worker-fleet.sh
#     applies to compute.googleapis.com.
#
#     Monitoring, Compute and Cloud Run are not listed: the tiers that create those resources have
#     already enabled them, and a deployment with an admin console but no fleet still wants its
#     Costs page.
for admin_api in cloudbilling.googleapis.com billingbudgets.googleapis.com; do
  gc services enable "${admin_api}" >/dev/null 2>&1 \
    || warn "could not enable ${admin_api}. If it is already on, the Costs page works; if it is not,
       that page reports a permission error naming an API rather than a role, and this is the
       command:
         gcloud services enable ${admin_api} --project ${GCP_PROJECT_ID}"
done

# 1a2) THE KEY THAT SEALS THE MAILBOX CREDENTIAL. Outreach stores a Gmail REFRESH TOKEN, which is
#      long-lived access to the sending mailbox. On the laptop it lived in a local SQLite file;
#      hosted, it lives in a shared database whose backups, replicas and dumps all inherit it.
#      Envelope-encrypting it with KMS is what stops a database dump from being a mailbox
#      compromise.
#
#      Created only where an outreach console runs. The key is NOT rotated automatically: a
#      rotation that re-keys without re-encrypting the stored rows would lock the mailbox out, and
#      re-encryption is a migration, not a schedule.
if [ -n "${OUTREACH_KMS_KEY:-}" ]; then
  gc services enable cloudkms.googleapis.com >/dev/null 2>&1 || true
  OUTREACH_KMS_KEYRING="${OUTREACH_KMS_KEYRING:-outreach}"
  OUTREACH_KMS_KEY_ID="${OUTREACH_KMS_KEY_ID:-oauth-tokens}"

  gc kms keyrings create "${OUTREACH_KMS_KEYRING}" --location "${GCP_REGION}" >/dev/null 2>&1 \
    || log "kms keyring ${OUTREACH_KMS_KEYRING} (exists)"
  gc kms keys create "${OUTREACH_KMS_KEY_ID}" \
    --location "${GCP_REGION}" --keyring "${OUTREACH_KMS_KEYRING}" --purpose encryption >/dev/null 2>&1 \
    || log "kms key ${OUTREACH_KMS_KEY_ID} (exists)"

  #   ENCRYPTER/DECRYPTER ONLY. Not admin: the console must never be able to destroy or rotate the
  #   key that its stored tokens depend on.
  gc kms keys add-iam-policy-binding "${OUTREACH_KMS_KEY_ID}" \
    --location "${GCP_REGION}" --keyring "${OUTREACH_KMS_KEYRING}" \
    --member "serviceAccount:${ADMIN_SA_EMAIL}" \
    --role roles/cloudkms.cryptoKeyEncrypterDecrypter >/dev/null 2>&1 \
    && log "kms key ${OUTREACH_KMS_KEY_ID} += cryptoKeyEncrypterDecrypter -> ${ADMIN_SA_EMAIL}" \
    || warn "could not grant cryptoKeyEncrypterDecrypter on ${OUTREACH_KMS_KEY_ID}. The outreach
       pages refuse to store a mailbox token without it, which is the correct failure, and this is
       the command:
         gcloud kms keys add-iam-policy-binding ${OUTREACH_KMS_KEY_ID} --project ${GCP_PROJECT_ID} \\
           --location ${GCP_REGION} --keyring ${OUTREACH_KMS_KEYRING} \\
           --member serviceAccount:${ADMIN_SA_EMAIL} --role roles/cloudkms.cryptoKeyEncrypterDecrypter"
fi

# 1b) WHAT THE CONSOLE MAY READ, AND WHAT IT MAY CHANGE.
#
#     READS are four predefined viewer roles: compute for the group, its autoscaler and its
#     template; monitoring for every graph; run for revisions and current scaling; billing for the
#     account and its budgets.
#
#     WRITES are a CUSTOM ROLE, and that is deliberate. roles/compute.instanceAdmin.v1 would work
#     and would also grant disk and instance CREATION this console never performs; roles/run.admin
#     would let an IAP-gated web page deploy arbitrary revisions. The custom role carries exactly
#     the verbs the Fleet page's controls issue and nothing else.
#
#     WHAT THIS COSTS, STATED PLAINLY: after this grant, a compromised IAP session can raise the
#     fleet's ceiling to its cap, resize the group and delete workers. The cap in the console's
#     own configuration and the typed-name confirmation bound the damage; per-admin permissions do
#     not exist, so it is not bounded to a subset of admins.
#
#     A deploy identity may not hold resourcemanager.projectIamAdmin or iam.roles.create. Every
#     grant here is ATTEMPTED, a failure is reported with the command that fixes it, and the console
#     itself is the verdict - a page that cannot read its metric says so, and a control whose
#     permission is missing fails visibly rather than silently.
ADMIN_ROLE_ID="${ADMIN_CUSTOM_ROLE:-$(printf '%s' "${DEPLOYMENT_ID}" | tr -c 'a-zA-Z0-9' '_')_admin_console}"
#     THE LIST WAS VALIDATED BY EXERCISING IT, not by reading the API reference. The first attempt
#     carried `instanceGroupManagers.update` and nothing else, and every scaling change returned
#     403 asking for `.use` - a separate permission that governs acting ON the group, which
#     `.update` does not imply. `instances.delete` and `instances.reset` are here for the same
#     reason: deleting and recreating a managed instance touch the instances themselves, not only
#     the group that manages them.
ADMIN_ROLE_PERMISSIONS="compute.autoscalers.get,compute.autoscalers.update,\
compute.instanceGroupManagers.get,compute.instanceGroupManagers.update,\
compute.instanceGroupManagers.use,\
compute.instanceTemplates.get,compute.instances.get,compute.instances.list,\
compute.instances.delete,compute.instances.reset,\
compute.zoneOperations.get,compute.zones.get,\
run.services.get,run.services.update,run.revisions.get,run.revisions.list,run.operations.get"

if gc iam roles describe "${ADMIN_ROLE_ID}" >/dev/null 2>&1; then
  if gc iam roles update "${ADMIN_ROLE_ID}" \
    --permissions "${ADMIN_ROLE_PERMISSIONS}" \
    --stage GA >/dev/null 2>&1; then
    ADMIN_ROLE_DISPOSITION=updated
  else
    ADMIN_ROLE_DISPOSITION="exists, NOT updated"
    warn "could not update the custom role ${ADMIN_ROLE_ID}. If its permissions already match,
       the console's controls still work; if they do not, the control whose permission is missing
       returns PERMISSION_DENIED on the page."
  fi
else
  # The disposition is set from the RESULT. Reporting "created" for a call that failed is how a
  # summary line ends up contradicting the warning three lines above it.
  if gc iam roles create "${ADMIN_ROLE_ID}" \
    --title "Hexera admin console fleet operator" \
    --description "Read and resize the worker fleet and Cloud Run scaling from the admin console" \
    --permissions "${ADMIN_ROLE_PERMISSIONS}" \
    --stage GA >/dev/null 2>&1; then
    ADMIN_ROLE_DISPOSITION=created
  else
    ADMIN_ROLE_DISPOSITION="ABSENT - could not be created"
    warn "could not create the custom role ${ADMIN_ROLE_ID}. Creating roles needs iam.roles.create,
       which a DEPLOY identity is deliberately not given. The console's READ pages still work; its
       controls return PERMISSION_DENIED until an owner runs:
         gcloud iam roles create ${ADMIN_ROLE_ID} --project ${GCP_PROJECT_ID} \\
           --title 'Hexera admin console fleet operator' --stage GA \\
           --permissions ${ADMIN_ROLE_PERMISSIONS}"
  fi
fi

# roles/billing.viewer is deliberately ABSENT from this list. It is a billing-ACCOUNT role and the
# API refuses it on a project outright - "Role roles/billing.viewer is not supported for this
# resource" - so including it here could only ever produce a warning that never becomes a grant.
# Both Costs reads need it, and both are covered by the one manual command printed below.
for secret_name in ${ADMIN_SECRET_NAMES[@]+"${ADMIN_SECRET_NAMES[@]}"}; do
  gc secrets add-iam-policy-binding "${secret_name}" \
    --member "serviceAccount:${ADMIN_SA_EMAIL}" \
    --role roles/secretmanager.secretAccessor >/dev/null 2>&1 \
    && log "secret/${secret_name} += secretAccessor -> ${ADMIN_SA_EMAIL}" \
    || warn "could not grant secretAccessor on ${secret_name}. If the binding exists the service
       still starts; if it does not, Cloud Run refuses the revision:
         gcloud secrets add-iam-policy-binding ${secret_name} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${ADMIN_SA_EMAIL} --role roles/secretmanager.secretAccessor"
done

ADMIN_PROJECT_ROLES=(
  roles/compute.viewer
  roles/monitoring.viewer
  roles/run.viewer
  "projects/${GCP_PROJECT_ID}/roles/${ADMIN_ROLE_ID}"
)
# Only where a billing export exists. A grant for a dataset this deployment does not have is
# authority nobody asked for.
if [ -n "${BILLING_EXPORT_TABLE:-}" ]; then
  ADMIN_PROJECT_ROLES+=(roles/bigquery.jobUser)
fi

for admin_role in "${ADMIN_PROJECT_ROLES[@]}"; do
  if gc projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
       --member "serviceAccount:${ADMIN_SA_EMAIL}" \
       --role "${admin_role}" --condition None >/dev/null 2>&1; then
    log "project += ${admin_role} -> ${ADMIN_SA_EMAIL}"
  else
    warn "could not grant ${admin_role} to ${ADMIN_SA_EMAIL}. If the binding is already in place the
       console still works; if it is not, the pages that need it report the permission error:
         gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \\
           --member serviceAccount:${ADMIN_SA_EMAIL} --role ${admin_role} --condition None"
  fi
done

# The Costs page's TWO reads - which account pays for this project, and what budgets it carries -
# both need a role that exists only on the BILLING ACCOUNT, where a deploy identity has no
# authority at all. Stated as a command rather than attempted, because attempting it always fails.
log "the Costs page needs roles/billing.viewer on the billing ACCOUNT; grant it once:"
log "  gcloud billing accounts add-iam-policy-binding <BILLING_ACCOUNT_ID> \\"
log "    --member serviceAccount:${ADMIN_SA_EMAIL} --role roles/billing.viewer"

# 2) THE NON-SECRET SETTINGS. No secret bindings here at all: IAP is this service's gate and it
#    holds no session of its own, so there is nothing for Secret Manager to hand it.
#
#    THE FLEET TARGETS. The console reads the managed instance group and the Cloud Run services BY
#    NAME rather than discovering them. Discovery would mean listing every group in the project and
#    guessing which one is ours - a wider IAM grant AND a worse failure mode, because a renamed
#    group would silently show a different fleet rather than an error.
#
#    An empty value is not a hole: the console treats an absent WORKER_MIG as "this deployment runs
#    no fleet", which is exactly what create-worker-fleet.sh does with the same variable, and its
#    Fleet page renders that state rather than failing.
#
#    GCP_PROJECT_NUMBER IS LOAD-BEARING FOR WRITES. It is half of the audience IAP signs its
#    assertion for (/projects/<number>/locations/<region>/services/<service>). Without it the
#    console cannot verify WHO is making a change, and it refuses every mutation rather than falling
#    back to the plain X-Goog-Authenticated-User-Email header.
ADMIN_ENV_PAIRS=(
  "BILLING_EXPORT_TABLE=${BILLING_EXPORT_TABLE:-}"
  "CLOUDRUN_ADMIN_SERVICE=${ADMIN_SERVICE}"
  "CLOUDRUN_API_SERVICE=${CLOUDRUN_API_SERVICE:-}"
  "CLOUDRUN_CONSOLE_SERVICE=${CLOUDRUN_CONSOLE_SERVICE:-}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "ENV=${APP_ENV}"
  "GCP_PROJECT_ID=${GCP_PROJECT_ID}"
  "GCP_PROJECT_NUMBER=${GCP_PROJECT_NUMBER}"
  "GCP_REGION=${GCP_REGION}"
  "NODE_ENV=production"
  "QUEUE_NAME=${QUEUE_NAME:-simulation_jobs}"
  "WORKER_MIG=${WORKER_MIG:-}"
  "WORKER_MIG_ZONE=${WORKER_MIG_ZONE:-}"
  "OUTREACH_KMS_KEY=${OUTREACH_KMS_KEY:-}"
  "OUTREACH_DB_HOST=${OUTREACH_DB_HOST:-}"
  "OUTREACH_DB_NAME=${OUTREACH_DB_NAME:-}"
  "OUTREACH_DB_USER=${OUTREACH_DB_USER:-}"
  "GOOGLE_REDIRECT_URI=${GOOGLE_REDIRECT_URI:-}"
  "DRY_RUN=${DRY_RUN:-1}"
)
DECLARED_ENV_NAMES=()
for pair in "${ADMIN_ENV_PAIRS[@]}"; do
  case "${pair}" in
    *"|"*) die "the setting '${pair%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with. Give it a value without one." ;;
  esac
  DECLARED_ENV_NAMES+=("${pair%%=*}")
done

# 3) WHAT IS ALREADY THERE. --set-env-vars is DECLARATIVE: it replaces the container's environment
#    with exactly what this deployment states. That is what stops drift, and it is also what would
#    silently delete a setting that only ever existed on the live service. The difference is
#    computed, named, and refused unless an operator says to prune it.
ADMIN_SERVICE_EXISTS=0
if run_svc_exists "${ADMIN_SERVICE}"; then
  ADMIN_SERVICE_EXISTS=1
  live_image="$(gc run services describe "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [ "${live_image}" = "${ADMIN_IMAGE}" ]; then
    ADMIN_DISPOSITION=reused
  else
    ADMIN_DISPOSITION=rolled
  fi

  live_env_names="$(gc run services describe "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].env[].name)' 2>/dev/null | tr ';' '\n' || true)"
  undeclared=()
  while read -r live_name; do
    [ -n "${live_name}" ] || continue
    declared=0
    for declared_name in "${DECLARED_ENV_NAMES[@]}"; do
      [ "${live_name}" != "${declared_name}" ] || { declared=1; break; }
    done
    [ "${declared}" = "1" ] || undeclared+=("${live_name}")
  done <<<"${live_env_names}"

  if [ ${#undeclared[@]} -gt 0 ]; then
    if [ "${ADMIN_ENV_PRUNE:-0}" = "1" ]; then
      warn "ADMIN_ENV_PRUNE=1 - dropping ${#undeclared[@]} setting(s) the live service carries and
       this deployment does not declare: ${undeclared[*]}"
    else
      warn "the live ${ADMIN_SERVICE} carries ${#undeclared[@]} setting(s) this deployment does not declare:"
      printf '    %s\n' "${undeclared[*]}" >&2
      die "refusing to silently drop them. The service's environment is declarative here, so a
   setting that exists only on the live service disappears on the next revision. Either state them
   in this script, or run again with ADMIN_ENV_PRUNE=1 to say that dropping them is the intent.
   Only NAMES are printed above: this refusal must not republish a value."
    fi
  fi
fi

# 4) the rollout. A new REVISION on an existing service - its URL, IAM policy and revision history
#    all survive. --no-allow-unauthenticated is UNCONDITIONAL here - unlike the console, where it
#    is added only on create so an update cannot momentarily revoke a public binding a live service
#    holds - because there is no state in which this service should be public. --iap turns on
#    Identity-Aware Proxy in front of it.
deploy_args=(
  --region "${GCP_REGION}"
  --image "${ADMIN_IMAGE}"
  --service-account "${ADMIN_SA_EMAIL}"
  --port 8080
  --cpu "${ADMIN_CPU}"
  --memory "${ADMIN_MEMORY}"
  --concurrency "${ADMIN_CONCURRENCY}"
  --timeout "${ADMIN_TIMEOUT_SECONDS}"
  --cpu-boost
  --execution-environment gen2
  --ingress "${ADMIN_INGRESS}"
  --labels "app=hexera,component=admin,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${ADMIN_ENV_PAIRS[*]}")"
  --no-allow-unauthenticated
  --iap
)
if [ ${#ADMIN_SECRET_BINDINGS[@]} -gt 0 ]; then
  deploy_args+=(--set-secrets "$(IFS=,; printf '%s' "${ADMIN_SECRET_BINDINGS[*]}")")
fi

# THE PRIVATE ROUTE, added only where something needs it. A deployment with no outreach database
# keeps the console exactly as it was: no VPC attachment, no Cloud SQL socket, nothing to misroute.
if [ -n "${OUTREACH_DB_HOST:-}" ]; then
  VPC_NETWORK="${VPC_NETWORK:-default}"
  VPC_SUBNET="${VPC_SUBNET:-default}"
  deploy_args+=(
    --network "${VPC_NETWORK}"
    --subnet "${VPC_SUBNET}"
    --vpc-egress private-ranges-only
  )
  # The Cloud SQL socket, when the host is one. OUTREACH_DB_HOST doubles as the socket path -
  # /cloudsql/<connection-name> - which is how the Cloud Run integration exposes it.
  case "${OUTREACH_DB_HOST}" in
    /cloudsql/*) deploy_args+=(--add-cloudsql-instances "${OUTREACH_DB_HOST#/cloudsql/}") ;;
  esac
  log "network       ${VPC_NETWORK}/${VPC_SUBNET}, private-ranges-only (the outreach database is private-IP only)"
fi

# THE SCALING FLAGS ARE CREATE-ONLY. Once the service exists its warm floor and ceiling belong to
# the ADMIN CONSOLE's Fleet page, and `gcloud run deploy` leaves a flag it is not given untouched -
# so omitting them is precisely "do not reconcile this". Re-applying them on every deploy is what
# would silently reset a floor an operator raised, and the symptom - a cold start on the next idle
# request - arrives long after the deploy that caused it.
if [ "${ADMIN_SERVICE_EXISTS}" = "0" ]; then
  deploy_args+=(--min-instances "${ADMIN_MIN_INSTANCES}" --max-instances "${ADMIN_MAX_INSTANCES}")
  ADMIN_SCALING_STATE="${ADMIN_MIN_INSTANCES}..${ADMIN_MAX_INSTANCES} (set at creation)"
else
  ADMIN_SCALING_STATE="left as it is - the admin console owns this service's warm floor"
fi

info "Deploying ${ADMIN_SERVICE} (${ADMIN_DISPOSITION}: ${#ADMIN_ENV_PAIRS[@]} env vars, IAP-gated, never public)"
gc run deploy "${ADMIN_SERVICE}" "${deploy_args[@]}"

# 5) THE IAP SERVICE AGENT GRANT. IAP terminates the request and calls Cloud Run itself, so the
#    IAP service agent - not the end user - is what needs the invoker role. Without it every
#    request returns 403 and the failure reads as a broken IAP policy rather than a missing
#    binding.
IAP_AGENT="service-${GCP_PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"
gc run services add-iam-policy-binding "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
  --member "serviceAccount:${IAP_AGENT}" --role roles/run.invoker >/dev/null \
  || warn "could not grant ${IAP_AGENT} roles/run.invoker on ${ADMIN_SERVICE}. Requests will
       return 403 until it holds that role:
         gcloud run services add-iam-policy-binding ${ADMIN_SERVICE} --project ${GCP_PROJECT_ID} \\
           --region ${GCP_REGION} --member serviceAccount:${IAP_AGENT} --role roles/run.invoker"

# 6) WHO MAY INVOKE IT - NOT a choice, unlike the console's step 6. A public binding on this
#    service is always wrong, so it is removed whenever it is found rather than being governed by
#    a variable. IAP does not protect a service that allUsers may invoke; the two are separate
#    gates.
#
#    A FAILED READ IS NOT A CONFIRMED-EMPTY POLICY. Folding the read into `|| true` made the two
#    indistinguishable: a `get-iam-policy` failure left POLICY_MEMBERS empty, took no branch below,
#    and the summary at the end of this script still asserted "no public invoker binding" -
#    claiming a verification that never happened. The exit status is captured separately so a read
#    failure gets its own warning instead of a false all-clear.
POLICY_RC=0
POLICY_MEMBERS="$(gc run services get-iam-policy "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
  --format='value(bindings.members)' 2>/dev/null)" || POLICY_RC=$?
if [ "${POLICY_RC}" -ne 0 ]; then
  warn "could not read ${ADMIN_SERVICE}'s invoker policy (gcloud exited ${POLICY_RC}) - whether it
       carries a public allUsers binding could not be confirmed. IAP does not protect a service
       that allUsers may invoke, so verify by hand:
         gcloud run services get-iam-policy ${ADMIN_SERVICE} --project ${GCP_PROJECT_ID} \\
           --region ${GCP_REGION}"
  INVOKER_STATE="COULD NOT BE CONFIRMED - the policy read failed; verify by hand"
else
  case "${POLICY_MEMBERS}" in
    *allUsers*)
      warn "${ADMIN_SERVICE} carried a public invoker binding - removing it. IAP does not protect a
       service that allUsers may invoke; the two are separate gates."
      gc run services remove-iam-policy-binding "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
        --member allUsers --role roles/run.invoker >/dev/null \
        || warn "could not remove the public invoker binding from ${ADMIN_SERVICE}. It remains
       publicly invokable until this is run by hand:
         gcloud run services remove-iam-policy-binding ${ADMIN_SERVICE} --project ${GCP_PROJECT_ID} \\
           --region ${GCP_REGION} --member allUsers --role roles/run.invoker"
      INVOKER_STATE="a public invoker binding was found and removed" ;;
    *)
      INVOKER_STATE="no public invoker binding (confirmed)" ;;
  esac
fi

ADMIN_URL="$(gc run services describe "${ADMIN_SERVICE}" --region "${GCP_REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"
log "admin console service ${ADMIN_SERVICE}  (${ADMIN_DISPOSITION})"
log "  identity      ${ADMIN_SA_EMAIL}"
log "  image         ${ADMIN_IMAGE}"
log "  scaling       ${ADMIN_SCALING_STATE}, concurrency ${ADMIN_CONCURRENCY}"
log "  authority     ${#ADMIN_PROJECT_ROLES[@]} project role(s); custom role ${ADMIN_ROLE_ID} (${ADMIN_ROLE_DISPOSITION})"
log "  gate          IAP, fronted by roles/run.invoker for ${IAP_AGENT} - ${INVOKER_STATE}"
log "  url           ${ADMIN_URL:-<not reported>}"

# THE DEPLOYED URL, for the caller that runs after this stage. Task 8's post-deploy verification
# needs it to know where to point its checks, and this script is the one place that already
# resolves it - written to $GITHUB_OUTPUT rather than re-derived downstream, exactly once, ONLY
# when the admin console was actually reconciled (this line is unreachable from the "no admin
# service configured" skip near the top). A no-op outside a GitHub Actions step, so nothing here
# touches a local run or a unit test.
if [ -n "${GITHUB_OUTPUT:-}" ] && [ -n "${ADMIN_URL}" ]; then
  printf 'admin_url=%s\n' "${ADMIN_URL}" >> "${GITHUB_OUTPUT}"
fi
log "done"
