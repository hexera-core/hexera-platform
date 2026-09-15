#!/usr/bin/env bash
# Responsibility: Stand up one personal development environment as its own Google Cloud project, from nothing.
# Owns: the slug rule, the project name it produces, and the order the one-time owner acts happen in.
# Boundaries: it prepares a project for the deploy workflow; it deploys nothing and builds no image.
# Collaborates with: enable-apis.sh, create-workload-identity.sh, seed-secrets.sh - each invoked unchanged.

# `make new-env SLUG=pranav` - create hexera-dev-pranav and make it deployable.
#
# WHY A PROJECT PER DEVELOPER, rather than a name prefix inside hexera-dev.
#
# The obvious cheaper design is to put `pranav-api`, `pranav-mesh` and so on beside `dev-api` in
# the shared project, and it was rejected for three reasons that all point the same way:
#
#   TEARDOWN. Deleting a project is one call that removes everything, including the resource
#   somebody added by hand last week that no label names and no script knows about. Deleting a
#   name prefix means enumerating resource types and hoping the list is complete - and the failure
#   mode of an incomplete list is a bill that keeps arriving for an environment its owner believes
#   they destroyed. destroy-env.sh is 80 lines because of this decision; it would have been 400
#   and still not exhaustive.
#
#   BLAST RADIUS. Shared dev's Cloud SQL, its Memorystore, its private-services range and its
#   quota would be shared with every personal environment. A developer testing a migration, or
#   filling the broker, or exhausting an IP range, would be doing it to everyone.
#
#   THE TOOLING ALREADY ASSUMES IT. bootstrap-env.sh defaults DEPLOYMENT_ID to the project id;
#   lib.sh refuses outright to mix one project's identity with another's resource names; and
#   create-workload-identity.sh was written to bring a blank project up to deployable, because
#   hexera-prod once was one. Every provisioning script is create-or-reuse. This script is almost
#   entirely a matter of calling them in the right order.
#
# WHY DEPLOYMENT_ID IS `dev` AND NOT `dev-pranav`. The project is already called hexera-dev-pranav,
# so a per-developer prefix INSIDE it says the same word twice - the exact mistake §2 of
# docs/deployment/environments-and-delivery.md records about `hexera-dev-api`. Keeping the id `dev`
# means every resource in a personal environment has the same name as its counterpart in shared
# dev, so there is one naming convention to learn rather than one per developer, and the 30-character
# service-account limit cannot be reached by picking a long slug.
#
# WHAT THIS IS NOT. It is not idempotent in the way the deploy scripts are - it is RESUMABLE, which
# is not the same claim. Every step tests for what it is about to create and skips it if present,
# so rerunning after a failure continues from where it stopped. But it creates a billable project,
# and that is not something to do by accident, so it confirms before the first mutation.
#
# INPUTS   SLUG (required), GCP_REGION, GCP_BILLING_ACCOUNT, GCP_ORG_ID, SEED_SOURCE_PROJECT.
# MUTATES  one Google Cloud project, its billing link, its enabled APIs, its workload identity
#          federation, its secret containers and their first versions, and one GCS object.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SLUG="${SLUG:-${1:-}}"

# ---------------------------------------------------------------------------------------------
# 1) the slug, and why it is checked this hard
#
# This one string becomes a project id, and a project id cannot be changed after creation and
# cannot be reused for 30 days after deletion. A typo is therefore not a typo - it is a permanent
# resource with the wrong name, and a slot that cannot be reclaimed until the undelete window
# closes. Every rule below is a real Google constraint or a real collision, not house style.
[ -n "${SLUG}" ] || die "no slug.
   The slug names YOUR environment and becomes its project id:
     make new-env SLUG=pranav      ->  hexera-dev-pranav
   Use your first name. It is permanent - a project id cannot be renamed."

PROJECT_PREFIX="${PROJECT_PREFIX:-hexera-dev-}"

