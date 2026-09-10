#!/usr/bin/env bash
# Responsibility: Provision the Cloud Run API service - the digest-pinned front door of a deployment.
# Owns: the service's network path, its scaling floor and ceiling, and whether it may be invoked anonymously.
# Boundaries: every credential is a REFERENCE to Secret Manager; the image is promoted, never built and never re-resolved from a tag.

# Provision the CLOUD RUN API SERVICE. Idempotent and RECONCILING: a service that already exists is
# updated in place - a new revision on the promoted digest, never a delete and recreate - and one
# already running that digest is reported as reused.
#
#   bash deploy/gcp/scripts/create-api-service.sh
#
# WHY EVERY CREDENTIAL IS A REFERENCE. docs/deployment/gcp-live-inventory.md records exactly what
# this exists to stop: DEEPINFRA_API_KEY, DEEPSEEK_API_KEY, POSTGRES_PASSWORD and MINIO_SECRET_KEY
# sitting as literal values in the public hexera-dev-api service spec - readable by anyone holding
# run.services.get, and echoed into every `gcloud` output and deploy log that dumps the service.
# What this script places in the spec is a Secret Manager CONTAINER NAME; the runtime resolves it
# under the service's own identity, so no value enters the spec, the log, or this file's output.
# devtools/quality/check_deploy_secrets.py is the gate that keeps it that way.
#
# WHY DIRECT VPC EGRESS. Cloud SQL and Memorystore answer on private addresses only, so a service
# that is not on the VPC cannot reach its own database. --network/--subnet put the revision on that
# network; private-ranges-only keeps everything else - the model providers the agents call - on the
# ordinary public path instead of hairpinning the whole internet through the VPC.
#
# WHY THE COMMAND IS THE ENTRYPOINT. deploy/docker/entrypoint.sh applies the shipped migrations
# under a Postgres advisory lock before it execs uvicorn. run-migrations.sh already brought the
# schema to head once, before anything ran this image; the entrypoint stays as the safety net for
# every path that did not come through a deploy, and on an already-migrated database it finds
# nothing to do.
#
# INPUTS   the deployment env (APP_IMAGE, CLOUDRUN_API_SERVICE, API_*, VPC_*, the *_SECRET names)
# MUTATES  the API runtime identity, one accessor binding per declared secret, the Cloud Run
#          service, and the service's invoker policy. It deletes nothing.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

API_SERVICE="${CLOUDRUN_API_SERVICE:-}"
API_SA="${API_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-api}"
API_SA_EMAIL="${API_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# A deployment may genuinely serve no API from Cloud Run - the mesh-only arrangement this tooling
# started as runs the API, the pipeline and every data store on the operator's machine and reaches
# out only to the mesh job. That is a skip, and it is stated, the same way the migration and the
# queue-depth publisher state theirs.
if [ -z "${API_SERVICE}" ]; then
  info "No Cloud Run API service configured - skipping the API tier"
  log "set CLOUDRUN_API_SERVICE to deploy the API from this deployment rather than run it locally"
  exit 0
fi

# The image is the APPLICATION image, BY DIGEST: the same bytes the pre-deploy migration ran and the
# queue-depth publisher publishes from. promote-release.sh wrote it from the validated release
# record; this refuses a tag rather than re-resolving one, because a tag can be moved between the
# validation that approved the bytes and the rollout that ships them.
require_digest_reference APP_IMAGE "${APP_IMAGE:-}"

