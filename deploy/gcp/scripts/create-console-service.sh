#!/usr/bin/env bash
# Responsibility: Provision the Cloud Run console service - the browser front door of a deployment.
# Owns: the console's identity, its scaling, and whether the public may reach it.
# Boundaries: every credential is a REFERENCE to Secret Manager; the image is promoted, never built.

# Provision the CLOUD RUN CONSOLE SERVICE. Idempotent and RECONCILING: an existing service is
# updated in place - a new revision on the promoted digest - and one already running that digest is
# reported as reused.
#
#   bash deploy/gcp/scripts/create-console-service.sh
#
# WHY IT IS PUBLICLY INVOKABLE. Unlike the API, this service IS the front door: its own Auth.js
# session is the gate, and putting Cloud Run IAM in front of it would mean nobody could reach the
# sign-in page to authenticate at all. That is why CONSOLE_ALLOW_UNAUTHENTICATED defaults to 1
# here and API_ALLOW_UNAUTHENTICATED defaults to 0 there - two different questions, answered
# separately rather than one flag applied to both.
#
# WHY NO VPC EGRESS. The console reaches the product API over its public HTTPS origin, the same
# one the browser's WebSocket dials. It opens no database and no Memorystore, so putting it on the
# VPC would buy nothing and cost a subnet attachment.
#
# INPUTS   the deployment env (CONSOLE_IMAGE, CLOUDRUN_CONSOLE_SERVICE, CONSOLE_*,
#          HEXERA_API_BASE_URL, the *_SECRET container names)
# MUTATES  the console runtime identity, one accessor binding per declared secret, the Cloud Run
#          service, and its invoker policy. It deletes nothing.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

CONSOLE_SERVICE="${CLOUDRUN_CONSOLE_SERVICE:-}"
CONSOLE_SA="${CONSOLE_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-console}"
CONSOLE_SA_EMAIL="${CONSOLE_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# A deployment may genuinely serve no browser console - a mesh-only or API-only arrangement does.
# That is a skip, and it is stated, exactly as the API tier states its own.
if [ -z "${CONSOLE_SERVICE}" ]; then
  info "No Cloud Run console service configured - skipping the console tier"
  log "set CLOUDRUN_CONSOLE_SERVICE to deploy the browser console from this deployment"
  exit 0
fi

# The image is the CONSOLE image, BY DIGEST. promote-release.sh wrote it from the validated
# release record; a tag is refused rather than re-resolved, because a tag can be moved between the
# validation that approved the bytes and the rollout that ships them.
require_digest_reference CONSOLE_IMAGE "${CONSOLE_IMAGE:-}"

CONSOLE_CPU="${CONSOLE_CPU:-1}"
CONSOLE_MEMORY="${CONSOLE_MEMORY:-512Mi}"
CONSOLE_CONCURRENCY="${CONSOLE_CONCURRENCY:-80}"
CONSOLE_TIMEOUT_SECONDS="${CONSOLE_TIMEOUT_SECONDS:-300}"
CONSOLE_MIN_INSTANCES="${CONSOLE_MIN_INSTANCES:-0}"
CONSOLE_MAX_INSTANCES="${CONSOLE_MAX_INSTANCES:-3}"
CONSOLE_INGRESS="${CONSOLE_INGRESS:-all}"
CONSOLE_ALLOW_UNAUTHENTICATED="${CONSOLE_ALLOW_UNAUTHENTICATED:-1}"
APP_ENV="${APP_ENV:-dev}"
CONSOLE_DISPOSITION=created

info "Console service ${CONSOLE_SERVICE} in ${GCP_REGION} (${GCP_PROJECT_ID})"
log "image (validated digest): ${CONSOLE_IMAGE}"

# 1) the console runtime identity - a named account, not the default compute service account.
if sa_exists "${CONSOLE_SA_EMAIL}"; then
  log "console identity ${CONSOLE_SA_EMAIL} (exists)"
