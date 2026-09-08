#!/usr/bin/env bash
# Responsibility: Stand up the identity GitHub Actions deploys as - a federated pool, one repository-scoped provider, one narrow deployer.
# Owns: the attribute condition that decides WHICH repository may impersonate the deployer, and the four project roles that deployer holds.
# Boundaries: it provisions an identity and nothing else; an existing pool, provider or account is validated and left exactly as it is.

# Create or reconcile WORKLOAD IDENTITY FEDERATION between GitHub Actions and one project.
#
#   GITHUB_REPOSITORY=owner/repo GCP_PROJECT_ID=hexera-prod \
#     bash deploy/gcp/scripts/create-workload-identity.sh
#
# WHY IT EXISTS. .github/workflows/deploy.yml authenticates with no `credentials_json`, no key file
# and no long-lived GitHub secret: GitHub mints a short-lived OIDC token, Google exchanges it
# through the provider created here, and the deployer service account accepts that exchange. There
# is nothing in the repository to leak and nothing to rotate. The alternative - a service-account
# JSON key pasted into a GitHub secret - is the thing item 9 of the build-out plan exists to avoid.
#
# RECONCILING, not recreating. An existing pool, provider or service account is validated and
# reused; nothing here deletes or replaces one. hexera-dev's were created by hand and must survive
# a run of this script unchanged, and hexera-prod - which has none of them - must come out of the
# same script complete.
#
# INPUTS   GCP_PROJECT_ID, GITHUB_REPOSITORY, and the four names below (WIF_POOL, WIF_PROVIDER,
#          DEPLOYER_SERVICE_ACCOUNT are defaulted to what deploy.yml already pins).
# MUTATES  one workload identity pool, one OIDC provider, one service account, four project-level
#          role bindings, and one workloadIdentityUser binding. Nothing else.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

# THE PROJECT, and why the environment beats the file. This runs BEFORE the first deploy of a
# project - on hexera-prod nothing has been discovered yet, so deploy/gcp/generated.env may not
# exist, and where it does exist it is BOUND to hexera-dev (bootstrap-env.sh writes that binding
# into its header). Sourcing it while standing up prod would silently provision dev. So an
# explicit GCP_PROJECT_ID always wins, the generated file is only a fallback, and the project
# NUMBER is read live from the project itself rather than taken from a file that may describe
# another one. enable-apis.sh takes the same fallback for the same reason: neither can require
# discovery to have run first.
_ENV_FILE="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
_PROJECT_OVERRIDE="${GCP_PROJECT_ID:-}"
if [ -z "${_PROJECT_OVERRIDE}" ] && [ -f "${_ENV_FILE}" ]; then
  load_env
fi
GCP_PROJECT_ID="${_PROJECT_OVERRIDE:-${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}}"
export GCP_PROJECT_ID
require_vars GCP_PROJECT_ID

GCP_PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT_ID}" \
  --format='value(projectNumber)' 2>/dev/null || true)"
[ -n "${GCP_PROJECT_NUMBER}" ] || die "cannot read the project number for '${GCP_PROJECT_ID}'.
   A workload identity pool is addressed by project NUMBER, so this is not optional. Check the
   project id and that this account can see it:
     gcloud projects describe ${GCP_PROJECT_ID}"

# The four names, taken from variables so dev and prod differ without editing this file. The
# defaults are exactly what .github/workflows/deploy.yml already pins for both environments -
# provisioning with anything else means editing the workflow's target lines to match.
WIF_POOL="${WIF_POOL:-github-actions}"
WIF_PROVIDER="${WIF_PROVIDER:-github}"
DEPLOYER_SERVICE_ACCOUNT="${DEPLOYER_SERVICE_ACCOUNT:-github-deployer}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"

# GitHub's OIDC issuer. One value, for every repository and every workflow run on github.com -
# which is precisely why the attribute condition below carries the whole restriction.
GITHUB_ISSUER="https://token.actions.githubusercontent.com"

