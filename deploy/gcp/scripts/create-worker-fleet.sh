#!/usr/bin/env bash
# Responsibility: Provision the worker fleet - one instance template per digest, the managed group, and the policy that sizes it.
# Owns: template naming, the rolling update that moves an existing group onto a new digest, and the warm-pool floor.
# Boundaries: instance metadata carries secret NAMES only; the group is rolled, never recreated, and never deleted.

# Provision the WORKER FLEET: a digest-pinned instance template, the managed instance group that
# runs it, and the autoscaler that sizes the group from the queue-depth metric.
#
#   bash deploy/gcp/scripts/create-worker-fleet.sh
#
# Idempotent and RECONCILING. An instance template is immutable, so a new one is created whenever
# what a worker would run changes, and the existing GROUP is rolled onto it - `rolling-action
# start-update`, not a delete and recreate. The group keeps its name, its autoscaler, its in-flight
# instances and its history across every rotation.
#
# WHY METADATA CARRIES NAMES AND NOT VALUES. `gcloud compute instances describe` is readable by
# anyone holding compute.instances.get, and so is the template. deploy/gcp/worker/startup.sh
# already reads its four credentials BY NAME - metadata says `postgres-password-secret`, the
# instance fetches the value under its own identity, and the value only ever exists in a root-owned
# file. This script writes the names that contract expects and nothing else; rotating a credential
# is then a new secret version plus an instance roll, with no template to edit.
#
# WHY THE FLOOR DIFFERS BY ENVIRONMENT. Build-out plan, Decision 4: scale-to-zero in dev, a warm
# pool in prod. Dev is a shared sandbox that is idle most of the day, and the queue-depth metric now
# comes from a scheduled job off the fleet (create-queue-depth-publisher.sh), so a group at zero can
# still be woken - which is what made a floor of zero reachable at all. Prod holds a warm pool
# because a cold worker pays for a VM boot, a docker install and a multi-gigabyte image pull before
# it takes the first job, and that latency is charged to a paying customer rather than to a
# developer who is watching the console anyway.
#
# INPUTS   the deployment env (APP_IMAGE, WORKER_*, VPC_*, REDIS_URL, MIGRATE_DB_*, the *_SECRET names)
# MUTATES  the worker identity, one accessor binding per declared secret, one instance template per
#          distinct worker specification, the managed instance group, and its autoscaling policy.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

# A deployment may declare no fleet - the mesh-only arrangement runs the pipeline on the operator's
# machine. That is a skip and it is stated, the same way the migration and the publisher state
# theirs. The group is ZONAL because everything downstream already treats it that way:
# validate-config.sh refuses a WORKER_MIG without a WORKER_MIG_ZONE, and the published metric's
# `location` label - which the autoscaler filter matches on - is that zone.
if [ -z "${WORKER_MIG:-}" ] || [ -z "${WORKER_MIG_ZONE:-}" ]; then
  info "No worker fleet configured - skipping the fleet"
  log "set WORKER_MIG and WORKER_MIG_ZONE to run pipeline work on managed instances"
  exit 0
fi

# The image is the APPLICATION image, BY DIGEST - the same bytes the API serves and the migration
# applied. A tag is refused rather than re-resolved: the fleet is the longest-lived consumer of an
# image reference here, so a moved tag would be discovered weeks later as two workers running
# different code.
require_digest_reference APP_IMAGE "${APP_IMAGE:-}"
require_vars WORKER_ENV_URI REDIS_URL

# instance shape - the live fleet's, stated rather than remembered
WORKER_MACHINE_TYPE="${WORKER_MACHINE_TYPE:-e2-standard-4}"
WORKER_BOOT_DISK_GB="${WORKER_BOOT_DISK_GB:-100}"
WORKER_BOOT_DISK_TYPE="${WORKER_BOOT_DISK_TYPE:-pd-balanced}"
WORKER_IMAGE_FAMILY="${WORKER_IMAGE_FAMILY:-ubuntu-2204-lts}"
WORKER_IMAGE_PROJECT="${WORKER_IMAGE_PROJECT:-ubuntu-os-cloud}"
VPC_NETWORK="${VPC_NETWORK:-default}"
VPC_SUBNET="${VPC_SUBNET:-default}"
WORKER_BASE_INSTANCE_NAME="${WORKER_BASE_INSTANCE_NAME:-${WORKER_MIG}}"

