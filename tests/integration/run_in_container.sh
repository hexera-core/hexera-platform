#!/usr/bin/env bash
# Responsibility: Run the full integration tier inside the pipeline image against real Postgres, Redis and MinIO.
# Boundaries: it fails loudly on an unavailable service or an unexpected skip, and tears the stack down on exit.

# Dependency-backed container integration - the LOCAL equivalent of CI's BLOCKING integration job.
#
# Builds the pipeline image, provisions REAL Postgres + Redis + MinIO on a throwaway network,
# health-checks all three, runs the FULL integration tier inside the image (bind-mounted suite,
# its own pytest session), and FAILS LOUDLY if any required service is unavailable or if any test
# skips for a reason other than the two optional fixtures a clean clone never has. Tears the
# stack down on exit regardless of outcome.
#
# This proves: real Postgres, real Redis, real MinIO, Docker/runtime. It does NOT prove native
# mesh correctness, hosted GCS, Neon, or Upstash.
#
# THE ENVIRONMENT IS BUILT HERE, NOT INHERITED. `DATABASE_URL` is the application's canonical
# database configuration - `settings/providers.py` lets it override the decomposed POSTGRES_*
# variables, and the integration suite uses its presence as the signal that a real endpoint
# exists. Supplying only POSTGRES_* made every database-backed module call
# `pytest.skip(..., allow_module_level=True)`, so the tier reported a green ~264 tests while
# silently skipping the ~162 that are the entire point of it. Any ambient DATABASE_URL/API_ROOT
# is unset below so a developer's shell can never point this at a real database.
set -euo pipefail

# The build context and the bind-mounted suite below are repo-relative, so run from the repo
# root whatever the caller's CWD is (`make test-container-integration` and a direct `bash` both work).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# HOSTILE-AMBIENT GUARD. These are constructed per-run against the disposable stack; inheriting
# them would point the suite at whatever the caller happened to export.
unset DATABASE_URL API_ROOT POSTGRES_DSN || true

NET=ci-local-net
PG=ci-local-postgres
RD=ci-local-redis
MN=ci-local-minio
IMG=meshpipeline-pipeline:test
MINIO_IMAGE=minio/minio:RELEASE.2025-04-22T22-12-26Z
# Every resource this script creates carries this label, so teardown can prove ownership instead
# of subtracting one inventory from another.
LABEL=hexera-ci-local

# The disposable Postgres this script starts: plaintext, on a throwaway network, never reachable
# from outside it. `sslmode=disable` applies to THIS instance only - `normalize_database_url`
# treats TLS as required unless the query string says otherwise, which is the correct default for
# every real deployment and the reason a managed URL needs no special casing here.
PGUSER=mesh
PGPASSWORD=x
PGDATABASE=mesh
# The server is created with this database; the SUITE never uses it. A fresh disposable database
# is provisioned below and carries the marker the destructive helpers require.
BOOTSTRAP_URL="postgresql://${PGUSER}:${PGPASSWORD}@${PG}:5432/${PGDATABASE}?sslmode=disable"

# Task-owned scratch. `API_ROOT` is a writable directory the upload/geometry suites materialise
# real files into; the JUnit report is written here too, so the guard below reads a structured
# result rather than scraping console text.
RUNDIR="$(mktemp -d -t hexera-ci-XXXXXXXX)"
API_ROOT_HOST="$RUNDIR/api-root"
mkdir -p "$API_ROOT_HOST"
# The image's pytest process is not necessarily this host uid.
chmod 777 "$RUNDIR" "$API_ROOT_HOST"

