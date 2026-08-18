#!/usr/bin/env bash
# Responsibility: Diagnose the hybrid workflow - the local control plane plus the Cloud Run mesh job.
# Boundaries: read-only; it starts nothing, meshes nothing, and reports whether a value is set, never the value.

# `make dev-doctor` - preflight for the HYBRID developer workflow:
#
#     local control plane (UI / API / Celery / Postgres / Redis / MinIO / SearXNG)
#       + Cloud Run native mesh compute (via the mesh job + GCS exchange)
#
# It validates configuration WITHOUT starting anything and WITHOUT running a mesh. Native meshing
# is NOT run on the developer's machine in the normal workflow - mesh compute is the existing Cloud
# Run mesh Job. This script never prints a credential value; it reports only whether each is set.
#
# Exit non-zero on a hard failure, with a corrective message. Cloud reachability checks are
# best-effort: when the gcloud CLI is absent we verify configuration PRESENCE and say plainly that
# live reachability could not be checked here - we never fake a cloud probe.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; Z=$'\033[0m'
else B=""; G=""; Y=""; R=""; Z=""; fi
step() { printf '\n%s━━━ %s%s\n' "${B}" "$1" "${Z}"; }
ok()   { printf '  %sok%s    %s\n' "${G}" "${Z}" "$1"; }
warn() { printf '  %s!!%s    %s\n' "${Y}" "${Z}" "$1"; }
bad()  { printf '  %sERROR%s %s\n' "${R}" "${Z}" "$1"; FAIL=1; }
have() { command -v "$1" >/dev/null 2>&1; }
FAIL=0

# load .env (values are used for presence checks only; never echoed)
step "Configuration source"
if [ -f .env ]; then
  set -a; . ./.env 2>/dev/null || true; set +a
  ok ".env found"
else
  bad ".env not found - run: make setup  (it creates one from .env.example; you fill it in)"
fi
isset() { local v="${!1-}"; [ -n "${v}" ] && [ "${v}" != "__set_me__" ]; }

# WHICH settings must be set is the catalogue's answer, never a list kept here: a diagnostic that
# knows a different set from the gate is worse than no diagnostic.
VPY="${ROOT}/.venv/bin/python"; [ -x "${VPY}" ] || VPY="python3"
REQUIRED="$("${VPY}" -m meshpipeline.settings.inventory --required 2>/dev/null || true)"
[ -n "${REQUIRED}" ] || warn "the settings catalogue could not be read (is .venv built? run: make setup)"

# host tools
step "Host tools"
if have python3.12 || have python3.11 || have python3; then ok "python3"; else
  bad "python3 (>=3.11) not found"; fi
have docker && ok "docker" || bad "docker not found"
if docker compose version >/dev/null 2>&1; then ok "docker compose (v2)"
elif have docker-compose; then ok "docker-compose (v1)"
else bad "docker compose (the Compose v2 plugin) not found"; fi
docker info >/dev/null 2>&1 && ok "docker daemon reachable" || bad "docker daemon not reachable - start Docker"

# every setting the catalogue declares required, checked by presence only
step "Required settings"
for k in ${REQUIRED}; do
  if isset "$k"; then ok "$k is set"; else bad "$k is unset in .env"; fi
done

# local infrastructure config
step "Local infrastructure"
[ "${POSTGRES_HOST:-postgres}" = "postgres" ] && ok "POSTGRES_HOST=postgres (local compose)" \
  || warn "POSTGRES_HOST=${POSTGRES_HOST:-} - the hybrid dev stack uses the local 'postgres' service"
ok "REDIS_URL=${REDIS_URL:-redis://redis:6379/0} (local)"
ok "MINIO_ENDPOINT=${MINIO_ENDPOINT:-minio:9000} (local)"
# bucket name must be S3/MinIO-valid (lowercase, DNS-compatible)
BUCKET="${MINIO_BUCKET:-mesh-artifacts}"
if printf '%s' "${BUCKET}" | grep -Eq '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$' \
   && [ "${BUCKET}" = "$(printf '%s' "${BUCKET}" | tr 'A-Z' 'a-z')" ] \
   && ! printf '%s' "${BUCKET}" | grep -q '\.\.'; then
  ok "MINIO_BUCKET=${BUCKET} (valid)"