# scaling - the same knobs create-queue-depth-publisher.sh reads, so the two writers of this policy
# cannot disagree about it
WORKER_MIG_MIN_REPLICAS="${WORKER_MIG_MIN_REPLICAS:-0}"
WORKER_MIG_MAX_REPLICAS="${WORKER_MIG_MAX_REPLICAS:-5}"
WORKER_MIG_COOLDOWN_SECONDS="${WORKER_MIG_COOLDOWN_SECONDS:-180}"
WORKER_JOBS_PER_INSTANCE="${WORKER_JOBS_PER_INSTANCE:-1}"
QUEUE_NAME="${QUEUE_NAME:-simulation_jobs}"
METRIC="custom.googleapis.com/hexera/queue_depth"

# rotation - see step 5 for why these values and not others
WORKER_ROLLING_TYPE="${WORKER_ROLLING_TYPE:-proactive}"
WORKER_ROLLING_MAX_SURGE="${WORKER_ROLLING_MAX_SURGE:-1}"
WORKER_ROLLING_MAX_UNAVAILABLE="${WORKER_ROLLING_MAX_UNAVAILABLE:-0}"
WORKER_ROLLING_MIN_READY_SECONDS="${WORKER_ROLLING_MIN_READY_SECONDS:-180}"

# THE ENDPOINTS THE INSTANCE WILL READ FROM METADATA, checked before anything is created.
#
# A CREDENTIALED BROKER URL IS A SECRET whatever the settings catalogue calls the setting, and
# instance metadata is readable by anyone who can describe the instance. Refused rather than
# published - this is the same class of disclosure the live audit found in the Cloud Run spec.
case "${REDIS_URL}" in
  *"@"*) die "REDIS_URL carries credentials in its authority, and instance metadata is readable by
   anyone holding compute.instances.get. Put the password in Secret Manager and give REDIS_URL the
   host and port only." ;;
esac
case "${WORKER_ENV_URI}" in
  gs://*) ;;
  *) die "WORKER_ENV_URI must be a gs:// object - startup.sh fetches it with \`gcloud storage cp\`.
   It holds the deployment's NON-SECRET settings only; a credential in that object is readable by
   everyone who can read the bucket." ;;
esac

# `database-url` IS DELIBERATELY EMPTY unless a deployment states one, and that is a trade-off worth
# naming. startup.sh treats metadata as authoritative for the database endpoint, but a DSN that
# authenticates carries the password - in metadata, in the clear, which is exactly what moving the
# credentials into Secret Manager removed. And a DSN WITHOUT a password is worse than none:
# settings/providers.py makes DATABASE_URL, when set, THE database and ignores the POSTGRES_* parts
# entirely, so a passwordless DSN would override the password the instance just fetched and the
# worker would authenticate with nothing. Empty leaves DATABASE_URL falsy, so the worker composes
# its connection from POSTGRES_* in the env object plus POSTGRES_PASSWORD from Secret Manager - the
# only arrangement where metadata holds no credential and the connection still authenticates.
WORKER_DATABASE_URL="${WORKER_DATABASE_URL:-}"
case "${WORKER_DATABASE_URL}" in
  *"@"*) die "WORKER_DATABASE_URL carries credentials, and instance metadata is readable by anyone
   holding compute.instances.get. Leave it unset: the worker then reads POSTGRES_HOST/PORT/DB/USER
   from the env object and POSTGRES_PASSWORD from Secret Manager." ;;
esac

# The rotation policy is checked HERE, before anything is created: a refusal that arrives after a
# template exists has already left a resource behind for a configuration this run will not apply.
# Step 5 says why these are the values that preserve a warm pool.
if [ "${WORKER_MIG_MIN_REPLICAS}" -ge 1 ] && [ "${WORKER_ROLLING_MAX_UNAVAILABLE}" -gt 0 ]; then
  die "WORKER_ROLLING_MAX_UNAVAILABLE=${WORKER_ROLLING_MAX_UNAVAILABLE} would let the group drop
   below its warm-pool floor of ${WORKER_MIG_MIN_REPLICAS} during a rotation, which is the one thing
   the floor exists to prevent. Leave it at 0 and let maxSurge create the replacement first."
fi
if [ "${WORKER_ROLLING_MAX_SURGE}" -eq 0 ] && [ "${WORKER_ROLLING_MAX_UNAVAILABLE}" -eq 0 ]; then
  die "maxSurge and maxUnavailable are both 0 - a rolling update with neither can never replace an
   instance. Compute Engine rejects it, and so does this."