# Google's rule: 6-30 characters, lowercase letter first, letters/digits/hyphens, no trailing
# hyphen. The prefix eats 11 of the 30, so the slug gets 19 - checked here rather than discovered
# as an API error after the confirmation prompt has already been answered.
_max=$(( 30 - ${#PROJECT_PREFIX} ))
case "${SLUG}" in
  *[!a-z0-9-]*) die "slug '${SLUG}' has a character a project id may not contain.
   Lowercase letters, digits and hyphens only." ;;
  [!a-z]*)      die "slug '${SLUG}' must start with a lowercase letter." ;;
  *-)           die "slug '${SLUG}' must not end with a hyphen." ;;
esac
[ "${#SLUG}" -le "${_max}" ] || die "slug '${SLUG}' is ${#SLUG} characters; the most that fits is ${_max}.
   '${PROJECT_PREFIX}' plus the slug has to stay inside Google's 30-character project id limit."

# THE TWO NAMES THAT ARE NOT AVAILABLE. `dev` and `prod` would produce hexera-dev-dev and
# hexera-dev-prod, neither of which is the shared environment of that name - which is exactly what
# makes them dangerous. A developer who believes they are working in a personal copy of prod, in a
# project whose name reads like prod, is one confident command away from the wrong conclusion.
case "${SLUG}" in
  dev|prod|production|shared|main|staging)
    die "slug '${SLUG}' is reserved - it names or resembles a shared environment.
   Personal environments are named after a person. Use your own name." ;;
esac

PROJECT_ID="${PROJECT_PREFIX}${SLUG}"
# The prefix and the slug are both constrained above, so this cannot be short; the check costs
# nothing and turns a future prefix change into an error here rather than an API rejection.
[ "${#PROJECT_ID}" -ge 6 ] || die "project id '${PROJECT_ID}' is shorter than Google's 6-character minimum"

# See the header: the project is the isolation boundary, so the deployment id inside it is the
# same `dev` every other development environment uses.
DEPLOYMENT_ID="dev"
GCP_REGION="${GCP_REGION:-us-central1}"
export GCP_PROJECT_ID="${PROJECT_ID}" DEPLOYMENT_ID GCP_REGION

# ---------------------------------------------------------------------------------------------
# 2) the operator, the organisation and the billing account
#
# All three are read from the live session rather than assumed, because every one of them is a way
# for this to create the right-looking project in the wrong place - under no organisation, or on a
# billing account that is not the company's.
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
[ -n "${ACCOUNT}" ] || die "no active gcloud account - run: gcloud auth login"
case "${ACCOUNT}" in
  *.gserviceaccount.com)
    die "this is running as the service account ${ACCOUNT}.
   Creating a project, minting credentials and seeding secret VALUES are owner's acts, performed
   by a person under their own credentials. The federated deploy identity deliberately cannot do
   any of them. Run: gcloud auth login" ;;
esac

if [ -z "${GCP_ORG_ID:-}" ]; then
  _orgs="$(gcloud organizations list --format='value(ID)' 2>/dev/null || true)"
  _n="$(printf '%s' "${_orgs}" | grep -c . || true)"
  [ "${_n}" -eq 1 ] || die "expected exactly one organisation, found ${_n}.
   Name the one to create this project under:
     GCP_ORG_ID=<id> make new-env SLUG=${SLUG}"
  GCP_ORG_ID="$(printf '%s' "${_orgs}" | head -1)"
fi

if [ -z "${GCP_BILLING_ACCOUNT:-}" ]; then
  # OPEN accounts only. A closed account links without complaint and then refuses every billable
  # resource, so the failure would land eight steps later on `gcloud sql instances create`.
  _bill="$(gcloud billing accounts list --filter='open=true' --format='value(ACCOUNT_ID)' 2>/dev/null || true)"
  _n="$(printf '%s' "${_bill}" | grep -c . || true)"
  [ "${_n}" -eq 1 ] || die "expected exactly one OPEN billing account, found ${_n}.
   Name the one to charge this environment to:
     GCP_BILLING_ACCOUNT=<id> make new-env SLUG=${SLUG}"
  GCP_BILLING_ACCOUNT="$(printf '%s' "${_bill}" | head -1)"