# Teardown removes ONLY the exact names created above, with -v so each container's anonymous
# volume goes with it rather than accumulating on the host. No prune, no dangling sweep, no
# baseline subtraction.
#
# SPLIT DELIBERATELY. The pre-run sweep must clear a previous run's CONTAINERS without touching
# this run's scratch directory: deleting it here left Docker to re-create the bind-mount source
# itself, which it does as root, and the image's uid-1000 pytest then could not write its JUnit
# report into it.
docker_cleanup() {
  docker rm -fv "$RD" "$PG" "$MN" >/dev/null 2>&1 || true
  # A run killed mid-flight leaves its short-lived health-check/pytest container attached, and an
  # attached endpoint makes `network rm` fail - which then made `network create` fail on the next
  # run. Those containers carry this round's label, so they can be removed by proven ownership
  # rather than by sweeping whatever happens to be attached.
  local strays
  strays="$(docker ps -aq --filter "label=$LABEL=1" --filter "network=$NET" 2>/dev/null || true)"
  [ -n "$strays" ] && docker rm -fv $strays >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
# An unset RUNDIR must never become `rm -rf /`.
cleanup() {
  docker_cleanup
  [ -n "${RUNDIR:-}" ] && [ -d "${RUNDIR:-}" ] && rm -rf "${RUNDIR:?}" || true
}
trap cleanup EXIT

# The image is built by `make test-container-integration` before this script runs. A runner that
# builds its own image can turn a stale one current inside its own preflight, and the tier would
# then describe whatever it just built. The stamp is checked BEFORE any task resource exists.
bash tests/native/assert_mesh_image_current.sh "$IMG"

docker_cleanup
docker network create --label "$LABEL=1" "$NET" >/dev/null
docker run -d --label "$LABEL=1" --name "$RD" --network "$NET" redis:7-alpine >/dev/null
docker run -d --label "$LABEL=1" --name "$PG" --network "$NET" \
  -e POSTGRES_USER="$PGUSER" -e POSTGRES_PASSWORD="$PGPASSWORD" -e POSTGRES_DB="$PGDATABASE" \
  postgres:16-alpine >/dev/null
docker run -d --label "$LABEL=1" --name "$MN" --network "$NET" \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  "$MINIO_IMAGE" server /data >/dev/null

echo "── health-checking Redis ──"
ok=""; for i in $(seq 1 20); do
  if docker run --rm --label "$LABEL=1" --network "$NET" redis:7-alpine redis-cli -h "$RD" ping 2>/dev/null | grep -q PONG; then ok=1; break; fi; sleep 1; done
[ -n "$ok" ] || { echo "FATAL: Redis did not become healthy"; exit 1; }

echo "── health-checking Postgres ──"
ok=""; for i in $(seq 1 30); do
  if docker exec "$PG" pg_isready -U "$PGUSER" >/dev/null 2>&1; then ok=1; break; fi; sleep 1; done
[ -n "$ok" ] || { echo "FATAL: Postgres did not become healthy"; exit 1; }

echo "── health-checking MinIO ──"
ok=""; for i in $(seq 1 30); do
  if docker run --rm --label "$LABEL=1" --network "$NET" --entrypoint curl curlimages/curl:8.11.1 \
       -sf "http://$MN:9000/minio/health/ready" >/dev/null 2>&1; then ok=1; break; fi; sleep 1; done
[ -n "$ok" ] || { echo "FATAL: MinIO did not become healthy"; docker logs "$MN" || true; exit 1; }

# One disposable session per invocation. The identity is minted here from the kernel CSPRNG, the
# database is created and marked through the shared authority, and the suite is pointed at that
# database - so the destructive helpers can prove, from the live server, that it belongs to this
# run. Nothing here re-implements those rules.
RUN_ID="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TASK_DB="meshtest_${RUN_ID}"
export DATABASE_URL="postgresql+asyncpg://${PGUSER}:${PGPASSWORD}@${PG}:5432/${TASK_DB}?sslmode=disable"

echo "── provisioning the disposable database $TASK_DB ──"
docker run --rm --label "$LABEL=1" --network "$NET" \
  -e MESH_TEST_RUN_ID="$RUN_ID" -e PYTHONPATH=/srv \
  -v "$PWD/tests:/srv/tests:ro" \
  --entrypoint python "$IMG" -c "
import sys; sys.path.insert(0, '/srv')
from tests import disposable_database as dd
a = dd.provision('${BOOTSTRAP_URL}')
dd.authorize(a.url)
print('  provisioned and verified', a.database)
" || { echo "FATAL: could not provision a disposable database for this run"; exit 1; }

# INTEGRATION_PASSES: how many pytest passes to run against the SAME provisioned, Alembic-managed
# stack. 1 (the default, and what CI runs) is the cold pass. Above 1, the extra passes are warm -
# same services, schema already built - and the LAST pass runs in randomised order, which is what
# catches a suite that only passes because of collection order. Every pass must satisfy the same
# non-vacuity guard.
PASSES="${INTEGRATION_PASSES:-1}"
case "$PASSES" in ''|*[!0-9]*|0) echo "FATAL: INTEGRATION_PASSES must be a positive integer"; exit 1;; esac