# sizing and scaling
API_CPU="${API_CPU:-2}"
API_MEMORY="${API_MEMORY:-2Gi}"
API_CONCURRENCY="${API_CONCURRENCY:-160}"
API_TIMEOUT_SECONDS="${API_TIMEOUT_SECONDS:-900}"
# THE FLOOR IS THE WARM POOL, and the two environments differ deliberately (build-out plan,
# Decision 4: scale-to-zero in dev, a warm pool in prod). Dev is a shared sandbox that is idle most
# of the day, and a cold start there costs a wait nobody is paying for; prod pays for a warm
# instance so the first request of the day does not absorb a multi-gigabyte image pull plus an
# advisory-locked migration check. Zero is the default because an environment that never states a
# floor should not be silently billed for one.
API_MIN_INSTANCES="${API_MIN_INSTANCES:-0}"
# The ceiling is the COST CEILING: every instance can spend money at DeepInfra and DeepSeek.
API_MAX_INSTANCES="${API_MAX_INSTANCES:-5}"
API_INGRESS="${API_INGRESS:-all}"
APP_ENV="${APP_ENV:-dev}"
API_CORS_ORIGINS="${API_CORS_ORIGINS:-*}"
VPC_NETWORK="${VPC_NETWORK:-default}"
VPC_SUBNET="${VPC_SUBNET:-default}"
API_DISPOSITION=created

# PUBLIC INVOCATION IS A STATED CHOICE, and the default is private. The live dev service is
# `allUsers`-invokable with CORS_ORIGINS=* while it spends real money on model calls; that is a
# sandbox concession with an expiry date (build-out plan, Decision 1), not a property prod may
# inherit by omission. A deployment that wants the open endpoint asks for it by name.
API_ALLOW_UNAUTHENTICATED="${API_ALLOW_UNAUTHENTICATED:-0}"

# A CREDENTIALED BROKER URL IS A SECRET, whatever the catalogue calls the setting. `redis://` with a
# password in the authority is a plaintext credential the moment it lands in a spec, and this is the
# one place that could put it there. Refused before anything is created rather than published.
case "${REDIS_URL:-}" in
  *"@"*) die "REDIS_URL carries credentials in its authority. A Cloud Run spec is readable by
   anyone holding run.services.get, so a credentialed URL there is a published password. Put the
   password in Secret Manager, reference it, and give REDIS_URL the host and port only." ;;
esac

info "API service ${API_SERVICE} in ${GCP_REGION} (${GCP_PROJECT_ID})"
log "image (validated digest): ${APP_IMAGE}"

# 1) the API runtime identity. It is the identity that opens the database, reads the credentials
#    below, and submits mesh executions - so it is a named account rather than the default compute
#    service account the live project attaches to everything.
if sa_exists "${API_SA_EMAIL}"; then
  log "api identity    ${API_SA_EMAIL} (exists)"