fi

GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"
if [ -z "${GITHUB_REPOSITORY}" ]; then
  _origin="$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
  case "${_origin}" in
    *github.com[:/]*)
      GITHUB_REPOSITORY="${_origin#*github.com}"
      GITHUB_REPOSITORY="${GITHUB_REPOSITORY#[:/]}"
      GITHUB_REPOSITORY="${GITHUB_REPOSITORY%.git}" ;;
  esac
fi
[ -n "${GITHUB_REPOSITORY}" ] || die "cannot tell which GitHub repository may deploy to this project.
   It decides who can impersonate the deploy identity, so it is never guessed:
     GITHUB_REPOSITORY=owner/repo make new-env SLUG=${SLUG}"

# ---------------------------------------------------------------------------------------------
# 3) the plan, stated in full before anything is created
info "Personal development environment: ${SLUG}"
cat <<PLAN

  Project            ${PROJECT_ID}       (new, under organisation ${GCP_ORG_ID})
  Deployment id      ${DEPLOYMENT_ID}                 (resources are named dev-api, dev-mesh, ... as in shared dev)
  Region             ${GCP_REGION}
  Billing account    ${GCP_BILLING_ACCOUNT}
  Deployable by      ${GITHUB_REPOSITORY}
  Owner performing   ${ACCOUNT}
  Provider keys      copied from ${SEED_SOURCE_PROJECT:-hexera-dev}

  This CREATES A BILLABLE PROJECT and prepares it to be deployed into. It provisions no
  database, no broker and no service - the deploy workflow does that, and this project costs
  nothing until it runs.

  A project id CANNOT BE RENAMED, and cannot be reused for 30 days after deletion.

PLAN
confirm "Create ${PROJECT_ID}?"

# ---------------------------------------------------------------------------------------------
# 4) the project
#
# Probed first: `projects create` on an existing id fails with ALREADY_EXISTS, which for a
# RESUMED run is the expected state and not an error. A project that exists but belongs to
# another organisation is a different matter and is refused - it is somebody else's.
if gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  _parent="$(gcloud projects describe "${PROJECT_ID}" --format='value(parent.id)' 2>/dev/null || true)"
  [ "${_parent}" = "${GCP_ORG_ID}" ] || die "project ${PROJECT_ID} already exists under parent '${_parent}',
   not organisation ${GCP_ORG_ID}. Refusing to adopt a project this run did not create."
  log "project         ${PROJECT_ID}  (reused - untouched)"
else
  info "Creating project ${PROJECT_ID}"
  # THE DISPLAY NAME HAS ITS OWN RULES, and they are NOT the project id's. It permits letters,
  # digits, single quotes, hyphens, spaces and exclamation points, in 4-30 characters - and
  # nothing else. Parentheses are rejected, which is not documented anywhere a reader would look
  # and which fails as `INVALID_ARGUMENT: field [display_name] has issue`, an error that names the
  # field but not the character. So: no punctuation beyond a space.
  #
  # The length works out because the slug is already bounded above: "Hexera dev " is 11 characters
  # and the slug can be at most ${_max}, which is exactly the 30 this field allows. That is a
  # coincidence of the two limits rather than a margin, so a longer PROJECT_PREFIX would need this
  # rechecked.
  gcloud projects create "${PROJECT_ID}" \
    --organization "${GCP_ORG_ID}" \
    --name "Hexera dev ${SLUG}" \
    --labels "app=hexera,environment=dev,personal=true,owner-slug=${SLUG}" >/dev/null \
    || die "could not create ${PROJECT_ID} - gcloud's own error is immediately above this, and it
   names the actual cause. The usual ones, in the order worth checking:
     * the id is taken, or was deleted inside the last 30 days and is still reserved
     * this account lacks resourcemanager.projectCreator on organisation ${GCP_ORG_ID}
     * the organisation is at its project QUOTA:
         gcloud alpha resource-manager quotas list --organization ${GCP_ORG_ID}"
  log "project         ${PROJECT_ID}  (created)"