else
  info "Creating console runtime identity ${CONSOLE_SA_EMAIL}"
  gc iam service-accounts create "${CONSOLE_SA}" \
    --display-name "Hexera console service" \
    || die "could not create ${CONSOLE_SA_EMAIL}. Creating identities needs
   iam.serviceAccountAdmin, which a DEPLOY identity is deliberately not given. Create it once, as
   an owner:
     gcloud iam service-accounts create ${CONSOLE_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera console service'"
fi

# 2) THE CREDENTIALS, as references. `RUNTIME_VAR:ENV_VAR_HOLDING_THE_SECRET_NAME` - the same shape
#    the API tier and the worker startup script read theirs from. An unset name is skipped, never
#    bound to an empty secret.
SECRET_BINDINGS=()
SECRET_NAMES=()
DECLARED_ENV_NAMES=()
for pair in "AUTH_SECRET:AUTH_SECRET_SECRET" \
            "CONSOLE_AUTH_USERS:CONSOLE_AUTH_USERS_SECRET" \
            "MESH_API_KEY:MESH_API_KEY_SECRET" \
            "USER_TOKEN_SECRET:USER_TOKEN_SECRET_SECRET"; do
  runtime_var="${pair%%:*}"
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  SECRET_BINDINGS+=("${runtime_var}=${secret_name}:latest")
  SECRET_NAMES+=("${secret_name}")
  DECLARED_ENV_NAMES+=("${runtime_var}")
done

for secret_name in ${SECRET_NAMES[@]+"${SECRET_NAMES[@]}"}; do
  secret_exists "${secret_name}" \
    || warn "cannot confirm secret '${secret_name}' exists in ${GCP_PROJECT_ID} - it may be absent,
       or this identity may not be allowed to read Secret Manager. If it is absent:
         gcloud secrets create ${secret_name} --project ${GCP_PROJECT_ID} --replication-policy=automatic"
  if gc secrets add-iam-policy-binding "${secret_name}" \
       --member "serviceAccount:${CONSOLE_SA_EMAIL}" \
       --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then
    log "secret/${secret_name} += roles/secretmanager.secretAccessor -> ${CONSOLE_SA_EMAIL}"
  else
    warn "could not set IAM on secret ${secret_name}. If the binding is already in place the
       rollout below still succeeds; if it is not, the revision never becomes ready and this is
       the command:
         gcloud secrets add-iam-policy-binding ${secret_name} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${CONSOLE_SA_EMAIL} --role roles/secretmanager.secretAccessor"
  fi
done

# 3) THE NON-SECRET SETTINGS.
#    NEXT_PUBLIC_HEXERA_API_BASE_URL is compiled into the browser bundle at BUILD time by Next, so
#    setting it here changes nothing the browser already downloaded. It is stated anyway because a
#    server component may read it, and because a spec that does not name the API this console
#    talks to is not reviewable. When the two origins must differ per environment, the value has
#    to be a build argument to the image - recorded in the deployment doc as a known limit.
CONSOLE_ENV_PAIRS=(
  "ENV=${APP_ENV}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "NODE_ENV=production"
  "HEXERA_API_BASE_URL=${HEXERA_API_BASE_URL}"
  "NEXT_PUBLIC_HEXERA_API_BASE_URL=${NEXT_PUBLIC_HEXERA_API_BASE_URL:-${HEXERA_API_BASE_URL}}"
)
for pair in "${CONSOLE_ENV_PAIRS[@]}"; do
  case "${pair}" in
    *"|"*) die "the setting '${pair%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with. Give it a value without one." ;;
  esac
  DECLARED_ENV_NAMES+=("${pair%%=*}")
done

# 4) WHAT IS ALREADY THERE. --set-env-vars is DECLARATIVE: it replaces the container's environment
#    with exactly what this deployment states. That is what stops drift, and it is also what would
#    silently delete a setting that only ever existed on the live service. The difference is
#    computed, named, and refused unless an operator says to prune it.
if run_svc_exists "${CONSOLE_SERVICE}"; then
  live_image="$(gc run services describe "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [ "${live_image}" = "${CONSOLE_IMAGE}" ]; then
    CONSOLE_DISPOSITION=reused
  else
    CONSOLE_DISPOSITION=rolled
  fi

  live_env_names="$(gc run services describe "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
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
    if [ "${CONSOLE_ENV_PRUNE:-0}" = "1" ]; then
      warn "CONSOLE_ENV_PRUNE=1 - dropping ${#undeclared[@]} setting(s) the live service carries and
       this deployment does not declare: ${undeclared[*]}"
    else
      warn "the live ${CONSOLE_SERVICE} carries ${#undeclared[@]} setting(s) this deployment does not declare:"
      printf '    %s\n' "${undeclared[*]}" >&2
      die "refusing to silently drop them. The service's environment is declarative here, so a
   setting that exists only on the live service disappears on the next revision. Either state them
   in this script, or run again with CONSOLE_ENV_PRUNE=1 to say that dropping them is the intent.
   Only NAMES are printed above: this refusal must not republish a value."
    fi
  fi