else
  info "Creating API runtime identity ${API_SA_EMAIL}"
  gc iam service-accounts create "${API_SA}" \
    --display-name "Hexera API service" \
    || die "could not create ${API_SA_EMAIL}. Creating identities needs iam.serviceAccountAdmin,
   which a DEPLOY identity is deliberately not given. Create it once, as an owner:
     gcloud iam service-accounts create ${API_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera API service'"
fi

# 2) THE CREDENTIALS, as references. Each entry is `RUNTIME_VAR:ENV_VAR_HOLDING_THE_SECRET_NAME`,
#    the same shape deploy/gcp/worker/startup.sh reads its four from - one rule, spelled the same
#    way on both tiers: the deployment env names a Secret Manager container, never a value. The
#    `<SETTING>_SECRET` convention is uniform even where it reads oddly (USER_TOKEN_SECRET_SECRET
#    names the container holding USER_TOKEN_SECRET), because a convention with one exception is a
#    convention nobody can apply to the next credential.
#
#    A credential this deployment does not use is simply absent - an unset name is skipped, not
#    bound to an empty secret.
SECRET_BINDINGS=()
SECRET_NAMES=()
DECLARED_ENV_NAMES=()
for pair in "POSTGRES_PASSWORD:POSTGRES_PASSWORD_SECRET" \
            "MINIO_SECRET_KEY:MINIO_SECRET_KEY_SECRET" \
            "DEEPINFRA_API_KEY:DEEPINFRA_API_KEY_SECRET" \
            "DEEPSEEK_API_KEY:DEEPSEEK_API_KEY_SECRET" \
            "MESH_API_KEY:MESH_API_KEY_SECRET" \
            "USER_TOKEN_SECRET:USER_TOKEN_SECRET_SECRET"; do
  runtime_var="${pair%%:*}"
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  # `:latest` is the version, not a value: the reference is resolved at instance start under the
  # service's own identity, so rotating the credential is a new secret version and nothing else.
  SECRET_BINDINGS+=("${runtime_var}=${secret_name}:latest")
  SECRET_NAMES+=("${secret_name}")
  DECLARED_ENV_NAMES+=("${runtime_var}")
done

# The grant is PER SECRET rather than project-wide - the rule item 9 of the build-out plan states,
# applied to the identity that actually reads them. A DEPLOY IDENTITY MAY NOT BE ABLE TO SET IT:
# secret IAM is an owner's act and the four roles a CI deployer holds do not include it. The grant
# is attempted, a failure is reported with the command that fixes it, and the ROLLOUT is the
# verdict - a revision that cannot resolve a reference never becomes ready, so a missing binding
# cannot pass as a successful deploy.
for secret_name in ${SECRET_NAMES[@]+"${SECRET_NAMES[@]}"}; do
  secret_exists "${secret_name}" \
    || warn "cannot confirm secret '${secret_name}' exists in ${GCP_PROJECT_ID} - it may be absent,
       or this identity may not be allowed to read Secret Manager. If it is absent:
         gcloud secrets create ${secret_name} --project ${GCP_PROJECT_ID} --replication-policy=automatic"
  if gc secrets add-iam-policy-binding "${secret_name}" \
       --member "serviceAccount:${API_SA_EMAIL}" \
       --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then
    log "secret/${secret_name} += roles/secretmanager.secretAccessor -> ${API_SA_EMAIL}"
  else
    warn "could not set IAM on secret ${secret_name}. If the binding is already in place the
       rollout below still succeeds; if it is not, the revision never becomes ready and this is
       the command:
         gcloud secrets add-iam-policy-binding ${secret_name} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${API_SA_EMAIL} --role roles/secretmanager.secretAccessor"
  fi
done

# 3) THE NON-SECRET SETTINGS. Endpoints, identity of the surrounding deployment, and policy - every
#    one of them safe to read in a spec, which is why they are values and the six above are not.
#    The database is named in the clear (host, port, database, user) for the same reason the
#    migration job names it: a spec that cannot say which database it talks to is not reviewable.
API_ENV_PAIRS=(
  "ENV=${APP_ENV}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "GCP_PROJECT_ID=${GCP_PROJECT_ID}"
  "GCP_REGION=${GCP_REGION}"
  # ENV=production refuses a wildcard at startup (settings inventory, "Auth / environment"), so a
  # prod deployment that leaves this at its default fails closed rather than serving every origin.
  "CORS_ORIGINS=${API_CORS_ORIGINS}"
  # Identity Platform lives in the SAME project as everything else this deployment provisions -
  # the console signs in through it, and this is the audience the API verifies the resulting ID
  # token against. A deployment that ever splits them states FIREBASE_PROJECT_ID explicitly;
  # until then the default is correct and nobody has to know Identity Platform's project id.
  "FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID:-${GCP_PROJECT_ID}}"
  # Whether an unknown Identity Platform account may provision itself an organisation on first
  # sign-in, and how many credits that organisation starts with. Both are API settings, never the
  # console's: the console can only decline to *render* /sign-up, while anyone can create an
  # Identity Platform account directly against the project's public web API key and present the
  # resulting token, so the API is the only place this is a real gate.
  "CONSOLE_SIGNUP_ENABLED=${CONSOLE_SIGNUP_ENABLED:-true}"
  "SIGNUP_GRANT_CREDITS=${SIGNUP_GRANT_CREDITS:-100}"
)
# The mesh executor, if this deployment has one. The application reads the job as CLOUDRUN_JOB.
if [ -n "${CLOUDRUN_MESH_JOB:-}" ]; then
  API_ENV_PAIRS+=("CLOUDRUN_JOB=${CLOUDRUN_MESH_JOB}")
fi
if [ -n "${GCP_MESH_BUCKET:-}" ]; then
  API_ENV_PAIRS+=("GCP_MESH_BUCKET=${GCP_MESH_BUCKET}")
fi
# The database this deployment migrated. POSTGRES_* and not DATABASE_URL: a DSN carries the
# password, and DATABASE_URL is declared secret precisely because it does.
if [ -n "${MIGRATE_DB_HOST:-}" ]; then
  API_ENV_PAIRS+=(
    "POSTGRES_HOST=${MIGRATE_DB_HOST}"
    "POSTGRES_PORT=${MIGRATE_DB_PORT:-5432}"
    "POSTGRES_DB=${MIGRATE_DB_NAME:-meshpipeline}"
    "POSTGRES_USER=${MIGRATE_DB_USER:-meshpipeline}"
  )
fi
if [ -n "${REDIS_URL:-}" ]; then
  API_ENV_PAIRS+=(
    "REDIS_URL=${REDIS_URL}"
    "CELERY_BROKER_URL=${REDIS_URL}"
    "CELERY_RESULT_BACKEND=${REDIS_URL}"
  )
fi
# THE OBJECT STORE'S NON-SECRET HALF. The secret is mounted as a reference below, but a credential
# on its own addresses nothing: without these the adapter falls back to its local-stack defaults
# and dials localhost:9000, which on Cloud Run is the container itself. That failure is silent
# until the first upload, and it presents as "Storage is unavailable" rather than as a missing
# setting - which is exactly how it was found, on a deployed prod API.
if [ -n "${MINIO_ENDPOINT:-}" ]; then
  API_ENV_PAIRS+=(
    "MINIO_ENDPOINT=${MINIO_ENDPOINT}"
    "MINIO_PUBLIC_ENDPOINT=${MINIO_PUBLIC_ENDPOINT:-${MINIO_ENDPOINT}}"
    "MINIO_ACCESS_KEY=${MINIO_ACCESS_KEY}"
    "MINIO_BUCKET=${MINIO_BUCKET}"
    "MINIO_REGION=${MINIO_REGION}"
    # Google Cloud Storage's S3 endpoint serves TLS only; the adapter defaults to plain HTTP for
    # the local stack, so this must be stated or every request fails.
    "MINIO_SECURE=${MINIO_SECURE:-true}"
  )
fi

# API_EXTRA_ENV carries whatever else this deployment states - the agent model routing, the token
# budgets, the feature flags. It is '|'-SEPARATED, and the separator is neither of the two obvious
# ones for a reason: several of these values are themselves comma-separated lists (CORS_ORIGINS,
# model rosters), and '@' appears inside every image digest and every address-shaped value. A pipe
# cannot occur in an environment variable NAME and does not occur in any value this deployment
# builds, and the assembled list is checked for one below rather than assumed.
#
# A CREDENTIAL MAY NOT TRAVEL THIS WAY. check_deploy_secrets.py matches a declared secret-bearing
# name at the start of a line in generated.env, so a credential smuggled inside this one variable
# would pass the gate that exists to catch it. The roster below is the `secret=True` set from
# src/meshpipeline/settings/inventory.py, restated here for exactly that blind spot.
SECRET_BEARING=(POSTGRES_PASSWORD DATABASE_URL REDIS_PASSWORD MINIO_SECRET_KEY MESH_API_KEY
                USER_TOKEN_SECRET LANGFUSE_SECRET_KEY TAVILY_API_KEY SENTRY_DSN
                DEEPSEEK_API_KEY DEEPINFRA_API_KEY)
if [ -n "${API_EXTRA_ENV:-}" ]; then
  IFS='|' read -r -a _extra_pairs <<<"${API_EXTRA_ENV}"
  for pair in ${_extra_pairs[@]+"${_extra_pairs[@]}"}; do
    [ -n "${pair}" ] || continue
    case "${pair}" in
      *=*) ;;
      *) die "API_EXTRA_ENV entry '${pair}' is not KEY=VALUE. Entries are separated by '|'." ;;
    esac
    name="${pair%%=*}"
    for forbidden in "${SECRET_BEARING[@]}"; do
      [ "${name}" != "${forbidden}" ] || die "API_EXTRA_ENV sets ${name}, which is declared
   secret-bearing in src/meshpipeline/settings/inventory.py. A credential reaches this service as a
   Secret Manager reference and never as a value: set ${name}_SECRET to the container name instead."
    done
    API_ENV_PAIRS+=("${pair}")
  done