overall=0
for pass in $(seq 1 "$PASSES"); do
  if [ "$PASSES" -gt 1 ] && [ "$pass" -eq "$PASSES" ]; then
    # pytest-randomly shuffles BY DEFAULT, so a randomised pass is simply the absence of
    # `-p no:randomly`. (`-p randomly` is not the plugin's module name and makes pytest fail to
    # start.) It lives in requirements/dev.txt, so it is present only because the tier image is the
    # `validation` target - and that is CHECKED rather than assumed: without the plugin this pass
    # runs in declaration order while reporting itself as randomised, which is a claim about
    # inter-test independence that nothing tested.
    if ! docker run --rm --label "$LABEL=1" --entrypoint python "$IMG" \
           -c "import pytest_randomly" >/dev/null 2>&1; then
      echo "::error:: $IMG carries no pytest-randomly - a 'randomised' pass would reorder nothing." >&2
      echo "          Build the tier image with --target validation (make test-container-integration)." >&2
      exit 1
    fi
    order=""; label="randomised order"
  else
    order="-p no:randomly"; label=$([ "$pass" -eq 1 ] && echo "cold" || echo "warm")
  fi
  echo "── running the FULL integration tier (real PG + Redis + MinIO) - pass $pass/$PASSES, $label ──"
  REPORT="$RUNDIR/report-$pass.xml"

  # The pytest exit status is captured from PIPESTATUS and re-propagated below. `tee` must not
  # become the status that matters, and neither may the guard's own echo, the trap, or anything
  # else that runs afterwards - a green shell after a red pytest is exactly the failure mode this
  # runner exists to prevent.
  set +e
  docker run --rm --label "$LABEL=1" --network "$NET" \
    -e DATABASE_URL="$DATABASE_URL" \
    -e MESH_TEST_RUN_ID="$RUN_ID" \
    -e REDIS_URL="redis://$RD:6379/0" \
    -e API_ROOT=/srv/api-root \
    -e POSTGRES_HOST="$PG" -e POSTGRES_USER="$PGUSER" -e POSTGRES_PASSWORD="$PGPASSWORD" -e POSTGRES_DB="$PGDATABASE" \
    -e MINIO_ENDPOINT="$MN:9000" -e MINIO_ACCESS_KEY=minioadmin -e MINIO_SECRET_KEY=minioadmin \
    -e MINIO_BUCKET=ci-test-jobs \
    -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x \
    -e HEXERA_CONTAINER_TIER=1 \
    -v "$PWD/tests:/srv/tests:ro" \
    -v "$PWD/devtools/quality:/srv/devtools/quality:ro" \
    `# The TRACKED .env.example, mounted where the suite looks for it (parents[2] of a test file,
     # i.e. /srv). It is not in the image - the image ships the source, not the repository - so the
     # test that pins "the code default and the template a clone starts from agree" skipped here
     # forever and the guard counted that skip as a coverage hole. It is a repo file, not an
     # optional fixture, so the runner supplies it rather than the test tolerating its absence.` \
    -v "$PWD/.env.example:/srv/.env.example:ro" \
    -v "$API_ROOT_HOST:/srv/api-root" \
    -v "$RUNDIR:/srv/report" \
    `# -w /srv: the tier image is now the validation target, whose WORKDIR is /repo. The tier has
     # always run from /srv, and a spawned capture worker derives SRC from its cwd.` \
    -w /srv --entrypoint python "$IMG" \
    -m pytest -q -rs -o asyncio_mode=auto -p no:cacheprovider $order \
    --junitxml="/srv/report/$(basename "$REPORT")" /srv/tests/integration
  rc=${PIPESTATUS[0]}
  set -e

  # Structured non-vacuity guard. Reads the JUnit report, not the console, so a change in pytest's
  # summary formatting cannot silently disarm it.
  if ! python3 tests/integration/assert_integration_coverage.py "$REPORT"; then
    echo "::error:: integration coverage guard failed on pass $pass ($label)"
    overall=1
  fi
  if [ "$rc" -ne 0 ]; then
    echo "::error:: pytest exited $rc on pass $pass ($label)"
    overall="$rc"
  fi
done

[ "$overall" -eq 0 ] || exit "$overall"
echo "── dependency-backed container integration: OK ──"