fi
# substitute needs somewhere to put the replacement, which is the surge; a group configured with no
# surge can only replace an instance in place.
if [ "${WORKER_ROLLING_MAX_SURGE}" -ge 1 ]; then
  REPLACEMENT_METHOD=substitute
else
  REPLACEMENT_METHOD=recreate
fi

STARTUP="${DEPLOY_DIR}/worker/startup.sh"
[ -f "${STARTUP}" ] || die "worker startup script not found: ${STARTUP}"

# Compute Engine is enabled HERE rather than in enable-apis.sh: that list is what EVERY deployment
# needs, and a mesh-only deployment has no fleet at all.
gc services enable compute.googleapis.com \
  || warn "could not enable the Compute Engine API - if it is already on, the steps below still
       work; if it is not, they fail and this is the command:
         gcloud services enable compute.googleapis.com --project ${GCP_PROJECT_ID}"

info "Worker fleet ${WORKER_MIG} in ${WORKER_MIG_ZONE}"
log "image (validated digest): ${APP_IMAGE}"

# 1) the worker identity. The live fleet runs as the project's DEFAULT COMPUTE service account with
#    the cloud-platform scope, which is why that is the fallback and not the recommendation: a named
#    account is what lets the secret grants below be about the workers rather than about every VM in
#    the project. The boundary is IAM on that account, not the access scope - a scope can only
#    narrow what a role already permits, and Secret Manager has no scope narrower than
#    cloud-platform to narrow to.
if [ -n "${WORKER_SERVICE_ACCOUNT:-}" ]; then
  WORKER_SA_EMAIL="${WORKER_SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  if sa_exists "${WORKER_SA_EMAIL}"; then
    log "worker identity ${WORKER_SA_EMAIL} (exists)"
  else
    info "Creating worker runtime identity ${WORKER_SA_EMAIL}"
    gc iam service-accounts create "${WORKER_SERVICE_ACCOUNT}" \
      --display-name "Hexera pipeline worker" \
      || die "could not create ${WORKER_SA_EMAIL}. Creating identities needs
   iam.serviceAccountAdmin, which a DEPLOY identity is deliberately not given. Create it once, as
   an owner:
     gcloud iam service-accounts create ${WORKER_SERVICE_ACCOUNT} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera pipeline worker'"
  fi
else
  require_vars GCP_PROJECT_NUMBER
  WORKER_SA_EMAIL="${GCP_PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  warn "WORKER_SERVICE_ACCOUNT is unset - the fleet will run as the DEFAULT compute account
       ${WORKER_SA_EMAIL}, which every other VM in this project also runs as. Set it to give the
       workers an identity of their own."
fi
WORKER_SCOPES="${WORKER_SCOPES:-https://www.googleapis.com/auth/cloud-platform}"

# 2) the credentials the instance will fetch, BY NAME. Each entry is
#    `METADATA_KEY:ENV_VAR_HOLDING_THE_SECRET_NAME`, and the metadata keys are exactly the four
#    deploy/gcp/worker/startup.sh already reads. A credential this deployment does not use has no
#    key at all - startup.sh treats an absent key as "not used here" and carries on.
WORKER_SECRET_METADATA=()
WORKER_SECRET_NAMES=()
for pair in "postgres-password-secret:POSTGRES_PASSWORD_SECRET" \
            "minio-secret-key-secret:MINIO_SECRET_KEY_SECRET" \
            "deepinfra-api-key-secret:DEEPINFRA_API_KEY_SECRET" \
            "deepseek-api-key-secret:DEEPSEEK_API_KEY_SECRET"; do
  meta_key="${pair%%:*}"
  holder="${pair##*:}"
  secret_name="${!holder:-}"
  [ -n "${secret_name}" ] || continue
  WORKER_SECRET_METADATA+=("${meta_key}=${secret_name}")
  WORKER_SECRET_NAMES+=("${secret_name}")
done

# The grant is PER SECRET, on the identity that opens them, rather than project-wide. A deploy
# identity may not hold secretmanager.admin; the attempt is made, a failure is reported with the
# command that fixes it, and the instance itself is the verdict - startup.sh refuses to start a
# worker whose credential it cannot read, so a missing binding cannot pass as a healthy fleet.
for secret_name in ${WORKER_SECRET_NAMES[@]+"${WORKER_SECRET_NAMES[@]}"}; do
  if gc secrets add-iam-policy-binding "${secret_name}" \
       --member "serviceAccount:${WORKER_SA_EMAIL}" \
       --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then
    log "secret/${secret_name} += roles/secretmanager.secretAccessor -> ${WORKER_SA_EMAIL}"
  else
    warn "could not set IAM on secret ${secret_name}. If the binding is already in place the fleet
       still starts; if it is not, every instance exits at startup and this is the command:
         gcloud secrets add-iam-policy-binding ${secret_name} --project ${GCP_PROJECT_ID} \\
           --member serviceAccount:${WORKER_SA_EMAIL} --role roles/secretmanager.secretAccessor"
  fi