fi
for pair in "${API_ENV_PAIRS[@]}"; do
  # The list is handed to gcloud with '|' as its delimiter, so a '|' inside a value would split one
  # setting into two. Refused here, where the offending setting can be named, rather than in a
  # rejected API call that names only the malformed list.
  case "${pair}" in
    *"|"*) die "the setting '${pair%%=*}' has a '|' in its value, which is the delimiter this
   environment list is passed with. Give it a value without one." ;;
  esac
  DECLARED_ENV_NAMES+=("${pair%%=*}")
done

# 4) WHAT IS ALREADY THERE. --set-env-vars is DECLARATIVE: it replaces the container's environment
#    with exactly what this deployment states, which is the property that stops a service drifting
#    into whatever a previous run left behind. It is also the property that would silently delete a
#    setting that only ever existed on the live service - and the live hexera-dev-api carries ~150
#    inline environment variables that no file in this repository describes. So the difference is
#    computed, named, and refused unless an operator has said to prune it.
if run_svc_exists "${API_SERVICE}"; then
  live_image="$(gc run services describe "${API_SERVICE}" --region "${GCP_REGION}" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [ "${live_image}" = "${APP_IMAGE}" ]; then
    API_DISPOSITION=reused
  else
    API_DISPOSITION=rolled
  fi

  live_env_names="$(gc run services describe "${API_SERVICE}" --region "${GCP_REGION}" \
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
    if [ "${API_ENV_PRUNE:-0}" = "1" ]; then
      warn "API_ENV_PRUNE=1 - dropping ${#undeclared[@]} setting(s) the live service carries and this
       deployment does not declare: ${undeclared[*]}"
    else
      warn "the live ${API_SERVICE} carries ${#undeclared[@]} setting(s) this deployment does not declare:"
      printf '    %s\n' "${undeclared[*]}" >&2
      die "refusing to silently drop them. The service's environment is declarative here, so a
   setting that exists only on the live service disappears on the next revision - which for the
   agent model routing means an API that starts and then behaves differently. Either state them in
   API_EXTRA_ENV (KEY=VALUE entries separated by '|'), or run again with API_ENV_PRUNE=1 to say
   that dropping them is the intent.
   Only NAMES are printed above: this refusal must not republish a value."
    fi
  fi