# A pool is a GLOBAL resource; there is no regional form, so the region this deployment runs in
# is irrelevant here.
WIF_LOCATION="global"

# The repository may be derived from the checkout's own origin remote, because that is a fact
# about this tree rather than a guess. It is STATED when it is derived: this one string decides
# who may deploy, so it is never quietly assumed.
if [ -z "${GITHUB_REPOSITORY}" ]; then
  _origin="$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
  case "${_origin}" in
    *github.com[:/]*)
      GITHUB_REPOSITORY="${_origin#*github.com}"
      GITHUB_REPOSITORY="${GITHUB_REPOSITORY#[:/]}"
      GITHUB_REPOSITORY="${GITHUB_REPOSITORY%.git}"
      ;;
  esac
  if [ -n "${GITHUB_REPOSITORY}" ]; then
    log "GITHUB_REPOSITORY was not set - derived '${GITHUB_REPOSITORY}' from the origin remote"
  fi
fi
[[ "${GITHUB_REPOSITORY}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die \
"GITHUB_REPOSITORY must be the owner/repo this project accepts deployments from (got
   '${GITHUB_REPOSITORY}'). It is the only thing standing between this project and every other
   repository on GitHub, so it is never guessed:
     GITHUB_REPOSITORY=owner/repo bash deploy/gcp/scripts/create-workload-identity.sh"

DEPLOYER_SA_EMAIL="${DEPLOYER_SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
POOL_RESOURCE="projects/${GCP_PROJECT_NUMBER}/locations/${WIF_LOCATION}/workloadIdentityPools/${WIF_POOL}"
PROVIDER_RESOURCE="${POOL_RESOURCE}/providers/${WIF_PROVIDER}"
# The principal set is the pool's view of ONE repository: every token whose mapped
# attribute.repository is this repository, and no other principal in the pool.
PRINCIPAL_SET="principalSet://iam.googleapis.com/${POOL_RESOURCE}/attribute.repository/${GITHUB_REPOSITORY}"

# The claims the pool keeps. `google.subject` is required. `attribute.repository` is what the
# binding at the end is keyed on, so without this mapping that binding matches nothing and every
# deploy fails closed. The other two are kept because they are what an audit of a token exchange
# is read with; neither is granted anything.
ATTRIBUTE_MAPPING="google.subject=assertion.sub"
ATTRIBUTE_MAPPING="${ATTRIBUTE_MAPPING},attribute.repository=assertion.repository"
ATTRIBUTE_MAPPING="${ATTRIBUTE_MAPPING},attribute.repository_owner=assertion.repository_owner"
ATTRIBUTE_MAPPING="${ATTRIBUTE_MAPPING},attribute.ref=assertion.ref"

# ─────────────────────────────────────────────────────────────────────────────────────────────
# THE SINGLE MOST IMPORTANT LINE IN THIS FILE.
#
# An OIDC provider with no attribute condition accepts EVERY token its issuer mints - and this
# issuer is github.com's, which mints one for every workflow run in every public and private
# repository on GitHub. Any of them could then exchange that token for a principal inside this
# project's pool, impersonate the deployer below through any binding the pool as a whole holds,
# and mutate this project. The provider is not "ours" because we named it `github`; it is ours
# because of this condition, evaluated at the exchange, before a Google credential exists at all.
ATTRIBUTE_CONDITION="attribute.repository == \"${GITHUB_REPOSITORY}\""
# ─────────────────────────────────────────────────────────────────────────────────────────────

# Set when a resource that already exists is WRONG in a way this script will not correct by
# itself - reconciling means validate and reuse, so an existing provider is reported and never
# rewritten. The run finishes (so the rest is still provisioned) and then exits non-zero, because
# a provider that does not restrict to a repository is a live exposure, not a warning.
RECONCILE_FAILED=0

info "Workload identity federation for ${GITHUB_REPOSITORY} -> ${GCP_PROJECT_ID} (#${GCP_PROJECT_NUMBER})"
log "pool ${WIF_POOL} / provider ${WIF_PROVIDER} / deployer ${DEPLOYER_SA_EMAIL}"

confirm "This grants ${GITHUB_REPOSITORY} the right to deploy to ${GCP_PROJECT_ID} as
  ${DEPLOYER_SA_EMAIL} (run.admin, artifactregistry.writer, iam.serviceAccountUser,
  compute.instanceAdmin). No other repository is accepted."

# The federation APIs. sts.googleapis.com is the one that performs the token exchange and it is
# NOT in enable-apis.sh's list - that list is what every deployment needs, and a deployment driven
# from an operator's machine federates nothing.
gc services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com >/dev/null 2>&1 \
  || warn "could not enable the IAM / STS APIs - if they are already on, everything below still
       works; if they are not, the next step fails and this is the command:
         gcloud services enable iam.googleapis.com iamcredentials.googleapis.com \\
           sts.googleapis.com cloudresourcemanager.googleapis.com --project ${GCP_PROJECT_ID}"

# 1) THE POOL. A deleted pool is not gone - Google keeps it for 30 days and refuses to create
#    another with the same id - so its state is read rather than its mere existence, and the
#    undelete is named instead of a create that would fail with "already exists" on a pool nobody
#    can see.
POOL_STATE="$(gc iam workload-identity-pools describe "${WIF_POOL}" --location "${WIF_LOCATION}" \
  --format='value(state)' 2>/dev/null || true)"
case "${POOL_STATE}" in
  ACTIVE)
    log "pool     ${WIF_POOL}  (reused - untouched)" ;;
  DELETED)
    die "workload identity pool '${WIF_POOL}' exists in the DELETED state. Google keeps a deleted
   pool for 30 days and refuses to create another with the same id. Restore it rather than
   working around it:
     gcloud iam workload-identity-pools undelete ${WIF_POOL} --location=${WIF_LOCATION} --project ${GCP_PROJECT_ID}" ;;
  *)
    info "Creating workload identity pool ${WIF_POOL}"
    gc iam workload-identity-pools create "${WIF_POOL}" \
      --location "${WIF_LOCATION}" \
      --display-name "GitHub Actions" \
      --description "Federated deploy identity for ${GITHUB_REPOSITORY}" >/dev/null
    log "pool     ${WIF_POOL}  (created)" ;;
