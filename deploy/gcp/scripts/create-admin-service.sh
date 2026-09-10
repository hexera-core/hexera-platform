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
# WHY NO SECRET BINDINGS. IAP is this service's gate and it holds no session of its own - there is
# nothing here for Secret Manager to hand it, so that loop is omitted rather than carried empty.
#
# WHY NO VPC EGRESS, NO DATABASE CONNECTION. Those arrive with the next sub-project, alongside the
# first page that actually queries something. Provisioning them here would be ahead of the feature
# that needs them.
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

# 2) THE NON-SECRET SETTINGS. No secret bindings here at all: IAP is this service's gate and it
#    holds no session of its own, so there is nothing for Secret Manager to hand it.
ADMIN_ENV_PAIRS=(
  "ENV=${APP_ENV}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "NODE_ENV=production"
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
if run_svc_exists "${ADMIN_SERVICE}"; then
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
  --min-instances "${ADMIN_MIN_INSTANCES}"
  --max-instances "${ADMIN_MAX_INSTANCES}"
  --cpu-boost
  --execution-environment gen2
  --ingress "${ADMIN_INGRESS}"
  --labels "app=hexera,component=admin,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${ADMIN_ENV_PAIRS[*]}")"
  --no-allow-unauthenticated
  --iap
)

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
log "  scaling       ${ADMIN_MIN_INSTANCES}..${ADMIN_MAX_INSTANCES} instances, concurrency ${ADMIN_CONCURRENCY}"
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