done

# 3) the metadata the instance reads. Names in the clear, credentials never - the values were
#    checked above, before anything was created.
WORKER_METADATA=(
  "worker-image=${APP_IMAGE}"
  "redis-url=${REDIS_URL}"
  "database-url=${WORKER_DATABASE_URL}"
  "env-uri=${WORKER_ENV_URI}"
)
WORKER_METADATA+=(${WORKER_SECRET_METADATA[@]+"${WORKER_SECRET_METADATA[@]}"})

# The metadata list is handed to gcloud with '|' as its delimiter. Neither of the obvious
# separators works: a comma is legal inside a URL, and '@' is in every image digest - passing this
# list comma- or '@'-separated splits `app@sha256:...` into two entries and pins the fleet to
# nothing. A pipe occurs in none of these values, and that is checked rather than assumed.
for entry in "${WORKER_METADATA[@]}"; do
  case "${entry}" in
    *"|"*) die "the metadata entry '${entry%%=*}' has a '|' in its value, which is the delimiter
   this list is passed with. Give it a value without one." ;;
  esac
done

# 4) THE TEMPLATE NAME, and why it is built the way it is. An instance template is immutable, so
#    every change to what a worker runs needs a NEW NAME - and a name that collided with an existing
#    template would leave the group running the old specification while every log line claimed the
#    new one.
#
#    The name therefore carries the image digest, so a new digest always yields a new template, AND
#    a short hash of the whole specification, so a change that is NOT the digest - the machine type,
#    the broker address, a renamed secret container, an edit to startup.sh itself - also rotates the
#    fleet instead of being silently ignored until the next image build. The digest is in the name
#    in the clear because that is the thing an operator reads a template name to learn.
DIGEST="${APP_IMAGE##*@sha256:}"
SPEC_HASH="$(printf '%s\n' "${APP_IMAGE}" "${WORKER_MACHINE_TYPE}" "${WORKER_BOOT_DISK_GB}" \
  "${WORKER_BOOT_DISK_TYPE}" "${WORKER_IMAGE_FAMILY}" "${WORKER_IMAGE_PROJECT}" "${VPC_NETWORK}" \
  "${VPC_SUBNET}" "${WORKER_SA_EMAIL}" "${WORKER_SCOPES}" "${WORKER_METADATA[@]}" \
  | cat - "${STARTUP}" \
  | python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:6])')"