else
  bad "MINIO_BUCKET='${BUCKET}' is not a valid S3/MinIO name (lowercase, 3-63, DNS-compatible)"
fi
ok "SearXNG: WEB_SEARCH_BASE_URL=${WEB_SEARCH_BASE_URL:-http://searxng:8080} (local)"

# every application mesh is dispatched to the Cloud Run mesh job; the settings that name it are
# in the required set above, so only the credential FILE needs its own check here
step "Mesh compute (Cloud Run)"
ADC="$("${VPY}" devtools/env/check_runtime_config.py --credential-path 2>/dev/null || true)"
if [ -n "${ADC}" ] && [ -s "${ADC}" ] && [ -r "${ADC}" ]; then
  ok "the credential is in place (secrets/gcp/) and readable"
else
  bad "no readable credential at secrets/gcp/application_default_credentials.json.
        1. gcloud auth application-default login   (tick EVERY box on the consent page)
        2. install -D -m 600 \"\$HOME/.config/gcloud/application_default_credentials.json\" \\
             secrets/gcp/application_default_credentials.json
        Step 2 is not optional: the login writes outside this repository. Copy, never move,
        or every other gcloud tool on this machine loses its credential. Mounted read-only."
fi

# cloud reachability (best-effort; never faked)
step "Cloud mesh reachability"
if have gcloud; then
  if isset GCP_PROJECT_ID && isset CLOUDRUN_JOB && isset GCP_REGION; then
    if gcloud run jobs describe "${CLOUDRUN_JOB}" --region "${GCP_REGION}" \
         --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
      ok "Cloud Run mesh Job '${CLOUDRUN_JOB}' is describable"
    else
      bad "cannot describe Cloud Run mesh Job '${CLOUDRUN_JOB}' in ${GCP_PROJECT_ID}/${GCP_REGION}
        - check the name, project, region, and your credentials (no mesh is started)"
    fi
  else
    warn "gcloud present but GCP_PROJECT_ID/CLOUDRUN_JOB/GCP_REGION incomplete - skipping describe"
  fi
  if isset GCP_MESH_BUCKET; then
    gcloud storage ls "gs://${GCP_MESH_BUCKET}" >/dev/null 2>&1 \
      && ok "mesh exchange bucket gs://${GCP_MESH_BUCKET} is accessible" \
      || bad "cannot access mesh exchange bucket gs://${GCP_MESH_BUCKET}"
  fi
else
  warn "gcloud CLI not installed - configuration PRESENCE checked above; live mesh Job/bucket
        reachability was NOT verified here (install the gcloud SDK to verify before a real run)"
fi

# compose validity + ports + migration config
step "Compose + ports + migrations"
if docker compose config >/dev/null 2>&1; then ok "docker-compose.yml is valid"
else bad "docker-compose.yml did not validate - run: docker compose config"; fi
for p in 5432 6379 9000 9001 8000; do
  if have ss && ss -ltn "( sport = :$p )" 2>/dev/null | grep -q ":$p"; then
    warn "port $p is already in use - the local stack binds it (stop the conflicting service)"
  else ok "port $p free"; fi
done
[ -f alembic.ini ] && ok "alembic configured (migrations apply automatically on API start)" \
  || bad "alembic.ini missing"

# verdict
step "Verdict"
if [ "${FAIL}" -eq 0 ]; then
  # Without the CLI the section above checked that the settings are PRESENT and said plainly that
  # it could not reach the job or the bucket. Calling that "looks good" and sending the developer
  # to dev-up promised a start that mesh-doctor, which dev-up runs first, refuses without gcloud.
  if have gcloud; then
    ok "hybrid developer configuration looks good - next:  make dev-up"
  else
    ok "no blocking local problem, but the mesh executor was NOT verified here (no gcloud CLI)"
    printf '        install the Google Cloud CLI, then: make mesh-doctor\n'
    printf '        make dev-up runs that same check first and will refuse until it passes\n'
  fi
  exit 0
else
  printf '\n%sdev-doctor found blocking problems above.%s Fix them, then rerun: make dev-doctor\n' \
    "${R}" "${Z}" >&2
  exit 1
fi