esac

# 2) THE PROVIDER. Created with the condition, or - when it already exists - CHECKED against what
#    it actually restricts to. hexera-dev's provider was made by hand, so "it exists" says nothing
#    about what it accepts; the three properties that decide that are read back one by one.
#    Each property is read with its own describe rather than one multi-field format string: an
#    attribute condition is CEL and may itself contain the `||` that any separator-joined format
#    would then split on, and a check that can be fooled by the value it is checking is not one.
PROVIDER_CONDITION=""
if gc iam workload-identity-pools providers describe "${WIF_PROVIDER}" \
     --location "${WIF_LOCATION}" --workload-identity-pool "${WIF_POOL}" >/dev/null 2>&1; then
  log "provider ${WIF_PROVIDER}  (reused - untouched)"

  PROVIDER_CONDITION="$(gc iam workload-identity-pools providers describe "${WIF_PROVIDER}" \
    --location "${WIF_LOCATION}" --workload-identity-pool "${WIF_POOL}" \
    --format='value(attributeCondition)' 2>/dev/null || true)"
  PROVIDER_ISSUER="$(gc iam workload-identity-pools providers describe "${WIF_PROVIDER}" \
    --location "${WIF_LOCATION}" --workload-identity-pool "${WIF_POOL}" \
    --format='value(oidc.issuerUri)' 2>/dev/null || true)"
  PROVIDER_MAPPING="$(gc iam workload-identity-pools providers describe "${WIF_PROVIDER}" \
    --location "${WIF_LOCATION}" --workload-identity-pool "${WIF_POOL}" \
    --format='value(attributeMapping)' 2>/dev/null || true)"

  # a) the condition must NAME this repository. An empty condition is the exposure described
  #    above; a condition naming a different repository is a provider for somebody else's deploys.
  case "${PROVIDER_CONDITION}" in
    "")
      warn "provider '${WIF_PROVIDER}' has NO attribute condition. It accepts a token from ANY
       repository on GitHub. Nothing here rewrites an existing provider - apply it yourself:
         gcloud iam workload-identity-pools providers update-oidc ${WIF_PROVIDER} \\
           --location=${WIF_LOCATION} --workload-identity-pool=${WIF_POOL} --project ${GCP_PROJECT_ID} \\
           --attribute-condition='${ATTRIBUTE_CONDITION}'"
      RECONCILE_FAILED=1 ;;
    # MATCHED WITH ITS QUOTES, because a bare substring match reads a LONGER repository name as
    # this one. `hexera-core/hexera-platform` occurs inside `hexera-core/hexera-platform-v2`, so
    # after this repository was renamed the unquoted test reported hexera-prod's provider - still
    # scoped to the old name, and therefore accepting nothing - as correctly restricted. The
    # closing delimiter is what makes the name whole. CEL takes either quote style.
    *"\"${GITHUB_REPOSITORY}\""*|*"'${GITHUB_REPOSITORY}'"*)
      log "  condition restricts to ${GITHUB_REPOSITORY}: ${PROVIDER_CONDITION}" ;;
    *)
      warn "provider '${WIF_PROVIDER}' has an attribute condition that does not mention
       ${GITHUB_REPOSITORY}:
         ${PROVIDER_CONDITION}
       Either this is the wrong provider for this repository, or the condition is wrong. Both are
       decisions for an operator, so nothing was changed."
      RECONCILE_FAILED=1 ;;
  esac

  # b) the issuer must be GitHub's. A provider pointing somewhere else is a different trust
  #    relationship wearing this provider's name.
  if [ "${PROVIDER_ISSUER}" != "${GITHUB_ISSUER}" ]; then
    warn "provider '${WIF_PROVIDER}' trusts issuer '${PROVIDER_ISSUER:-<none>}', not ${GITHUB_ISSUER}"
    RECONCILE_FAILED=1
  fi

  # c) the mapping must keep attribute.repository, because the workloadIdentityUser binding in
  #    step 5 is keyed on it. Without the mapping that binding matches no principal and every
  #    deploy fails at the exchange - which fails closed, so it is a warning and not a refusal.
  case "${PROVIDER_MAPPING}" in
    *attribute.repository=*) : ;;
    *) warn "provider '${WIF_PROVIDER}' does not map attribute.repository (mapping:
       ${PROVIDER_MAPPING:-<none>}). The principal set below is keyed on that attribute, so no
       token will match it and every deploy will fail at the token exchange." ;;
  esac