TEMPLATE="${WORKER_TEMPLATE_PREFIX:-${WORKER_MIG}-tpl}-${DIGEST:0:12}-${SPEC_HASH}"
[ ${#TEMPLATE} -le 63 ] \
  || die "instance template name '${TEMPLATE}' is ${#TEMPLATE} characters; Compute Engine allows 63.
   Shorten it with WORKER_TEMPLATE_PREFIX."
[[ "${TEMPLATE}" =~ ^[a-z]([-a-z0-9]*[a-z0-9])?$ ]] \
  || die "instance template name '${TEMPLATE}' is not a valid Compute Engine name (lowercase letter,
   then lowercase letters, digits or hyphens). Set WORKER_TEMPLATE_PREFIX."

if gc compute instance-templates describe "${TEMPLATE}" >/dev/null 2>&1; then
  TEMPLATE_DISPOSITION=reused
  log "template        ${TEMPLATE}  (reused - the specification is unchanged)"
else
  TEMPLATE_DISPOSITION=created
  # REGION-QUALIFIED SUBNET. An instance template is a global resource, so a bare subnet name is
# ambiguous - every region has a `default` - and gcloud refuses it with "Underspecified resource
# [default]". The fleet's subnet is the one in the deployment's own region.
WORKER_SUBNET_REF="projects/${GCP_PROJECT_ID}/regions/${GCP_REGION}/subnetworks/${VPC_SUBNET}"
info "Creating instance template ${TEMPLATE}"
  # --no-address: a worker has no public address and reaches Artifact Registry, Secret Manager and
  # the model providers through Cloud NAT, which is what the live fleet already does.
  gc compute instance-templates create "${TEMPLATE}" \
    --machine-type "${WORKER_MACHINE_TYPE}" \
    --image-family "${WORKER_IMAGE_FAMILY}" \
    --image-project "${WORKER_IMAGE_PROJECT}" \
    --boot-disk-size "${WORKER_BOOT_DISK_GB}GB" \
    --boot-disk-type "${WORKER_BOOT_DISK_TYPE}" \
    --network "${VPC_NETWORK}" \
    --subnet "${WORKER_SUBNET_REF}" \
    --no-address \
    --service-account "${WORKER_SA_EMAIL}" \
    --scopes "${WORKER_SCOPES}" \
    --labels "app=hexera,component=worker,version=0-0-1,deployment-id=${DEPLOYMENT_ID},managed-by=deploy" \
    --metadata-from-file "startup-script=${STARTUP}" \
    --metadata "^|^$(IFS='|'; printf '%s' "${WORKER_METADATA[*]}")"
  log "template        ${TEMPLATE}  (created - ${#WORKER_SECRET_METADATA[@]} secret NAME(s) in metadata, no value)"
fi

# 5) THE GROUP, and the rotation. A group that does not exist is created at its floor. A group that
#    DOES exist is rolled - never deleted, never recreated - so its name, its autoscaler, its
#    instance history and any work in flight all survive the change of digest.
#
#    THE SURGE MATH IS THE WARM-POOL GUARANTEE. maxUnavailable=0 means the group may never take an
#    instance out of service to replace it; maxSurge=1 means it creates the replacement first. A
#    prod pool of N therefore sits at N+1 for the length of the rotation and never at N-1, so the
#    floor that exists to absorb the first job of the day is never absent while a deploy runs.
#    minReady is 180 s because "RUNNING" is not "working": a fresh instance still has to install
#    docker and pull a multi-gigabyte image before it consumes anything from the queue, and without
#    a readiness delay the group would count it as available and delete the instance that actually
#    was.
#
#    PROACTIVE, not opportunistic. An opportunistic update only reaches instances the autoscaler
#    happens to replace, so a warm pool that never scales would never receive a new digest at all -
#    the rotation would silently not happen. The cost is that replacing an instance interrupts
#    whatever it is running; the drain contract in item 6 of the build-out plan (graceful shutdown,
#    lease-aware deletion, scale-in control) is what makes that safe, and until it lands
#    WORKER_ROLLING_TYPE=opportunistic is the deliberate escape hatch for a deploy that must not
#    disturb a long job.
if gc compute instance-groups managed describe "${WORKER_MIG}" --zone "${WORKER_MIG_ZONE}" >/dev/null 2>&1; then
  CURRENT_TEMPLATE_URL="$(gc compute instance-groups managed describe "${WORKER_MIG}" \
    --zone "${WORKER_MIG_ZONE}" --format='value(versions[0].instanceTemplate)' 2>/dev/null || true)"
  [ -n "${CURRENT_TEMPLATE_URL}" ] || CURRENT_TEMPLATE_URL="$(gc compute instance-groups managed describe \
    "${WORKER_MIG}" --zone "${WORKER_MIG_ZONE}" --format='value(instanceTemplate)' 2>/dev/null || true)"
  CURRENT_TEMPLATE="${CURRENT_TEMPLATE_URL##*/}"
  if [ "${CURRENT_TEMPLATE}" = "${TEMPLATE}" ]; then
    MIG_DISPOSITION=reused
    log "fleet           ${WORKER_MIG}  (reused - already on ${TEMPLATE})"
  else
    MIG_DISPOSITION=rolled
    info "Rolling ${WORKER_MIG} from ${CURRENT_TEMPLATE:-<unknown>} onto ${TEMPLATE}"
    gc compute instance-groups managed rolling-action start-update "${WORKER_MIG}" \
      --zone "${WORKER_MIG_ZONE}" \
      --version "template=${TEMPLATE}" \
      --type "${WORKER_ROLLING_TYPE}" \
      --replacement-method "${REPLACEMENT_METHOD}" \
      --max-surge "${WORKER_ROLLING_MAX_SURGE}" \
      --max-unavailable "${WORKER_ROLLING_MAX_UNAVAILABLE}" \
      --min-ready "${WORKER_ROLLING_MIN_READY_SECONDS}s"
    log "fleet           ${WORKER_MIG}  (rolled - surge ${WORKER_ROLLING_MAX_SURGE}, unavailable ${WORKER_ROLLING_MAX_UNAVAILABLE},"
    log "                min-ready ${WORKER_ROLLING_MIN_READY_SECONDS}s, ${WORKER_ROLLING_TYPE}/${REPLACEMENT_METHOD})"
    log "                the roll runs asynchronously; watch it with:"
    log "                gcloud compute instance-groups managed describe ${WORKER_MIG} --zone ${WORKER_MIG_ZONE} --project ${GCP_PROJECT_ID}"
  fi
else
  MIG_DISPOSITION=created
  info "Creating managed instance group ${WORKER_MIG} at its floor of ${WORKER_MIG_MIN_REPLICAS}"
  # The initial size is the FLOOR, not the ceiling: the autoscaler below owns the size from here on,
  # and starting at the floor means a dev group starts at zero and costs nothing until work arrives.
  gc compute instance-groups managed create "${WORKER_MIG}" \
    --zone "${WORKER_MIG_ZONE}" \
    --template "${TEMPLATE}" \
    --size "${WORKER_MIG_MIN_REPLICAS}" \
    --base-instance-name "${WORKER_BASE_INSTANCE_NAME}"
  log "fleet           ${WORKER_MIG}  (created - size ${WORKER_MIG_MIN_REPLICAS}, template ${TEMPLATE})"
fi

# 6) THE AUTOSCALING POLICY. The same shape create-queue-depth-publisher.sh applies, computed from
#    the same variables, so the two writers of this policy converge instead of fighting: the fleet
#    is provisioned before the publisher, and each run leaves the policy identical.
#
#    The filter must select exactly ONE time series - that is the contract
#    single-instance-assignment is defined against - so it names every label that identifies the
#    series the publisher writes. Desired size is then total queue depth divided by the work one
#    instance carries, bounded by the floor and the ceiling.
#
#    A BRAND-NEW FLEET POINTS AT A METRIC WITH NO DATA, because the publisher runs after this. The
#    autoscaler reports CUSTOM_METRIC_INVALID until the first point lands and then clears itself -
#    the live project already went through exactly that once. It holds at the floor meanwhile,
#    which is the correct behaviour for a group nobody has queued work for yet.
FILTER="resource.type = \"generic_task\""
FILTER="${FILTER} AND resource.labels.location = \"${WORKER_MIG_ZONE}\""
FILTER="${FILTER} AND resource.labels.namespace = \"${DEPLOYMENT_ID}\""
FILTER="${FILTER} AND resource.labels.job = \"queue-depth\""
FILTER="${FILTER} AND resource.labels.task_id = \"${QUEUE_NAME}\""
info "Reconciling the autoscaling policy for ${WORKER_MIG}"
gc compute instance-groups managed set-autoscaling "${WORKER_MIG}" \
  --zone "${WORKER_MIG_ZONE}" \
  --min-num-replicas "${WORKER_MIG_MIN_REPLICAS}" \
  --max-num-replicas "${WORKER_MIG_MAX_REPLICAS}" \
  --cool-down-period "${WORKER_MIG_COOLDOWN_SECONDS}" \
  --update-stackdriver-metric "${METRIC}" \
  --stackdriver-metric-filter "${FILTER}" \
  --stackdriver-metric-single-instance-assignment "${WORKER_JOBS_PER_INSTANCE}"

if [ "${WORKER_MIG_MIN_REPLICAS}" -eq 0 ]; then
  FLOOR_STATE="0 - scales to zero (Decision 4: dev pays for nothing while idle)"
else
  FLOOR_STATE="${WORKER_MIG_MIN_REPLICAS} - warm pool, held through every rotation"
fi

log "worker fleet    ${WORKER_MIG}  (${MIG_DISPOSITION})"
log "  template      ${TEMPLATE}  (${TEMPLATE_DISPOSITION})"
log "  identity      ${WORKER_SA_EMAIL}"
log "  image         ${APP_IMAGE}"
log "  floor         ${FLOOR_STATE}"
log "  ceiling       ${WORKER_MIG_MAX_REPLICAS} instances, one per ${WORKER_JOBS_PER_INSTANCE} queued job(s), cooldown ${WORKER_MIG_COOLDOWN_SECONDS}s"
log "  credentials   ${#WORKER_SECRET_METADATA[@]} secret NAME(s) in metadata - the values are fetched per instance"
log "done"
