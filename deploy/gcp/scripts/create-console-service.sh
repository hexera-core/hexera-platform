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

# HEXERA_API_BASE_URL is dereferenced unguarded below, in the container env spec. Stated here,
# before step 1 creates anything, so a deployment missing it dies with a clear refusal rather than
# an `unbound variable` after a service account and IAM bindings already exist.
require_vars HEXERA_API_BASE_URL

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

# 0) THE CREDENTIALS, as references. `RUNTIME_VAR:ENV_VAR_HOLDING_THE_SECRET_NAME` - the same shape
#    the API tier and the worker startup script read theirs from. An unset name is skipped, never
#    bound to an empty secret. Built here, before step 1, purely by reading env vars already
#    loaded - nothing below this point mutates anything, so it is safe to resolve ahead of the
#    refusal that follows.
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

# 0.5) REFUSE A KNOWN-ABSENT CONTAINER BEFORE ANY MUTATION. bootstrap-env.sh declares these
#      container names and create-secrets.sh is the OWNER-run script that creates them - it is not
#      a deploy.sh stage, so nothing in an ordinary deploy ever creates them. Left as a warning,
#      this stage would still pass --set-secrets a container Cloud Run refuses to bind, and the
#      rollout below fails after the identity and its IAM already exist. Refusing here - before
#      step 1 creates the service account or binds any IAM - is the same "refuse before mutating"
#      discipline create-secrets.sh already applies to its own required-and-missing case.
#
#      ONLY a CONFIRMED absence stops the deploy (secret_confirmed_absent, not secret_exists): a
#      failed read is also what this identity lacking secretmanager.viewer looks like, and that
#      ambiguity must not be read as proof the container is missing - it stays a warning, in the
#      loop below, exactly as today.
ABSENT_SECRETS=()
for secret_name in ${SECRET_NAMES[@]+"${SECRET_NAMES[@]}"}; do
  secret_confirmed_absent "${secret_name}" && ABSENT_SECRETS+=("${secret_name}")
done
if [ ${#ABSENT_SECRETS[@]} -gt 0 ]; then
  MSG="the console references ${#ABSENT_SECRETS[@]} secret container(s) confirmed absent in ${GCP_PROJECT_ID}:"
  for secret_name in "${ABSENT_SECRETS[@]}"; do MSG="${MSG}
     ${secret_name}"; done
  MSG="${MSG}
   create-secrets.sh declares them but is owner-run, not a deploy.sh stage, so nothing in a deploy
   creates them. Create each once, as an owner, then add a version (read from stdin, never a
   command-line argument):"
  for secret_name in "${ABSENT_SECRETS[@]}"; do
    MSG="${MSG}
     gcloud secrets create ${secret_name} --project ${GCP_PROJECT_ID} --replication-policy=automatic
     gcloud secrets versions add ${secret_name} --project ${GCP_PROJECT_ID} --data-file=-"
  done
  die "${MSG}"
fi

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

# 2) THE CREDENTIALS' IAM: grant each secret to the console identity now that it exists.
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
#    NEXT_PUBLIC_HEXERA_API_BASE_URL is load-bearing here and DOES take effect: its only consumer
#    is a server component that injects it into the page as it renders, and the console image
#    passes no NEXT_PUBLIC_* build argument, so nothing was inlined and the SSR bundle still reads
#    process.env at request time. The browser's WebSocket dials this origin directly - it does not
#    pass through this service's proxy - so a wrong value here is a console that renders correctly
#    and never streams.
#
#    DO NOT supply this as a Docker build argument to "fix" it per environment. NEXT_PUBLIC_ is
#    precisely the prefix Next inlines at build time when a value is present then; supplying one
#    would freeze it at whichever origin the image was built against, silently, and promote that
#    same frozen value to every environment. The durable fix is to stop using the NEXT_PUBLIC_
#    prefix for a value no client code reads.
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
