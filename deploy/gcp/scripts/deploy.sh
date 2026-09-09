#!/usr/bin/env bash
# Responsibility: Run every mesh provisioning stage in order, behind one confirmation of the plan.
# Owns: the single confirmation gate - the sub-scripts never prompt, so a mutation is agreed to exactly once.
# Boundaries: it provisions the mesh executor only; the application and its data stores run on the operator's machine.

# THE entry point for the mesh executor. One idempotent command that provisions or updates the
# Cloud Run mesh job the local pipeline submits to, and the exchange bucket they trade workspaces
# through.
#
#     make mesh-deploy     (from the repository root)
#
# Beyond that it deploys only what this deployment DECLARES it has: the schema of a hosted database
# (MIGRATE_DB_HOST) and the queue-depth publisher a worker fleet scales on (WORKER_MIG). Both stages
# state that they were skipped when those are unset, which is the mesh-only case this started as -
# there the API, the pipeline, PostgreSQL, Redis, MinIO and SearXNG all run on the operator's
# machine and are configured through the application's own .env.
#
# Every stage is idempotent and safe to rerun after a partial failure: existing resources are
# reconciled rather than duplicated, and a supplied mesh job or bucket is validated and reused,
# never recreated or deleted. Project number, region, image URI, bucket name and the service
# account email are discovered or derived; if configuration is missing, this stops with exactly
# one instruction.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

S="$(cd "$(dirname "$0")" && pwd)"
STAGE=0
# The total is COUNTED, not restated. It was the literal 12, so adding a stage printed [13/12] -
# a number that has to be remembered is a number that goes stale. Only call sites match `^stage "`;
# this definition begins `stage()` and is not counted.
STAGE_TOTAL="$(grep -c '^stage "' "${BASH_SOURCE[0]}")"
stage() { STAGE=$((STAGE + 1)); printf '\n\033[1m━━━ [%d/%d] %s ━━━\033[0m\n' "${STAGE}" "${STAGE_TOTAL}" "$1"; }

# component selection
# WHICH TIERS THIS RUN TOUCHES. A deploy that reconciles all eighteen stages is the right default and
# the wrong routine: most changes are a new application image, and rebuilding the fleet, re-reading
# Cloud SQL and re-minting the object-store credential to ship one costs minutes and money for
# resources nothing in the change affected.
#
# DEPLOY_COMPONENTS is a comma-separated list, or `all` (the default - an unset variable NEVER
# means "less"). The always-on stages are absent from it deliberately: discovery, validation,
# preflight, the plan, API enablement, the registry, the runtime identities, release promotion and
# IAM are either read-only or cheap and idempotent, and skipping them is how a run ends up
# deploying against configuration it never checked.
#
#   images   the mesh job and the API service - the two workloads that carry an application digest
#   data     Cloud SQL and Memorystore
#   storage  the artifacts bucket and its S3-interoperability credential
#   migrate  the schema, applied once before anything serves the new image
#   queue    the queue-depth publisher and the autoscaling policy that reads it
#   workers  the managed instance group and the rolling update onto a new template
#   console  the Cloud Run console service - the promoted console digest, in front of the API
#   admin    the Cloud Run admin console, behind IAP - never publicly reachable
#   edge     the reserved address, load balancer and managed certificate for both consoles' custom
#            hostnames - runs LAST, after both console stages exist for it to route to
#
# A SKIPPED STAGE IS STATED, never silent: each prints what it did not do and why, so a summary
# that says "reused" and a summary that says "not selected" are never read as the same thing.
DEPLOY_COMPONENTS="${DEPLOY_COMPONENTS:-all}"
export DEPLOY_COMPONENTS
want() {
  case ",${DEPLOY_COMPONENTS}," in
    *,all,*) return 0 ;;
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}
# A typo must not quietly deploy less than the operator asked for. `images` is not `image`, and a
# run that silently skipped the API service because of a missing 's' is a bad afternoon.
if [ "${DEPLOY_COMPONENTS}" != "all" ]; then
  _known="images data storage migrate queue workers console admin edge"
  _bad=""
  _good=0
  IFS=',' read -r -a _requested <<< "${DEPLOY_COMPONENTS}"
  for _c in "${_requested[@]}"; do
    [ -n "${_c}" ] || continue
    case " ${_known} " in *" ${_c} "*) _good=$((_good + 1)) ;; *) _bad="${_bad} ${_c}" ;; esac
  done
  [ -z "${_bad}" ] || die "DEPLOY_COMPONENTS names something this deploy has no stage for:${_bad}
   Known components: ${_known}  (or 'all')"
  # COUNTED, not merely non-empty. A value of "," or ",," splits into nothing but empty fields,
  # each skipped by the loop above, and every stage would then report itself as unselected - a
  # deploy that mutates nothing, exits 0, and reads as a success.
  [ "${_good}" -gt 0 ] || die "DEPLOY_COMPONENTS ('${DEPLOY_COMPONENTS}') selects no stage at all.
   Use 'all' to deploy everything, or name at least one of: ${_known}"