else
  info "Creating OIDC provider ${WIF_PROVIDER} restricted to ${GITHUB_REPOSITORY}"
  gc iam workload-identity-pools providers create-oidc "${WIF_PROVIDER}" \
    --location "${WIF_LOCATION}" \
    --workload-identity-pool "${WIF_POOL}" \
    --display-name "GitHub Actions OIDC" \
    --issuer-uri "${GITHUB_ISSUER}" \
    --attribute-mapping "${ATTRIBUTE_MAPPING}" \
    --attribute-condition "${ATTRIBUTE_CONDITION}" >/dev/null
  PROVIDER_CONDITION="${ATTRIBUTE_CONDITION}"
  log "provider ${WIF_PROVIDER}  (created - ${ATTRIBUTE_CONDITION})"
fi

# 3) THE DEPLOYER. One account per environment, created here if absent and otherwise left as it
#    is. It is never given a key: the whole point of the pool above is that no key exists.
if sa_exists "${DEPLOYER_SA_EMAIL}"; then
  log "deployer ${DEPLOYER_SA_EMAIL}  (reused - untouched)"
else
  info "Creating deployer identity ${DEPLOYER_SA_EMAIL}"
  gc iam service-accounts create "${DEPLOYER_SERVICE_ACCOUNT}" \
    --display-name "GitHub Actions deployer (${GITHUB_REPOSITORY})" \
    || die "could not create ${DEPLOYER_SA_EMAIL} - creating identities needs
   iam.serviceAccountAdmin. This script is an OWNER's act, run once per project; it is not
   something the deploy identity it creates can do for itself."
  log "deployer ${DEPLOYER_SA_EMAIL}  (created)"
