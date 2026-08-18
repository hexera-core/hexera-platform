#!/usr/bin/env bash
# Responsibility: Prepare a fresh clone for local development in one idempotent command.
# Boundaries: it creates .env only when absent and never edits one; it reads no setting and starts nothing.

# `make setup` - the ONE supported way to prepare a fresh clone for local development.
#
# Idempotent and safe to rerun. It detects host tools, seeds .env from the tracked template the
# first time, builds a Python virtualenv with the full pinned toolchain, prepares the local
# directories the stack bind-mounts, validates Docker, and builds the container images.
#
# It is configuration-independent: every runtime value may still be blank when it finishes. What
# those values must be is `make dev-up`'s question, and only its. Setup needs no credential, no
# cloud identity and no network beyond the package index, so it cannot fail for want of either.
#
# Database migrations apply automatically on `make dev-up` (the API container runs
# `alembic upgrade head` on start).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"
VENV="${ROOT}/.venv"

# output helpers
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; Z=$'\033[0m'
else B=""; G=""; Y=""; R=""; Z=""; fi
step() { printf '\n%s━━━ %s%s\n' "${B}" "$1" "${Z}"; }
ok()   { printf '  %sok%s   %s\n' "${G}" "${Z}" "$1"; }
warn() { printf '  %s!!%s   %s\n' "${Y}" "${Z}" "$1"; }
die()  { printf '\n%sSETUP STOPPED:%s %s\n' "${R}" "${Z}" "$1" >&2; exit 1; }

# 1. host tools
step "Checking host tools"
MISSING=()
have() { command -v "$1" >/dev/null 2>&1; }

# Python: the pinned wheels (vtk, cadquery-ocp) are proven on 3.11/3.12. Prefer those; accept
# a newer python3 with a warning rather than blocking.
PYTHON=""
for cand in python3.12 python3.11 python3; do
  if have "${cand}"; then PYTHON="${cand}"; break; fi
done
if [ -z "${PYTHON}" ]; then MISSING+=("python3 (>=3.11)"); else
  PYV="$(${PYTHON} -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
  case "${PYV}" in
    3.11|3.12) ok "python ${PYV} (${PYTHON})" ;;
    3.13|3.14) warn "python ${PYV} - the pinned wheels are proven on 3.11/3.12; install python3.12 if a heavy wheel fails to build" ; ok "using ${PYTHON}" ;;
    *) die "python ${PYV} is too old - this project needs >=3.11 (install python3.12)" ;;
  esac
fi
have git    && ok "git"    || MISSING+=("git")
have docker && ok "docker" || MISSING+=("docker")
if docker compose version >/dev/null 2>&1; then ok "docker compose (v2 plugin)"
elif have docker-compose;                   then ok "docker-compose (v1)"
else MISSING+=("docker compose (the Compose v2 plugin)"); fi

# The Google Cloud CLI is a HOST application, not a Python dependency: provisioning shells out to
# it. Installing it is OPTIONAL and BEST EFFORT - every step below runs in a subshell whose failure
# is a warning, never fatal, because a machine's package manager is not this script's to repair and
# a setup that cannot install one optional tool must still produce .env and the virtualenv.
# Docker is only ever reported: this repository owns no tested Docker installation authority.
_install_gcloud() (
  set +e
  # apt refuses to do anything while other packages are half-configured, and it would try to
  # finish them as part of our transaction - so somebody else's broken kernel module becomes our
  # failure. Detect that first and stay out of it entirely.
  broken="$(dpkg -l 2>/dev/null | awk '$1 ~ /^i[^i]/ {print $2}')"
  if [ -n "${broken}" ]; then
    warn "apt has packages pending configuration, so this script will not run apt:"
    for b in ${broken}; do warn "    ${b}"; done
    warn "  Those are unrelated to this project. Either fix them (sudo dpkg --configure -a),"
    warn "  or install the CLI without apt:"
    warn "    curl https://sdk.cloud.google.com | bash"
    return 1
  fi
  # Only what is genuinely absent. On a normal Ubuntu these are already present, and installing
  # them regardless is what turns an optional step into a package-manager transaction.
  missing=""
  for pkg in apt-transport-https ca-certificates gnupg curl; do
    dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q "^install ok installed$" || missing="${missing} ${pkg}"
  done
  if [ -n "${missing}" ]; then
    sudo apt-get update -qq || return 1
    # shellcheck disable=SC2086
    sudo apt-get install -y -qq ${missing} || return 1
  fi
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg || return 1
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null || return 1
  sudo apt-get update -qq || return 1
  sudo apt-get install -y -qq google-cloud-cli || return 1
)

if have gcloud; then
  ok "gcloud (present - setup never runs it; authentication is your own act)"
elif [ "${SKIP_GCLOUD_INSTALL:-0}" = "1" ]; then
  warn "gcloud not found - skipping installation (SKIP_GCLOUD_INSTALL=1)."
