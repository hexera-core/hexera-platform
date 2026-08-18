#!/usr/bin/env bash
# Responsibility: Provide what every deploy script shares - config loading, assertions and idempotent probes.
# Owns: the rule that a deployment image reference is already a digest, refused here rather than re-resolved.
# Boundaries: sourced, never executed; loading it mutates nothing.

# Shared helpers for the deploy/gcp scripts. SOURCED, never run directly:
#   source "$(dirname "$0")/lib.sh"
# Provides env loading, required-var assertions, derived SA emails, idempotent
# resource helpers, and a confirmation gate. No side effects on source.

# logging
log()  { printf '  %s\n' "$*"; }
info() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# env loading
# Load deploy/gcp/generated.env (or $DEPLOY_ENV_FILE) and EXPORT every var so `envsubst`
# and child `gcloud` calls see them.
# NEVER let gcloud ask. On a project whose APIs are not enabled yet it offers to enable and
# retry, and these scripts route command output away from the terminal - so the question is
# invisible and the run waits on an answer nobody can see. Provisioning enables the APIs it needs
# explicitly, in its own step, which is the only place that decision belongs.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"
export DEPLOY_DIR REPO_ROOT   # used by every sourcing script

load_env() {
  local file="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
  [ -f "${file}" ] || die "generated config '${file}' not found - it is created by discovery; run: make bootstrap"
  set -a
  # shellcheck disable=SC1090
  . "${file}"
  set +a
  # derived values used across scripts
  MESH_SA_EMAIL="${MESH_SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  export MESH_SA_EMAIL
}

# require_vars VAR1 VAR2 ... - die if any is empty/unset.
require_vars() {
  local missing=() name
  for name in "$@"; do
    [ -n "${!name:-}" ] || missing+=("${name}")
  done
  [ ${#missing[@]} -eq 0 ] || die "required config missing: ${missing[*]}"
}

# gcloud with project pinned (never relies on ambient config).
gc() { gcloud --project "${GCP_PROJECT_ID}" "$@"; }

# confirmation gate
# confirm "message" - prompt y/N; ASSUME_YES=1 skips (for CI). Dies on anything else.
confirm() {
  if [ "${ASSUME_YES:-0}" = "1" ]; then
    log "ASSUME_YES=1 - proceeding without prompt"
    return 0
  fi
  local reply
  printf '\n%s\n  type "yes" to proceed: ' "$1"
  read -r reply
  [ "${reply}" = "yes" ] || die "aborted by operator"
}

# idempotent existence checks (read-only)
sa_exists()      { gc iam service-accounts describe "$1" >/dev/null 2>&1; }
bucket_exists()  { gcloud storage buckets describe "gs://$1" >/dev/null 2>&1; }
secret_exists()  { gc secrets describe "$1" >/dev/null 2>&1; }
run_job_exists() { gc run jobs describe "$1" --region "${GCP_REGION}" >/dev/null 2>&1; }
run_svc_exists() { gc run services describe "$1" --region "${GCP_REGION}" >/dev/null 2>&1; }
ar_repo_exists() {
  gc artifacts repositories describe "${ARTIFACT_REGISTRY_REPOSITORY}" \
    --location "${GCP_REGION}" >/dev/null 2>&1
}

# Resolve an image reference to an immutable digest (REGION-docker.pkg.dev/.../img@sha256:..).
# Echoes the digest form; returns non-zero if it cannot be resolved.
# A deployment image reference must ALREADY be an immutable digest, resolved once at PROMOTION
# time from the release record - never re-resolved here from a mutable tag. Re-resolving is what
# let the bytes that deployed drift from the bytes that were validated: between validation and
# rollout, the tag can be moved.
require_digest_reference() {
  local var="$1" value="${2:-}"
  case "${value}" in
    *@sha256:*) return 0 ;;
    "") die "${var} is unset - run scripts/promote-release.sh to write the validated digest" ;;
    *) die "${var}=${value} is a TAG, not a digest.
       Deployment identity must be immutable. Run:
         make release-validate && make release-publish
       then deploy, which promotes the recorded <registry>/<component>@sha256:... reference." ;;
  esac
}

resolve_digest() {
  local image="$1"
  case "${image}" in
    *@sha256:*) printf '%s\n' "${image}"; return 0 ;;  # already pinned
  esac
  gcloud artifacts docker images describe "${image}" \
    --format='value(image_summary.fully_qualified_digest)' 2>/dev/null
}

# The product version, read from the ONE authority: src/meshpipeline/__init__.py. Parsed textually
# so it works without the package installed and without a Python import (this runs on a deploy host,
# before anything is built). pyproject.toml deliberately carries NO static version - it derives one
# from this same attribute - so it cannot be the source here.
product_version() {
  sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
    "${REPO_ROOT}/src/meshpipeline/__init__.py" | head -1
}

# The immutable IMAGE TAG for this source. A git checkout tags by commit SHA; a HISTORY-FREE
# RELEASE ARCHIVE (the supported fresh-organisation install - no .git at all) tags by RELEASE_TAG
# if supplied, else by the product version. Deployment must never REQUIRE .git.
source_tag() {
  local sha ver top
  # The tag must describe THIS tree. `git rev-parse` walks up to an enclosing repository, so a
  # product extracted into a subdirectory of some unrelated checkout would otherwise be tagged
  # with that checkout's commit - a wrong, confidently-reported image tag. Only accept git when
  # the repository it found is rooted here.
  top=""
  if command -v git >/dev/null 2>&1; then
    top="$(git -C "${REPO_ROOT}" rev-parse --show-toplevel 2>/dev/null || true)"
  fi
  if [ -n "${top}" ] && [ "$(cd "${top}" && pwd -P)" = "$(cd "${REPO_ROOT}" && pwd -P)" ] \
     && git -C "${REPO_ROOT}" rev-parse --short=12 HEAD >/dev/null 2>&1; then
    sha="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD)"
    if git -C "${REPO_ROOT}" diff --quiet HEAD 2>/dev/null; then printf '%s\n' "${sha}"
    else printf '%s-dirty\n' "${sha}"; fi
    return 0
  fi
  if [ -n "${RELEASE_TAG:-}" ]; then printf '%s\n' "${RELEASE_TAG}"; return 0; fi
  ver="$(product_version)"
  printf 'release-%s\n' "${ver}"
}

# Render a manifest template through envsubst and echo the path it was written to.
#
# RENDER_DIR chooses where rendered manifests land; it defaults to the deployment's own
# .rendered/ directory, which is what every production path uses. It exists so a caller that
# must not write into the checkout (a sandbox, a read-only tree, a test) can point it elsewhere
# without the renderer knowing who the caller is.
render_manifest() {
  local src="$1" out dir
  dir="${RENDER_DIR:-${DEPLOY_DIR}/.rendered}"
  mkdir -p "${dir}"
  out="${dir}/$(basename "${src}")"
  envsubst < "${src}" > "${out}"
  printf '%s\n' "${out}"
}
