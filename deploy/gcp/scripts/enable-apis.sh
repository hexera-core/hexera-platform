#!/usr/bin/env bash
# Responsibility: Enable the Google APIs the mesh tier needs.
# Boundaries: idempotent - enabling an already-enabled service is a no-op, and nothing else is granted.

# Enable the Google APIs the deployment needs. Idempotent: `services enable` is a no-op when
# already enabled.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
# Enabling needs ONE fact - which project - and discovery needs the APIs to be on before it can
# ask Cloud Run and Cloud Storage what exists. Requiring discovery's output here would make the
# two depend on each other and neither could run first on a blank project. So the project comes
# from the generated config when discovery has already run, and otherwise from the operator's own
# .env or active gcloud configuration.
if [ -f "${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}" ]; then
  load_env
else
  set -a; [ -f "${REPO_ROOT}/.env" ] && . "${REPO_ROOT}/.env"; set +a
  GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
  export GCP_PROJECT_ID
fi
require_vars GCP_PROJECT_ID

APIS=(
  run.googleapis.com
  artifactregistry.googleapis.com
  secretmanager.googleapis.com
  storage.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com          # SignBlob for v4 signed URLs without a key
  cloudresourcemanager.googleapis.com
  iap.googleapis.com                     # Identity-Aware Proxy, the admin console's gate
  compute.googleapis.com                 # the edge: address, NEGs, backend services, URL maps, proxies, certificate
  identitytoolkit.googleapis.com         # Identity Platform, the console's sign-in provider
)

info "Enabling ${#APIS[@]} APIs on ${GCP_PROJECT_ID} (idempotent)"
# ATTEMPT, then VERIFY - rather than assume the attempt was permitted. Enabling a service needs
# serviceusage.services.enable, which the federated deploy identity deliberately does not hold:
# its four roles are run.admin, artifactregistry.writer, iam.serviceAccountUser and
# compute.instanceAdmin, and widening them to cover this one call would hand a CI identity the
# ability to turn on any Google service in the project. So a refusal here is EXPECTED under
# automation and is not, by itself, a failure.
#
# What matters is the end state, not who reached it. If every API is already on - the ordinary
# case, because an owner enables them once per project - the deploy proceeds. Only an API that is
# genuinely off, and that this caller could not turn on, stops the run.
if ! gc services enable "${APIS[@]}" 2>/dev/null; then
  warn "could not enable APIs as $(gcloud config get-value account 2>/dev/null) - checking whether they are already on"
fi

enabled="$(gc services list --enabled --format='value(config.name)' 2>/dev/null || true)"
missing=()
for api in "${APIS[@]}"; do
  printf '%s\n' "${enabled}" | grep -qx "${api}" || missing+=("${api}")
done
if [ "${#missing[@]}" -gt 0 ]; then
  die "these APIs are not enabled on ${GCP_PROJECT_ID} and this account could not enable them:
    $(printf '%s ' "${missing[@]}")
  Run once as a project owner:  gcloud services enable ${missing[*]} --project=${GCP_PROJECT_ID}"
fi
log "done - all ${#APIS[@]} APIs enabled"

# IDENTITY PLATFORM, which the console signs in through. Enabling identitytoolkit.googleapis.com
# (above) is not the same as INITIALISING Identity Platform - that is a one-time console action,
# and the difference is invisible until somebody tries to sign up and the API's token verification
# finds no project. Read-only: this reports, it does not and cannot provision.
#
# This check belongs here rather than in deploy-preflight.sh, where the plan that added it
# (2026-09-10-signup-and-credits) originally asked for it. deploy-preflight.sh does not parse:
# `bash -n deploy/gcp/scripts/deploy-preflight.sh` fails under the bash macOS ships as /bin/bash
# (3.2.57, frozen pre-GPLv3) with "unexpected EOF while looking for matching `)'", because several
# of its `$(... <<'PY' ... PY)` blocks trip a long-standing bash<4 parser defect where a heredoc
# body nested inside a command substitution can desynchronise the substitution's own quote/paren
# tracking - confirmed here with content as small as a single embedded regex literal, and
# confirmed ABSENT under bash 5 (`docker run --rm bash:5 bash -n ...` parses it cleanly). Any
# fix belongs to that pre-existing defect (introduced in #5, unrelated to this plan) and not to
# this task: a check appended to a file that fails to parse would never run under the same shell
# that fails to parse it, on any operator's Mac using stock /bin/bash. This file parses cleanly
# under both, and already runs on every deploy (deploy.sh calls it directly), so the check lives
# here instead.
# `gcloud identity-platform config describe` would say for certain, but that command group is not
# part of the gcloud CLI this repository's tooling installs: `gcloud identity-platform ...` is an
# "Invalid choice" on the stock SDK, and `gcloud alpha identity-platform ...` demands installing
# the alpha component first (verified with `--help` against SDK 582.0.0; neither was invoked - a
# read-only check must not trigger a component install as a side effect). So this is enabled-API
# only, and it cannot confirm initialisation either way - it can only tell the operator to check.
#
# EMITTED AT `info`, NOT `warn`, deliberately. There is no state - not even a correctly
# initialised project with the Email/Password provider enabled - that this check can observe and
# so be silenced by, because the gcloud CLI here cannot observe it at all. A `warn` that fires on
# every single deploy regardless of whether anything is wrong is not a warning; it is noise that
# teaches operators that `warn` in this script means nothing, which is what makes the NEXT one
# invisible. This is a standing note about a manual step, and `info` is what a standing note is.
info "identitytoolkit.googleapis.com is enabled, which is NOT the same as Identity Platform being
    INITIALISED in ${GCP_PROJECT_ID}. This gcloud CLI has no working 'identity-platform' command
    to check that automatically. Until it is initialised - a one-time console action - the console
    renders sign-up and fails on submit. Initialise it once at
      https://console.cloud.google.com/customer-identity/providers?project=${GCP_PROJECT_ID}
    and enable the Email/Password provider."