fi

# 5) the rollout. `gcloud run deploy` on an existing service creates a NEW REVISION and migrates
#    traffic to it; it never deletes and recreates the service, so its URL, its IAM policy and its
#    revision history all survive a rotation.
deploy_args=(
  --region "${GCP_REGION}"
  --image "${APP_IMAGE}"
  --service-account "${API_SA_EMAIL}"
  # The migration-aware entrypoint, stated rather than inherited: the image's own ENTRYPOINT is the
  # same script, and naming it here means a spec review can see what runs without reading a
  # Dockerfile.
  --command /srv/entrypoint.sh
  --args "uvicorn,meshpipeline.runtime.api_server:app,--host,0.0.0.0,--port,8000"
  --port 8000
  --cpu "${API_CPU}"
  --memory "${API_MEMORY}"
  --concurrency "${API_CONCURRENCY}"
  --timeout "${API_TIMEOUT_SECONDS}"
  --min-instances "${API_MIN_INSTANCES}"
  --max-instances "${API_MAX_INSTANCES}"
  # Startup CPU boost: the container pulls a multi-gigabyte image and then runs an advisory-locked
  # migration check before it listens. Without the boost a cold start spends that on one vCPU.
  --cpu-boost
  --execution-environment gen2
  --ingress "${API_INGRESS}"
  --network "${VPC_NETWORK}"
  --subnet "${VPC_SUBNET}"
  --vpc-egress private-ranges-only
  --labels "app=hexera,component=api,version=0-0-1,deployment-id=${DEPLOYMENT_ID},managed-by=deploy"
  --set-env-vars "^|^$(IFS='|'; printf '%s' "${API_ENV_PAIRS[*]}")"
)
if [ ${#SECRET_BINDINGS[@]} -gt 0 ]; then
  # Secret references carry no comma, so the default separator is unambiguous here.
  deploy_args+=(--set-secrets "$(IFS=','; printf '%s' "${SECRET_BINDINGS[*]}")")
else
  warn "this deployment declares no secret container names, so the service's existing secret
       references are left exactly as they are. A deployment that means to have none clears them
       deliberately:
         gcloud run services update ${API_SERVICE} --region ${GCP_REGION} --project ${GCP_PROJECT_ID} --clear-secrets"
fi
if [ "${API_DISPOSITION}" = "created" ]; then
  # Only on CREATE. On an update the flag is omitted so this rollout cannot momentarily revoke the
  # public binding a live service already holds; step 6 is the one place the invoker policy is
  # decided, and it decides it once the new revision is serving.
  deploy_args+=(--no-allow-unauthenticated)
fi

info "Deploying ${API_SERVICE} (${API_DISPOSITION}: ${#API_ENV_PAIRS[@]} env vars, ${#SECRET_BINDINGS[@]} secret reference(s))"
gc run deploy "${API_SERVICE}" "${deploy_args[@]}"

# 6) WHO MAY INVOKE IT. Stated by the deployment, not inherited from what the service happened to
#    have. Removing an `allUsers` binding is not a deletion of a resource - it is this variable
#    being true - and a prod service that is private in configuration and public in fact is the
#    single failure this flag exists to prevent.
POLICY_MEMBERS="$(gc run services get-iam-policy "${API_SERVICE}" --region "${GCP_REGION}" \
  --format='value(bindings.members)' 2>/dev/null || true)"
if [ "${API_ALLOW_UNAUTHENTICATED}" = "1" ]; then
  gc run services add-iam-policy-binding "${API_SERVICE}" --region "${GCP_REGION}" \
    --member allUsers --role roles/run.invoker >/dev/null
  INVOKER_STATE="PUBLIC - allUsers may invoke it"
  warn "${API_SERVICE} is invokable by anyone on the internet, and every call it serves can spend
       money at DeepInfra and DeepSeek. That is API_ALLOW_UNAUTHENTICATED=1."
else
  case "${POLICY_MEMBERS}" in
    *allUsers*)
      info "Removing the public invoker binding (API_ALLOW_UNAUTHENTICATED is not 1)"
      gc run services remove-iam-policy-binding "${API_SERVICE}" --region "${GCP_REGION}" \
        --member allUsers --role roles/run.invoker >/dev/null
      INVOKER_STATE="private (the public invoker binding it had was removed)" ;;
    *)
      INVOKER_STATE="private - callers must present an identity holding roles/run.invoker" ;;
  esac
fi

API_URL="$(gc run services describe "${API_SERVICE}" --region "${GCP_REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"
log "api service     ${API_SERVICE}  (${API_DISPOSITION})"
log "  identity      ${API_SA_EMAIL}"
log "  image         ${APP_IMAGE}"
log "  scaling       ${API_MIN_INSTANCES}..${API_MAX_INSTANCES} instances, concurrency ${API_CONCURRENCY}"
log "  network       ${VPC_NETWORK}/${VPC_SUBNET}, private-ranges-only, ingress ${API_INGRESS}"
log "  credentials   ${#SECRET_BINDINGS[@]} Secret Manager reference(s) - no value is in the spec"
log "  invoker       ${INVOKER_STATE}"
log "  url           ${API_URL:-<not reported>}"
log "done"