elif [ -r /etc/os-release ] && . /etc/os-release 2>/dev/null && \
     { [ "${ID:-}" = "ubuntu" ] || [ "${ID_LIKE:-}" = "debian" ] || [ "${ID:-}" = "debian" ]; }; then
  printf '\n  The Google Cloud CLI is not installed. It is required by "make mesh-setup"\n'
  printf '  (provisioning) and by "make mesh-doctor". Setup finishes either way.\n\n'
  printf '  Install it now from Google'"'"'s official signed apt repository? [y/N] '
  if [ "${ASSUME_YES:-0}" = "1" ]; then REPLY_GC=y; printf 'y (ASSUME_YES)\n'; else read -r REPLY_GC; fi
  case "${REPLY_GC}" in
    y|Y|yes|YES)
      if _install_gcloud && have gcloud; then
        ok "gcloud installed"
      else
        warn "the Google Cloud CLI was not installed - setup continues without it."
        warn "  Install it yourself before 'make mesh-setup':"
        warn "    curl https://sdk.cloud.google.com | bash      (no root, no apt)"
        warn "    https://cloud.google.com/sdk/docs/install"
      fi
      ;;
    *) warn "skipped - install it before 'make mesh-setup': https://cloud.google.com/sdk/docs/install" ;;
  esac
else
  warn "gcloud not found, and this is not a Debian/Ubuntu host - install it yourself before"
  warn "  'make mesh-setup':  https://cloud.google.com/sdk/docs/install"
fi

if [ "${#MISSING[@]}" -gt 0 ]; then
  printf '\n'; for m in "${MISSING[@]}"; do printf '  %smissing%s  %s\n' "${R}" "${Z}" "${m}"; done
  die "install the tools above, then rerun: make setup"
fi

# 2. local configuration (.env)
#
# .env is the developer's file and this is the only line in the repository that writes it: a copy
# of the tracked template, made once, when there is none. Nothing here - and nothing in
# provisioning or deployment - ever edits it again, so a value you set cannot be reverted by a
# tool. Setup does not read it either; it never needs to know what is in it.
step "Local configuration (.env)"
ENV_CREATED=0
if [ -f "${ROOT}/.env" ]; then
  ok ".env present - left exactly as it is"
elif [ -f "${ROOT}/.env.example" ]; then
  cp "${ROOT}/.env.example" "${ROOT}/.env"
  ENV_CREATED=1
  ok ".env created from .env.example - every value is still the default"
else
  die "no .env and no .env.example at the repository root.
       .env.example is tracked; restore it, or regenerate it once the venv exists with:
         .venv/bin/python -m meshpipeline.settings.inventory > .env.example"
fi

# 3. Python virtualenv (create or reuse)
step "Python virtualenv (.venv)"
if [ -x "${VENV}/bin/python" ]; then ok "reusing existing .venv"
else
  "${PYTHON}" -m venv "${VENV}"
  ok "created .venv (${PYV})"
fi
VPY="${VENV}/bin/python"
"${VPY}" -m pip install --quiet --upgrade pip >/dev/null
ok "pip up to date"

# 4. install the pinned toolchain + the package (editable)
step "Installing dependencies (requirements/runtime.txt + requirements/dev.txt)"
echo "  this can take a few minutes on the first run (vtk/pyvista/cadquery are large)…"
"${VPY}" -m pip install --quiet -c requirements/constraints.txt -r requirements/runtime.txt -r requirements/dev.txt
ok "runtime + dev dependencies installed"
"${VPY}" -m pip install --quiet -e . --no-deps
"${VPY}" -c "import meshpipeline" || die "the package does not import after install - see the error above"
ok "meshpipeline installed (editable) - import verified"

# The pinned render/geometry wheels (vtk, pyvista, cadquery-ocp) dlopen system libraries at import.
# A minimal Linux install does not ship them, and `import meshpipeline` alone does not pull them in
# - so without this check setup reports success and a later test tier fails with a bare OSError
# from deep inside an import, naming a library but not how to get it.
NATIVE_LIB_PKGS="libglu1-mesa libgl1 libxrender1 libxcursor1 libxft2 libxinerama1 libgomp1"
LIBCHECK_ERR="$(mktemp)"
if "${VPY}" -c "import meshpipeline.sandbox.backend" >/dev/null 2>"${LIBCHECK_ERR}"; then
  ok "geometry/render stack loads (system libraries present)"
  rm -f "${LIBCHECK_ERR}"
else
  MISSING_LIB="$(grep -oE 'lib[A-Za-z0-9_.+-]+\.so[0-9.]*' "${LIBCHECK_ERR}" | head -1 || true)"
  cat "${LIBCHECK_ERR}" >&2
  rm -f "${LIBCHECK_ERR}"
  [ -n "${MISSING_LIB}" ] || die "the geometry/render stack does not import - see the error above"
  die "the geometry/render stack cannot load: ${MISSING_LIB} is missing.
       On Debian/Ubuntu:  sudo apt-get install -y ${NATIVE_LIB_PKGS}
       These are system libraries the pinned vtk/pyvista/cadquery wheels open at import;
       they are not Python packages, so pip cannot supply them."