fi

# 5) the rollout. A new REVISION on an existing service - its URL, IAM policy and revision history
#    all survive.
deploy_args=(
  --region "${GCP_REGION}"
  --image "${CONSOLE_IMAGE}"
  --service-account "${CONSOLE_SA_EMAIL}"
  --port 8080
  --cpu "${CONSOLE_CPU}"
  --memory "${CONSOLE_MEMORY}"
  --concurrency "${CONSOLE_CONCURRENCY}"
  --timeout "${CONSOLE_TIMEOUT_SECONDS}"
  --min-instances "${CONSOLE_MIN_INSTANCES}"
  --max-instances "${CONSOLE_MAX_INSTANCES}"
  --cpu-boost
  --execution-environment gen2
  --ingress "${CONSOLE_INGRESS}"
  --labels "app=hexera,component=console,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${CONSOLE_ENV_PAIRS[*]}")"
)
if [ ${#SECRET_BINDINGS[@]} -gt 0 ]; then
  deploy_args+=(--set-secrets "$(IFS=','; printf '%s' "${SECRET_BINDINGS[*]}")")
else
  warn "this deployment declares no secret container names for the console, so its existing
       secret references are left exactly as they are."
fi
if [ "${CONSOLE_DISPOSITION}" = "created" ]; then
  # Only on CREATE, so an update cannot momentarily revoke a public binding a live service holds.
  deploy_args+=(--no-allow-unauthenticated)
fi

info "Deploying ${CONSOLE_SERVICE} (${CONSOLE_DISPOSITION}: ${#CONSOLE_ENV_PAIRS[@]} env vars, ${#SECRET_BINDINGS[@]} secret reference(s))"
gc run deploy "${CONSOLE_SERVICE}" "${deploy_args[@]}"

# 6) WHO MAY INVOKE IT. Public by default and by design - see the header. Stated by the
#    deployment, never inherited from whatever the service happened to have.
POLICY_MEMBERS="$(gc run services get-iam-policy "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
  --format='value(bindings.members)' 2>/dev/null || true)"
if [ "${CONSOLE_ALLOW_UNAUTHENTICATED}" = "1" ]; then
  gc run services add-iam-policy-binding "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
    --member allUsers --role roles/run.invoker >/dev/null
  INVOKER_STATE="PUBLIC - allUsers may reach the sign-in page; Auth.js is the gate behind it"
else
  case "${POLICY_MEMBERS}" in
    *allUsers*)
      info "Removing the public invoker binding (CONSOLE_ALLOW_UNAUTHENTICATED is not 1)"
      gc run services remove-iam-policy-binding "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
        --member allUsers --role roles/run.invoker >/dev/null
      INVOKER_STATE="private (the public invoker binding it had was removed)" ;;
    *)
      INVOKER_STATE="private - callers must present an identity holding roles/run.invoker" ;;
  esac
fi

CONSOLE_URL="$(gc run services describe "${CONSOLE_SERVICE}" --region "${GCP_REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"
log "console service ${CONSOLE_SERVICE}  (${CONSOLE_DISPOSITION})"
log "  identity      ${CONSOLE_SA_EMAIL}"
log "  image         ${CONSOLE_IMAGE}"
log "  scaling       ${CONSOLE_MIN_INSTANCES}..${CONSOLE_MAX_INSTANCES} instances, concurrency ${CONSOLE_CONCURRENCY}"
log "  api origin    ${HEXERA_API_BASE_URL}"
log "  credentials   ${#SECRET_BINDINGS[@]} Secret Manager reference(s) - no value is in the spec"
log "  invoker       ${INVOKER_STATE}"
log "  url           ${CONSOLE_URL:-<not reported>}"
log "done"