fi

# 4a) THE SECRET-MANAGER ROLE, which is a CUSTOM one because no stock role fits.
#
#    The deploy has to CREATE secret containers and ADD versions to them, and the only stock role
#    that can create a secret is roles/secretmanager.admin - which also carries
#    `secretmanager.versions.access` over every secret in the project. That is how a deploy
#    identity ends up able to read the provider API keys in one call, which is more authority than
#    any stage of deploy.sh uses.
#
#    WHAT IS OMITTED, and why each omission is safe:
#      versions.access   - the ONLY caller is one recovery branch of create-data-tier.sh (a secret
#                          that holds a version whose database user is missing), and that branch
#                          already dies with the exact owner command. The ordinary path never
#                          reads a payload: it asks `versions list` whether one EXISTS.
#      secrets.delete    - nothing in deploy/ deletes a secret.
#      versions.destroy  - nothing in deploy/ destroys a version. A compromised run should not be
#                          able to make a credential unrecoverable.
#
#    WHAT THIS DOES NOT CLAIM. `secrets.setIamPolicy` is kept, because create-secrets.sh grants
#    the runtime identities their accessor bindings - and an identity that may rewrite a secret's
#    IAM policy can grant ITSELF access. This is therefore defence in depth, not a boundary: it
#    turns a silent one-call bulk read into a policy change that is logged and attributable, and
#    it removes destruction outright. The boundary is still the prod environment's reviewers.
SECRET_ROLE_ID="${SECRET_ROLE_ID:-hexeraDeploySecrets}"
SECRET_ROLE_PERMISSIONS="secretmanager.secrets.create,secretmanager.secrets.get,secretmanager.secrets.list,secretmanager.secrets.update,secretmanager.secrets.getIamPolicy,secretmanager.secrets.setIamPolicy,secretmanager.versions.add,secretmanager.versions.get,secretmanager.versions.list,secretmanager.versions.enable,secretmanager.versions.disable"
if _role_state="$(gcloud iam roles describe "${SECRET_ROLE_ID}" --project "${GCP_PROJECT_ID}" \
                    --format='value(deleted)' 2>/dev/null)"; then
  # A role deleted earlier still occupies the id for 7 days and cannot be re-created over.
  if [ "${_role_state}" = "True" ]; then
    info "Undeleting the custom role ${SECRET_ROLE_ID}"
    gcloud iam roles undelete "${SECRET_ROLE_ID}" --project "${GCP_PROJECT_ID}" >/dev/null \
      || die "could not undelete ${SECRET_ROLE_ID} - this needs iam.roles.undelete (an owner's act)"
  fi
  gcloud iam roles update "${SECRET_ROLE_ID}" --project "${GCP_PROJECT_ID}" \
    --permissions "${SECRET_ROLE_PERMISSIONS}" --stage GA >/dev/null \
    || die "could not reconcile the custom role ${SECRET_ROLE_ID} - this needs iam.roles.update (an owner's act)"
  log "secret role     ${SECRET_ROLE_ID}  (reconciled)"