fi

# 5. local directories the stack bind-mounts
step "Preparing local directories"
mkdir -p "${ROOT}/data/jobs" "${ROOT}/data/corpus"
ok "data/ (jobs, corpus) ready"
# The credential's canonical home, on every machine. Setup makes the DIRECTORY and never the
# file: obtaining an identity is the developer's own act, and a setup script that could produce
# a credential is a setup script that has to be trusted with one.
mkdir -p "${ROOT}/secrets/gcp"
chmod 700 "${ROOT}/secrets" "${ROOT}/secrets/gcp"
ok "secrets/gcp/ ready (owner-only) - put your own ADC file there; setup never creates it"

# 6. Docker daemon reachable
step "Validating Docker"
if docker info >/dev/null 2>&1; then ok "docker daemon is running"
else die "the Docker daemon is not reachable. Start Docker Desktop / the docker service, then rerun: make setup"; fi
COMPOSE="docker compose"; docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"

# Setup is the FIRST thing a clean clone runs, so it resolves the version itself from the same
# committed authority the Makefile, CI and the release tooling read - which is also why running this
# script directly behaves exactly like `make setup`.
APP_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
  "${ROOT}/src/meshpipeline/__init__.py" | head -1)"
export APP_VERSION
ok "product version ${APP_VERSION} (from src/meshpipeline/__init__.py)"

if ${COMPOSE} config >/dev/null 2>&1; then ok "docker-compose.yml is valid"
else
  # The version is already supplied above, so this is a real Compose fault. Report what Compose
  # actually said and hand back a command that carries the version - guidance that drops it would
  # only reproduce a failure the developer has not got.
  COMPOSE_WHY="$(${COMPOSE} config 2>&1 || true)"
  die "docker-compose.yml did not validate. Compose reported:
${COMPOSE_WHY}
    Reproduce it with: APP_VERSION='${APP_VERSION}' ${COMPOSE} config"
fi

# 7. build the container images (cached after the first run)
step "Building container images (api, worker, worker-utility, beat)"
echo "  first build compiles the render stack - later runs are cached and fast…"
${COMPOSE} build
ok "images built"

# done
step "Setup complete"
cat <<DONE
  A local virtualenv is at .venv (make check / make test use it automatically).
  Database migrations apply automatically when the stack starts.
DONE
# Only a run that CREATED .env says so. A rerun must not imply that setup rewrote a file it
# deliberately left alone.
if [ "${ENV_CREATED}" = "1" ]; then
  printf '\n  %s.env was created from .env.example and carries defaults only.%s\n' "${B}" "${Z}"
else
  printf '\n  Your existing .env was left unchanged.\n'
fi

# WHY THIS IS NOT "edit .env, then dev-up". Setup needs no credential and completes on a blank
# .env, so finishing here says nothing about whether the deployment can mesh. Every mesh is
# dispatched to a Cloud Run job, and `make dev-up` refuses to start until that executor is
# configured and reachable. A banner that named only .env and dev-up sent a developer into a
# refusal with no idea which of the two onboarding paths they were even on.
cat <<NEXT

  ${B}Setup finishing does not mean the system can accept jobs.${Z} Meshing runs on a Cloud Run
  job, so ${B}make dev-up${Z} refuses to start until the gcloud identity, the ADC file, the
  project, region, Cloud Run job and exchange bucket are configured and the executor preflight
  passes.

  Follow ONE path. Which one is decided by looking, NOT by your .env being empty - a local
  reset deletes .env while every cloud resource keeps running:

      gcloud run jobs list --project=<PROJECT_ID>

  Both paths are written out in ${B}docs/getting-started/setup.md${Z}.

  ${B}A. A mesh job WAS listed - the executor already exists${Z}
      gcloud auth login
      gcloud config set project <PROJECT_ID>
      gcloud auth application-default login    # TICK EVERY BOX on the consent page, or this fails
      install -D -m 600 "\$HOME/.config/gcloud/application_default_credentials.json" \\
        secrets/gcp/application_default_credentials.json    # nothing copies it for you
      make mesh-adopt      # reads the project, region, job and bucket off the executor itself
      \$EDITOR .env         # the two provider keys - the only values nothing can discover
      make mesh-doctor     # must pass before the next command
      make dev-up

  ${B}B. NOTHING was listed - provision the executor yourself${Z}
      gcloud auth login
      gcloud config set project <PROJECT_ID>
      gcloud auth application-default login    # TICK EVERY BOX on the consent page, or this fails
      install -D -m 600 "\$HOME/.config/gcloud/application_default_credentials.json" \\
        secrets/gcp/application_default_credentials.json    # nothing copies it for you
      \$EDITOR .env         # provider keys, plus the GCP names you want created
      make mesh-setup      # creates the job, bucket, service account and IAM
      make dev-up          # only after provisioning succeeds

NEXT