fi

GCP_PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
[ -n "${GCP_PROJECT_NUMBER}" ] || die "cannot read the project number for ${PROJECT_ID}"
export GCP_PROJECT_NUMBER
log "project number  ${GCP_PROJECT_NUMBER}"

# ---------------------------------------------------------------------------------------------
# 5) billing, BEFORE the APIs
#
# Enabling a service on a project with no billing account fails, and the error names the service
# rather than the billing link - so the order here is what makes the next step's failures mean
# what they say.
_linked="$(gcloud billing projects describe "${PROJECT_ID}" \
             --format='value(billingAccountName)' 2>/dev/null || true)"
if [ -n "${_linked}" ]; then
  log "billing         ${_linked}  (already linked)"
else
  info "Linking ${PROJECT_ID} to billing account ${GCP_BILLING_ACCOUNT}"
  gcloud billing projects link "${PROJECT_ID}" --billing-account "${GCP_BILLING_ACCOUNT}" >/dev/null \
    || die "could not link billing. This needs billing.user on the account (or billing.admin)."
  log "billing         ${GCP_BILLING_ACCOUNT}  (linked)"
fi

# ---------------------------------------------------------------------------------------------
# 6) the APIs, the deploy identity, and the object the worker fleet reads
#
# Each of these is an existing authority invoked unchanged. Nothing about what they do is restated
# here; the only thing this script contributes is the project they are pointed at.
info "Enabling the Google APIs this environment needs"
bash "${DEPLOY_DIR}/scripts/enable-apis.sh"

# WORKLOAD IDENTITY FEDERATION - the standard roster, with nothing added.
#
# The deploy identity this creates holds exactly what it holds in hexera-dev and hexera-prod. A
# personal environment is a sandbox, but it is not a place to practise a looser security posture:
# the whole point of testing a deploy here is that it exercises the same identity, with the same
# authority, that the shared environments' deploys run under. Anything this identity cannot do in
# a personal project is something a deploy could not do in prod either, which is what makes a
# personal environment a useful rehearsal rather than a more permissive one.
info "Workload identity federation for ${GITHUB_REPOSITORY}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY}" \
  bash "${DEPLOY_DIR}/scripts/create-workload-identity.sh"

# THE OBJECT STORE, established HERE rather than by the first deploy - and this is the one step
# whose placement is a real design decision rather than an ordering detail.
#
# create-object-storage.sh mints the HMAC key the API and the worker fleet authenticate to Cloud
# Storage with. Minting needs storage.hmacKeyAdmin, which the deploy identity is deliberately not
# given, so in every environment that key is minted ONCE by an owner and its ACCESS ID - the public
# half - is then pinned as a deploy target. Shared dev and prod pin theirs as literals in
# deploy.yml. A personal environment cannot: the value does not exist until the environment does,
# and pinning it in a commit would mean a code review before anybody could create one.
#
# So the owner mints it here, at creation, and the access id joins the project number in the
# register the picker reads. The alternative - letting each deploy discover it - is worse than it
# sounds: with no recorded access id, create-object-storage.sh concludes that no usable key exists
# and mints ANOTHER one, every run, until the account hits Google's five-key limit and the deploy
# stops dead. Establishing it once is what makes every later deploy a reuse.
#
# Run through a TEMPORARY env file. These scripts read their configuration from a generated file,
# and the one at deploy/gcp/generated.env belongs to whatever environment this developer last
# worked on - overwriting it as a side effect of creating a different environment is exactly the
# class of mix-up lib.sh's project check exists to catch.
# A temporary DIRECTORY, and the env file is a path INSIDE it that does not exist yet. `mktemp`
# alone would create the file, and bootstrap-env.sh branches on whether its target EXISTS: an
# empty-but-present file sends it down the "reuse what is already configured" path, where it
# sources nothing, discovers nothing, and then measures the environment it was handed against a
# file that describes no project at all.
OBJECT_STORE_DIR="$(mktemp -d)"
OBJECT_STORE_ENV="${OBJECT_STORE_DIR}/generated.env"
trap 'rm -rf "${OBJECT_STORE_DIR}"' EXIT
info "Discovering ${PROJECT_ID}"
DEPLOY_ENV_FILE="${OBJECT_STORE_ENV}" bash "${DEPLOY_DIR}/scripts/bootstrap-env.sh" >/dev/null \
  || die "could not discover ${PROJECT_ID}. Every step above succeeded, so this is recoverable -
   rerun this script; it resumes from where it stopped."