else
  info "Creating the custom role ${SECRET_ROLE_ID} (secret containers, no payload reads)"
  gcloud iam roles create "${SECRET_ROLE_ID}" --project "${GCP_PROJECT_ID}" \
    --title "Hexera deploy - Secret Manager containers" \
    --description "Create secret containers and add versions. Deliberately excludes versions.access, secrets.delete and versions.destroy." \
    --permissions "${SECRET_ROLE_PERMISSIONS}" --stage GA >/dev/null \
    || die "could not create the custom role ${SECRET_ROLE_ID} - this needs iam.roles.create (an owner's act)"
  log "secret role     ${SECRET_ROLE_ID}  (created)"
fi

# 4) THE ROLES, and nothing else at project level. Each is here because a NAMED STAGE of
#    deploy.sh cannot run without it; there is no role in this list that exists "in case".
#
#    THIS LIST GREW ON 2026-09-03, DELIBERATELY, and the trade it makes should be read before it
#    is copied. It used to be four roles, on the principle that a CI identity should not be able
#    to create infrastructure. The consequence was a deploy that could not complete: stage 7
#    (data tier) needs serviceusage to turn its APIs on, servicenetworking to establish the
#    private-services peering, and cloudsql/redis admin to reconcile the two instances - and each
#    refusal surfaced as an opaque gcloud error rather than as the design decision it was. The
#    first hexera-dev release died there, and the first hexera-prod release died one stage later
#    on storage.buckets.create for an exchange bucket that did not exist yet.
#
#    So the deploy identity now provisions the environment it deploys to, end to end. WHAT THAT
#    COSTS, stated plainly: a compromised workflow run can delete this project's database and its
#    broker. What contains it is not the role list any more - it is the `prod` GitHub environment's
#    required reviewers, the provider's repository attribute condition, and Cloud SQL deletion
#    protection (set by create-data-tier.sh on every instance it creates).
#
#    WHAT IS STILL DELIBERATELY ABSENT: no roles/owner, no resourcemanager.projectIamAdmin and no
#    iam.serviceAccountAdmin - so a compromised run still cannot grant itself anything further,
#    mint a new identity, or widen the provider that admitted it.
DEPLOYER_ROLES=(
  roles/run.admin                        # the mesh, migrate and queue-depth jobs, and the API service
  roles/artifactregistry.writer          # release-publish pushes the images validation proved
  roles/iam.serviceAccountUser           # actAs the runtime identities those workloads run as
  roles/compute.instanceAdmin.v1         # the worker MIG and the autoscaling policy that scales it
  roles/compute.networkAdmin             # the private-services range the data tier is addressed from
  roles/servicenetworking.networksAdmin  # the servicenetworking peering that carries that range
  roles/serviceusage.serviceUsageAdmin   # stage 5 and stage 7 turn on the APIs they then call
  roles/cloudsql.admin                   # the Postgres instance, its database and its user
  roles/redis.admin                      # the Memorystore broker
  roles/storage.admin                    # the exchange and artifact buckets, and their lifecycle
  # Secret Manager is NOT roles/secretmanager.admin - see SECRET_ROLE_ID below.
  "projects/${GCP_PROJECT_ID}/roles/${SECRET_ROLE_ID}"
)
info "Project roles for ${DEPLOYER_SA_EMAIL} (${#DEPLOYER_ROLES[@]}, and nothing else)"
for role in "${DEPLOYER_ROLES[@]}"; do
  # --condition=None is explicit rather than implied: on a project that already carries a
  # conditional binding for this member, gcloud otherwise has to ask which one is meant, and
  # these scripts run with prompts disabled.
  # RETRIED, because a service account is not immediately visible to IAM after it is created.
  # On a fresh project this loop runs seconds after `service-accounts create` and the first
  # binding fails with "Service account ... does not exist" - a propagation delay reported as a
  # missing resource, which reads like a bug in the script and is not. Observed on hexera-prod.
  _bound=0
  for _attempt in 1 2 3 4 5 6; do
    if gc projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
         --member "serviceAccount:${DEPLOYER_SA_EMAIL}" \
         --role "${role}" --condition=None >/dev/null 2>&1; then
      _bound=1; break
    fi
    sleep $(( _attempt * 5 ))
  done
  [ "${_bound}" = "1" ] || die "could not grant ${role} to ${DEPLOYER_SA_EMAIL} after 6 attempts.
  If this is a freshly created account the delay is normally under a minute - rerun this script;
  it is idempotent and will reconcile what already exists."
  log "project += ${role} -> ${DEPLOYER_SA_EMAIL}"