fi

# skipped <component> <what would have happened> - one shape for every unselected stage.
skipped() { log "SKIPPED - '$1' is not in DEPLOY_COMPONENTS (${DEPLOY_COMPONENTS}); $2"; }

# NAMING A COMPONENT IS A STATEMENT OF INTENT, and it has to be distinguishable from inheriting it
# through `all`. `all` covers a mesh-only deployment that genuinely has no database, where the
# migration stage skipping itself is correct. Typing `migrate` is different: it says this run is
# expected to move the schema, and a stage that then finds no target and exits 0 has told the
# operator their instruction was carried out when it was not.
explicitly() { [ "${DEPLOY_COMPONENTS}" != "all" ] && want "$1"; }
if explicitly migrate; then
  MIGRATE_REQUIRED=1
  export MIGRATE_REQUIRED
fi

# confirmation policy
# INTERACTIVE BY DEFAULT. A cloud-mutating deploy requires a deliberate go-ahead: after the
# read-only plan is shown, the operator types the exact project ID. Automation opts out ONLY with
# DEPLOY_NONINTERACTIVE=1, and then MUST pin every ambiguous target explicitly - ambient gcloud
# config alone is never enough to mutate a cloud project unattended.
NONINTERACTIVE="${DEPLOY_NONINTERACTIVE:-0}"
if [ "${NONINTERACTIVE}" = "1" ]; then
  _missing=()
  for _v in GCP_PROJECT_ID GCP_REGION CLOUDRUN_MESH_JOB GCP_MESH_BUCKET; do
    [ -n "${!_v:-}" ] || _missing+=("${_v}")
  done
  [ ${#_missing[@]} -eq 0 ] || die \
"DEPLOY_NONINTERACTIVE=1 requires every deployment target to be explicit - set: ${_missing[*]}.
   Ambient gcloud configuration alone is not enough to mutate a cloud project unattended."
fi
# Sub-scripts never prompt on their own; THIS driver owns the single confirmation gate (below).
export ASSUME_YES=1

# confirm_plan - show WHO / WHERE / WHAT before the first cloud mutation, then require the operator
# to type the exact project ID (interactive), or accept the explicitly-pinned targets (DEPLOY_NONINTERACTIVE).
confirm_plan() {
  load_env
  local account impersonation
  account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
  impersonation="$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)"
  [ -n "${impersonation}" ] && [ "${impersonation}" != "(unset)" ] || impersonation="(none)"
  cat <<PLAN

  This will MUTATE Google Cloud. Review the plan before anything is created:

    Human account         ${account:-<none>}
    Impersonating         ${impersonation}
    Deployment ID         ${DEPLOYMENT_ID}
    Project               ${GCP_PROJECT_ID}  (#${GCP_PROJECT_NUMBER})
    Region                ${GCP_REGION}
    Mesh job              ${CLOUDRUN_MESH_JOB}          (${MESH_JOB_DISPOSITION})
    Mesh runtime identity ${MESH_SA_EMAIL}              (${MESH_SA_DISPOSITION})
    Mesh exchange bucket  gs://${GCP_MESH_BUCKET}       (${MESH_BUCKET_DISPOSITION})
    Artifact Registry     ${ARTIFACT_REGISTRY_REPOSITORY}
    Public access         NONE - the mesh job is private and invoked by your local pipeline
PLAN
  if [ "${NONINTERACTIVE}" = "1" ]; then
    log "DEPLOY_NONINTERACTIVE=1 - every target was pinned explicitly; proceeding"
    return 0
  fi
  printf '\n  Type the project ID (%s) to deploy, or anything else to abort: ' "${GCP_PROJECT_ID}"
  local reply; read -r reply
  [ "${reply}" = "${GCP_PROJECT_ID}" ] || die "aborted - the typed value did not match the project ID"
  log "confirmed - proceeding to provision the mesh executor"
}

command -v gcloud >/dev/null 2>&1 || die \
  "gcloud not found. In Google Cloud Shell it is preinstalled; elsewhere install the Google Cloud
   CLI (https://cloud.google.com/sdk/docs/install), then: gcloud auth login && gcloud config set project <PROJECT_ID>"
command -v envsubst >/dev/null 2>&1 || die \
  "envsubst not found (package: gettext). Cloud Shell has it; on Debian/Ubuntu: sudo apt-get install -y gettext"
command -v python3 >/dev/null 2>&1 || die "python3 not found (needed to render config and parse results)"

stage "Discover the environment and generate config"
bash "${S}/bootstrap-env.sh"

stage "Validate the configuration schema (typed, read-only - one precise error if wrong)"
bash "${S}/validate-config.sh"

stage "Preflight (read-only: auth, project, permissions, region, existing mesh resources)"
bash "${S}/preflight.sh"

stage "Confirm the deployment plan (last read-only step before any cloud mutation)"
confirm_plan

stage "Enable required Google Cloud APIs"
bash "${S}/enable-apis.sh"

stage "Artifact Registry + the mesh runtime identity"
bash "${S}/create-artifact-registry.sh"
bash "${S}/create-service-accounts.sh"

stage "Data tier (Cloud SQL and Memorystore on private addresses)"
# BEFORE the schema, the API and the fleet, all of which consume MIGRATE_DB_HOST and REDIS_URL.
# Reconciling: an existing instance is reused untouched and its durability settings are REPORTED
# rather than patched - changing backups or deletion protection on the instance a deploy is about
# to migrate is not something a deploy should decide.
if want data; then
  bash "${S}/create-data-tier.sh"
else
  # The addresses the later stages read come from the env file this stage would refresh. Skipping
  # it is therefore only safe because the instances already exist and their private IPs do not
  # move; bootstrap-env.sh loads the recorded values, and run-migrations.sh refuses to run against
  # an address it cannot resolve rather than guessing one.
  skipped data "Cloud SQL and Memorystore are left exactly as they are"
fi

stage "Object store (artifacts bucket and the S3-interoperability credential)"
# BEFORE the API and the fleet, which read MINIO_*. The HMAC key is REUSED when one already
# exists: GCP allows five per account, and minting one per deploy both leaks credentials and
# fails outright on the fifth run.
if want storage; then
  bash "${S}/create-object-storage.sh"
else
  skipped storage "the artifacts bucket and its HMAC credential are left as they are"
fi

stage "Promote the validated release artifact (no build - see docs/development/gates.md)"
# Deployment does NOT build. It promotes the exact images Gate C validated and release-publish
# pushed, identified by immutable registry digests read from deploy/output/release.json. The
# previous step here submitted a Cloud Build, so the bytes that shipped were built in the cloud
# from source and were never the bytes that were validated.
bash "${S}/promote-release.sh"

stage "Mesh tier (exchange bucket + mesh job; the mesh IMAGE is promoted, never built here)"
# The mesh job MUST exist before IAM - the local caller is granted invoke ON it, and the mesh
# identity is granted exchange access - so this runs AFTER the identity, BEFORE IAM.
if want images; then
  bash "${S}/create-mesh-tier.sh"
else
  skipped images "the mesh job keeps running whichever digest it already has"
fi

stage "IAM (least privilege: invoke the mesh job, exchange objects)"
bash "${S}/apply-iam.sh"

stage "Schema (migrations applied ONCE, before anything runs the promoted image)"
# The API container still migrates itself under an advisory lock, and that stays - it is the safety
# net for every path that is not a deploy. But a deploy must not DEPEND on N cold-starting instances
# racing for a lock on a service that is already public: the schema reaches head here, once, with
# the exit code attributed to the deploy, and a refusal stops it with the database untouched.
if want migrate; then
  bash "${S}/run-migrations.sh"
else
  # DELIBERATELY SKIPPABLE, and the safety net is real: the API container still migrates itself
  # under an advisory lock on start. What is lost is the guarantee that the schema reached head
  # BEFORE the new image serves, which is why 'migrate' belongs in any deploy that ships a
  # revision whose model changed.
  skipped migrate "the schema is untouched; the API still migrates itself on start"
fi

stage "Worker fleet signal (the one queue-depth publisher, and the autoscaler that reads it)"
# The publisher runs OFF the fleet - a scheduled Cloud Run job - because the workers used to be the
# only writers of the number that wakes the workers. Scale-to-zero is unreachable while the metric
# is published by the instances it scales (build-out plan, Decision 4).
if want queue; then
  bash "${S}/create-queue-depth-publisher.sh"
else
  skipped queue "the existing publisher and autoscaling policy keep running"
fi

stage "API service (the promoted image, by digest, reaching the private data tier)"
# AFTER the schema: a service that starts before its database is at head serves errors while the
# migration it needs is still running. Credentials reach it as Secret Manager REFERENCES, never as
# literal values - the four that were once inline in this service's own spec are why.
if want images; then
  bash "${S}/create-api-service.sh"
else
  skipped images "the API service keeps serving whichever digest it already has"
fi

stage "Console service (the promoted console digest, in front of the API)"
# AFTER the API: the console's every page load reaches it, so a console that rolls out first
# serves errors until the API catches up. It is its own component rather than part of `images`
# because iterating on the console is exactly the case that wants to deploy it alone.
if want console; then
  bash "${S}/create-console-service.sh"
else
  skipped console "the console service keeps serving whichever digest it already has"
fi

stage "Admin console (the promoted admin digest, behind IAP)"
# AFTER the API, like the console: its pages read the API and the database, so an admin console
# that rolls out first shows errors until the rest catches up.
if want admin; then
  bash "${S}/create-admin-service.sh"
else
  skipped admin "the admin console keeps serving whichever digest it already has"
fi

stage "Worker fleet (template pinned to the digest, and the rolling update onto it)"
# LAST, because a worker that starts before the schema, the queue signal and the object store are
# in place fails on its first job rather than at deploy time. A digest change makes a NEW template
# and rolls the group onto it with surge 1 / unavailable 0, so a warm pool is never below its floor
# mid-rotation (build-out plan, Decision 4).
if want workers; then
  bash "${S}/create-worker-fleet.sh"
else
  skipped workers "the fleet keeps its current template; no rolling update is started"
fi

stage "Edge (the address, the load balancer and the managed certificate)"
# LAST, and after both console stages: a serverless NEG cannot be created for a Cloud Run service
# that does not exist, and the certificate names hostnames that route to them.
if want edge; then
  bash "${S}/create-edge.sh"
else
  skipped edge "the load balancer keeps its current address, routes and certificate"
fi

# Record the machine-readable deployment-state manifest (ownership + digests; no secret values).
bash "${S}/write-deployment-state.sh" || warn "deployment-state manifest could not be written (non-fatal)"

# final summary
load_env
MESH_DIGEST="$(resolve_digest "${MESH_IMAGE:-}" 2>/dev/null || printf '%s' "${MESH_IMAGE:-}")"

# The conditional stages report what they actually did, so a skip is never read as a success -
# and THREE outcomes are distinguished, not two: the tier was reconciled, the deployment declares
# no such tier, or this run was asked not to touch it. A summary that said "at head" after
# DEPLOY_COMPONENTS excluded `migrate` would be the single most misleading line this script prints.
if ! want migrate; then
  SCHEMA_STATE="NOT RECONCILED - 'migrate' was not selected. Whatever was there is still there."
elif [ -n "${MIGRATE_DB_HOST:-}" ] && [ "${MIGRATE_SKIP:-0}" != "1" ]; then
  SCHEMA_STATE="at head on ${MIGRATE_DB_HOST}/${MIGRATE_DB_NAME:-meshpipeline}  (job ${CLOUDRUN_MIGRATE_JOB:-${DEPLOYMENT_ID}-migrate})"
else
  SCHEMA_STATE="no hosted database declared - the API migrates itself on start"
fi
if ! want queue; then
  QUEUE_SIGNAL="NOT RECONCILED - 'queue' was not selected"
elif [ -n "${WORKER_MIG:-}" ]; then
  QUEUE_SIGNAL="${CLOUDRUN_QUEUE_DEPTH_JOB:-${DEPLOYMENT_ID}-queue-depth} on '${QUEUE_DEPTH_SCHEDULE:-*/2 * * * *}' -> autoscaler ${WORKER_MIG}"
else
  QUEUE_SIGNAL="no worker fleet declared"
fi
if want images; then
  MESH_JOB_STATE="[private, ${MESH_JOB_DISPOSITION}]"
else
  MESH_JOB_STATE="[private, NOT RECONCILED - 'images' was not selected]"
fi

printf '\n\033[1m━━━ mesh executor ready ━━━\033[0m\n'
cat <<SUMMARY

  Deployment id     ${DEPLOYMENT_ID}
  Project / region  ${GCP_PROJECT_ID} / ${GCP_REGION}
  Components        ${DEPLOY_COMPONENTS}
  Mesh job          ${CLOUDRUN_MESH_JOB}            ${MESH_JOB_STATE}
  Mesh identity     ${MESH_SA_EMAIL}                (${MESH_SA_DISPOSITION})
  Mesh image        ${MESH_DIGEST}
  Mesh exchange     gs://${GCP_MESH_BUCKET}         (${MESH_BUCKET_DISPOSITION})
  Schema            ${SCHEMA_STATE}
  Queue depth       ${QUEUE_SIGNAL}
  Deployment state  deploy/output/deployment.json   (ownership + digests; no secret values)

  Point the local application at it: set GCP_PROJECT_ID, GCP_REGION,
  CLOUDRUN_JOB=${CLOUDRUN_MESH_JOB} and GCP_MESH_BUCKET=${GCP_MESH_BUCKET} in .env,
  then run: make dev-up

  Rerun this at any time - it reconciles rather than recreates.

SUMMARY