# THE IMAGE REPOSITORY, and it has to exist BEFORE the first deploy rather than during it.
#
# create-artifact-registry.sh runs as stage 6 of deploy.sh - inside the `provision` job, which is a
# whole job AFTER `release`. That ordering is harmless in an environment whose repository already
# exists, which is every environment that has ever deployed. On a brand new project it is not: the
# release job's `release-publish` would `docker push` into a repository nothing has created, and
# the very first deploy of every personal environment would fail on that push.
#
# This is the same order deploy/gcp's own `publication-target` target uses (enable-apis, bootstrap,
# validate, artifact-registry) and for the same reason - it exists because a blank project cannot
# be published to before it has somewhere to publish.
info "Artifact Registry - where this environment's images are pushed"
DEPLOY_ENV_FILE="${OBJECT_STORE_ENV}" bash "${DEPLOY_DIR}/scripts/create-artifact-registry.sh" \
  || die "could not create the Artifact Registry repository in ${PROJECT_ID}.
   Without it the first deploy fails when it pushes. Rerun this script; it is resumable."

info "Object store: bucket, identity and the HMAC key this environment authenticates with"
DEPLOY_ENV_FILE="${OBJECT_STORE_ENV}" bash "${DEPLOY_DIR}/scripts/create-object-storage.sh" \
  || die "could not establish the object store for ${PROJECT_ID}.
   Minting the key is an OWNER's act and you are running as one, so this is not a permissions
   dead end - rerun this script; every step is resumable."

# Read back from the file the script wrote, rather than re-derived: the access id is whatever
# Google assigned, and the bucket name is whatever create-object-storage.sh actually used.
# shellcheck disable=SC1090
MINIO_ACCESS_KEY="$(sed -n 's/^MINIO_ACCESS_KEY=//p' "${OBJECT_STORE_ENV}" | tail -1)"
MINIO_BUCKET="$(sed -n 's/^MINIO_BUCKET=//p' "${OBJECT_STORE_ENV}" | tail -1)"
[ -n "${MINIO_ACCESS_KEY}" ] || die "the object store was established but recorded no MINIO_ACCESS_KEY.
   Without it every later deploy would mint a new key. Rerun this script."
log "object store    ${MINIO_BUCKET}"
log "access id       ${MINIO_ACCESS_KEY}  (the PUBLIC half; its secret is in Secret Manager)"

# THE WARM BUILD CACHE, read-only and across projects.
#
# devtools/release/validate.sh exports its Docker layer cache to the target environment's own
# Artifact Registry, so a NEW environment's cache is empty and its first build would re-download
# the whole native floor - the exact half-hour wait that stops people creating environments. This
# grant lets this project's deploy identity READ shared dev's cache, so the first build in a
# ten-minute-old environment starts warm and fills its own cache as it goes.
#
# READER, on one repository, in one direction. It confers no write, nothing outside that
# repository, and nothing on the personal project in return. A cache source that cannot be read is
# skipped by buildx rather than failing the build, so if this grant is refused the environment is
# still perfectly usable - it just builds slowly the first time.
CACHE_SOURCE_PROJECT="${CACHE_SOURCE_PROJECT:-hexera-dev}"
DEPLOYER_SA_EMAIL="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud artifacts repositories add-iam-policy-binding mesh \
     --project "${CACHE_SOURCE_PROJECT}" --location "${GCP_REGION}" \
     --member "serviceAccount:${DEPLOYER_SA_EMAIL}" \
     --role roles/artifactregistry.reader >/dev/null 2>&1; then
  log "build cache     read on ${CACHE_SOURCE_PROJECT}/mesh -> ${DEPLOYER_SA_EMAIL}"