done

# What the deployer holds BEYOND those four. Reported, never removed: revoking a binding an
# operator added deliberately is not this script's decision, and a silent revocation during a
# reconcile is how a deploy breaks at 2am. hexera-dev's deployer was made by hand, so this is the
# only place the difference between "narrow by design" and "narrow in fact" is visible.
# The exclusion list is DERIVED from DEPLOYER_ROLES rather than restated: a second copy of the
# list is a copy that stops matching the first time one is edited, and this whole block exists to
# report a difference accurately.
_expected_args=(); for _r in "${DEPLOYER_ROLES[@]}"; do _expected_args+=(-e "${_r}"); done
# The legacy, non-v1 spelling of the instance-admin role is the SAME grant by another name;
# hexera-dev carries it because its deployer was made by hand. Reporting it as unexpected would
# be reporting the naming, not an over-grant.
_expected_args+=(-e roles/compute.instanceAdmin)
EXTRA_ROLES="$(gcloud projects get-iam-policy "${GCP_PROJECT_ID}" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${DEPLOYER_SA_EMAIL}" \
  --format='value(bindings.role)' 2>/dev/null \
  | grep -vxF "${_expected_args[@]}" || true)"
if [ -n "${EXTRA_ROLES}" ]; then
  warn "${DEPLOYER_SA_EMAIL} holds project roles beyond the ${#DEPLOYER_ROLES[@]} above:"
  while IFS= read -r r; do
    if [ -n "${r}" ]; then printf '       %s\n' "${r}" >&2; fi
  done <<<"${EXTRA_ROLES}"
  warn "nothing was removed. Each is authority a compromised workflow run would inherit:
         gcloud projects remove-iam-policy-binding ${GCP_PROJECT_ID} \\
           --member serviceAccount:${DEPLOYER_SA_EMAIL} --role <role>"
  # Named explicitly, because this one is not merely extra - it REVERSES the narrowing above.
  # roles/secretmanager.admin carries versions.access over every secret in the project, so while
  # it stays bound the custom role is decorative and the provider API keys are one call away.
  if printf '%s\\n' "${EXTRA_ROLES}" | grep -qx roles/secretmanager.admin; then
    warn "roles/secretmanager.admin is among them, and it DEFEATS ${SECRET_ROLE_ID}:
         gcloud projects remove-iam-policy-binding ${GCP_PROJECT_ID} \\
           --member serviceAccount:${DEPLOYER_SA_EMAIL} --role roles/secretmanager.admin"
  fi
fi

# 5) THE IMPERSONATION BINDING, granted to ONE repository's principal set rather than to the pool.
#    A binding on the whole pool (principalSet://.../*) would let any repository the provider ever
#    admits become the deployer; this one names the repository, so the restriction survives even a
#    provider whose condition is later loosened.
gc iam service-accounts add-iam-policy-binding "${DEPLOYER_SA_EMAIL}" \
  --member "${PRINCIPAL_SET}" \
  --role roles/iam.workloadIdentityUser --condition=None >/dev/null
log "serviceAccount/${DEPLOYER_SERVICE_ACCOUNT} += roles/iam.workloadIdentityUser -> ${GITHUB_REPOSITORY}"