else
  warn "could not grant read on ${CACHE_SOURCE_PROJECT}'s registry to ${DEPLOYER_SA_EMAIL}.
       The environment works; its FIRST image build just will not start from a warm cache.
       To fix it later:
         gcloud artifacts repositories add-iam-policy-binding mesh \\
           --project ${CACHE_SOURCE_PROJECT} --location ${GCP_REGION} \\
           --member serviceAccount:${DEPLOYER_SA_EMAIL} --role roles/artifactregistry.reader"
fi

# THE WORKER FLEET'S NON-SECRET SETTINGS. create-worker-fleet.sh requires WORKER_ENV_URI and
# refuses to run without it, so a personal environment that never got this object could provision
# everything except the thing that actually executes jobs.
#
# The content is .env.example, which is not a stand-in for the real settings - it is GENERATED from
# src/meshpipeline/settings/inventory.py, the one authority on what a setting is and what its
# default is, and it is what shared dev's own worker.env was made from. It carries policy, model
# routing and limits and NO credential, which is required rather than incidental: startup.sh
# fetches this object onto the instance, and both a bucket object and instance metadata are
# readable by anyone who can describe the instance. Endpoints and credentials are injected at boot
# from metadata and Secret Manager respectively.
TRANSFER_BUCKET="${TRANSFER_BUCKET:-${DEPLOYMENT_ID}-transfer-${GCP_PROJECT_NUMBER}}"
WORKER_ENV_URI="gs://${TRANSFER_BUCKET}/worker.env"
if bucket_exists "${TRANSFER_BUCKET}"; then
  log "transfer bucket ${TRANSFER_BUCKET}  (reused)"
else
  info "Creating the transfer bucket ${TRANSFER_BUCKET}"
  # Uniform access, no public access: this object is readable by the fleet's identity, not by the
  # internet. Private by construction rather than by a later correction.
  gcloud storage buckets create "gs://${TRANSFER_BUCKET}" \
    --project "${PROJECT_ID}" --location "${GCP_REGION}" \
    --uniform-bucket-level-access --public-access-prevention >/dev/null \
    || die "could not create gs://${TRANSFER_BUCKET}"
  log "transfer bucket ${TRANSFER_BUCKET}  (created)"
fi
info "Publishing the worker settings object"
gcloud storage cp "${REPO_ROOT}/.env.example" "${WORKER_ENV_URI}" --project "${PROJECT_ID}" --quiet \
  || die "could not write ${WORKER_ENV_URI}"
log "worker settings ${WORKER_ENV_URI}"

# ---------------------------------------------------------------------------------------------
# 6b) the register the deploy workflow resolves this environment through
#
# WHY ANYTHING HAS TO BE REGISTERED AT ALL. A workload identity pool is addressed by project
# NUMBER, and Google assigns that at creation - it cannot be derived from the project id. The
# workflow's target picker runs before any authentication (deliberately: it holds no id-token
# permission, so the job that chooses a deploy target cannot mint a credential for one), so it
# cannot look the number up. One repository variable carries `slug=projectNumber:accessId` for
# every personal environment, and the picker reads this one.
#
# THE SECOND FIELD is the object store's ACCESS ID, which is the public half of the HMAC key
# minted above - the same value deploy.yml pins in the clear for shared dev and prod. It travels
# with the project number because it has the same problem: it does not exist until the
# environment does, and a deploy that cannot state it mints a new key every run.
#
# NO CREDENTIAL IS IN IT, and that is what makes a repository variable acceptable here when the workflow
# rejects them for every other target. A slug that is not in this register does not deploy; a slug
# that is deploys to the project its own name derives. Editing this variable cannot point a deploy
# at a project some slug does not already name, and it cannot reach prod, which is tag-only.
#
# BEST EFFORT, BY DESIGN. `gh` may be absent or unauthenticated on a machine that is otherwise
# perfectly able to create a project, and failing the whole run at this point would mean tearing
# down a finished environment over a CLI that is not installed. The command to run by hand is
# printed instead.
VAR_NAME="HEXERA_PERSONAL_ENVS"
register_environment() {
  local current merged
  command -v gh >/dev/null 2>&1 || return 1
  gh auth status >/dev/null 2>&1 || return 1
  current="$(gh variable get "${VAR_NAME}" --repo "${GITHUB_REPOSITORY}" 2>/dev/null || true)"
  # Rebuilt rather than appended: appending a second `pranav=` line would leave two entries for
  # one environment, and the picker reads the first it finds - so a re-created environment would
  # resolve to the number of the one it replaced. Dropping any existing entry for this slug first
  # makes re-registration idempotent.
  merged="$(printf '%s' "${current}" | tr ', ' '\n' | grep -v "^${SLUG}=" | grep . || true)"
  merged="$(printf '%s\n%s=%s:%s\n' "${merged}" "${SLUG}" "${GCP_PROJECT_NUMBER}" \
              "${MINIO_ACCESS_KEY}" | grep . | sort)"
  printf '%s' "${merged}" | gh variable set "${VAR_NAME}" --repo "${GITHUB_REPOSITORY}" --body-file - 2>/dev/null
}
info "Registering ${SLUG} so the deploy workflow can resolve it"
if register_environment; then
  log "${VAR_NAME}   ${SLUG}=${GCP_PROJECT_NUMBER}:${MINIO_ACCESS_KEY}  (registered)"
else
  warn "could not write the ${VAR_NAME} repository variable - the environment is provisioned but
       the deploy workflow cannot resolve '${SLUG}' until this is set. Either install and
       authenticate the GitHub CLI and rerun this script, or add the pair by hand at
         https://github.com/${GITHUB_REPOSITORY}/settings/variables/actions
       appending this line to ${VAR_NAME}:
         ${SLUG}=${GCP_PROJECT_NUMBER}:${MINIO_ACCESS_KEY}"
fi

# ---------------------------------------------------------------------------------------------
# 7) the credential values
#
# Last, because it is the only step that can legitimately leave the environment incomplete: a
# provider key that could not be copied is reported and the run exits non-zero, with everything
# else already in place. Ordering it here means that outcome is one command away from being fixed
# rather than a reason to start over.
_seed_rc=0
bash "${DEPLOY_DIR}/scripts/seed-secrets.sh" || _seed_rc=$?

# ---------------------------------------------------------------------------------------------
info "Environment ${SLUG} is ready to deploy"
cat <<NEXT

  Project     ${PROJECT_ID}  (${GCP_PROJECT_NUMBER})
  Console     https://console.cloud.google.com/home/dashboard?project=${PROJECT_ID}

  DEPLOY INTO IT - from any branch, as often as you like:

    gh workflow run deploy.yml --ref "\$(git branch --show-current)" \\
      -f slug=${SLUG} -f images=true -f data=true -f storage=true -f migrate=true \\
      -f queue=true -f workers=true -f console=true

  The first run creates Cloud SQL and Memorystore and takes roughly half an hour. Every run
  after that skips them - leave \`data\` unticked and it is images, schema and a rollout.

  DESTROY IT when you are done with it:

    make destroy-env SLUG=${SLUG}

NEXT

if [ "${_seed_rc}" -ne 0 ]; then
  warn "The environment is provisioned but at least one provider key is EMPTY - see the seeding
   summary above. Jobs will fail on their first model call until it holds a value."
  exit "${_seed_rc}"
fi