# 5b) AND EVERY OTHER PRINCIPAL THAT ALREADY HELD IT. Adding the right binding says nothing about
#     what else can impersonate this deployer, and that gap is not hypothetical: after this
#     repository was renamed, hexera-dev's provider condition was correct while its binding still
#     named `hexera-core/hexera-platform-v2` alone - so every branch dispatch failed the exchange,
#     and a run of this script "succeeded" without ever looking. A stale principal set is also a
#     standing grant to whoever creates a repository under that old name in this org.
#
#     REPORTED, NOT REVOKED. Removing an impersonation binding can cut off a deploy path somebody
#     depends on, and this script reconciles rather than rewrites - so the extra members are named
#     with the command that removes them, and the run exits non-zero so nothing reads as clean.
EXTRA_PRINCIPALS="$(gc iam service-accounts get-iam-policy "${DEPLOYER_SA_EMAIL}" \
  --flatten='bindings[].members' \
  --filter='bindings.role=roles/iam.workloadIdentityUser' \
  --format='value(bindings.members)' 2>/dev/null | grep -vFx "${PRINCIPAL_SET}" || true)"
if [ -n "${EXTRA_PRINCIPALS}" ]; then
  MSG="serviceAccount/${DEPLOYER_SERVICE_ACCOUNT} lets principals OTHER than ${GITHUB_REPOSITORY}
       impersonate it:"
  while IFS= read -r principal; do
    [ -n "${principal}" ] || continue
    MSG="${MSG}
         ${principal}"
  done <<<"${EXTRA_PRINCIPALS}"
  MSG="${MSG}
       Each is a separate decision, so nothing was removed. To drop one:
         gcloud iam service-accounts remove-iam-policy-binding ${DEPLOYER_SA_EMAIL} \\
           --project ${GCP_PROJECT_ID} --role roles/iam.workloadIdentityUser --condition=None \\
           --member '<principal>'"
  warn "${MSG}"
  RECONCILE_FAILED=1
fi

# An empty condition is printed as the refusal it is, never as an empty field an eye slides over.
CONDITION_SHOWN="${PROVIDER_CONDITION:-<NONE - this provider accepts every repository on GitHub>}"

printf '\n\033[1m━━━ federated deploy identity ready ━━━\033[0m\n'
cat <<SUMMARY

  Project              ${GCP_PROJECT_ID}  (#${GCP_PROJECT_NUMBER})
  Repository           ${GITHUB_REPOSITORY}   (the only one accepted)
  Pool / provider      ${WIF_POOL} / ${WIF_PROVIDER}
  Issuer               ${GITHUB_ISSUER}
  Attribute condition  ${CONDITION_SHOWN}
  Deployer             ${DEPLOYER_SA_EMAIL}
  Project roles        ${#DEPLOYER_ROLES[@]}, listed in this script - and nothing else

  What .github/workflows/deploy.yml passes to google-github-actions/auth:

    workload_identity_provider: ${PROVIDER_RESOURCE}
    service_account: ${DEPLOYER_SA_EMAIL}

  which is the workflow's own target lines, verbatim:

    echo "wif_provider=${PROVIDER_RESOURCE}"
    echo "deployer_sa=${DEPLOYER_SA_EMAIL}"

  No key was created and none is needed: the repository holds no credential for this project.

  ONE THING THE DEPLOYER CANNOT DO. Enabling APIs needs serviceusage.services.enable, which is
  deliberately not in the four roles - so deploy/gcp/scripts/enable-apis.sh must be run once
  against this project by an owner before the first federated deploy:
    GCP_PROJECT_ID=${GCP_PROJECT_ID} bash deploy/gcp/scripts/enable-apis.sh

SUMMARY

[ "${RECONCILE_FAILED}" = "0" ] || die "the federation exists but does not restrict deploys to
   ${GITHUB_REPOSITORY} - see the warnings above. Everything else was reconciled; nothing was
   rewritten, because correcting an existing provider is an operator's decision."
log "done"
