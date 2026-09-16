#!/usr/bin/env bash
# Responsibility: Drive a genuine native mesh through the real terminal chain against real Postgres, Redis and MinIO.
# Boundaries: proves local native-to-terminal closure only; it proves nothing about Cloud Run or hosted storage.

# NATIVE-to-TERMINAL tier - the heaviest local proof: a GENUINE native mesh from each engine driven
# through the REAL production terminal chain (application.pipeline_run._run_async) against REAL
# PostgreSQL + Redis + MinIO, read back on every durable surface (REST / Redis / WS-replay /
# persisted final_result), with a DETERMINISTIC Reviewer substitute and NO model calls.
#
# Runs INSIDE the `mesh` image (it needs both the OpenFOAM/gmsh/vmtk toolchains AND fastapi/redis/
# sqlalchemy). Provisions the three services on a throwaway network, health-checks them, runs the
# `native_terminal` matrix through the INSTALLED wheel (production provenance), tears the stack down.
#
# Proves local native→terminal closure. Does NOT prove Cloud Run / GCS / Neon / Upstash.
set -uo pipefail

# The build context and the bind-mounted suite below are repo-relative, so run from the repo
# root whatever the caller's CWD is (`make test-native-terminal` and a direct `bash` both work).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1

MESH_IMAGE="${MESH_IMAGE:-meshpipeline-mesh:native}"
NET=fm-nt-net; PG=fm-nt-pg; RD=fm-nt-redis; MN=fm-nt-minio
MINIO_IMAGE=quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z
EVID="${FM_TERMINAL_EVIDENCE_DIR:-/tmp/fm-nt/evidence}"

# `-v` with `-f`: each disposable service declares an anonymous volume, and `docker rm -f`
# alone leaves it behind - a measured leak of three volumes per run. The integration and
# release harnesses already tear down this way.
cleanup(){ docker rm -fv "$PG" "$RD" "$MN" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
mkdir -p "$EVID"; chmod -R 777 "$EVID"

# The image is built by `make mesh-image`, never here. A runner that builds its own image can
# turn a stale image current inside its own preflight, and the matrix would then describe
# whatever it just built rather than the checkout under test. SKIP_BUILD is gone with it: the
# stamp decides, so there is nothing left for a caller to assert about the source themselves.
bash tests/native/assert_mesh_image_current.sh "$MESH_IMAGE"

docker network create "$NET" >/dev/null
docker run -d --name "$RD" --network "$NET" redis:7-alpine >/dev/null
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=mesh -e POSTGRES_PASSWORD=x -e POSTGRES_DB=mesh postgres:16-alpine >/dev/null
docker run -d --name "$MN" --network "$NET" \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  "$MINIO_IMAGE" server /data >/dev/null

echo "── health-checking services ──"
ok=""; for i in $(seq 1 30); do docker exec "$PG" pg_isready -U mesh >/dev/null 2>&1 && ok=1 && break; sleep 1; done
[ -n "$ok" ] || { echo "FATAL: Postgres unhealthy"; exit 1; }
ok=""; for i in $(seq 1 20); do docker run --rm --network "$NET" redis:7-alpine redis-cli -h "$RD" ping 2>/dev/null | grep -q PONG && ok=1 && break; sleep 1; done
[ -n "$ok" ] || { echo "FATAL: Redis unhealthy"; exit 1; }
ok=""; for i in $(seq 1 30); do docker run --rm --network "$NET" --entrypoint curl curlimages/curl:8.11.1 -sf "http://$MN:9000/minio/health/ready" >/dev/null 2>&1 && ok=1 && break; sleep 1; done
[ -n "$ok" ] || { echo "FATAL: MinIO unhealthy"; exit 1; }

# The matrix clears application tables between engines, and those helpers require proof that the
# database was provisioned for THIS run. The identity is minted here, once, and the marker is
# written into the task database the suite will actually connect to - so the proof travels with
# the storage rather than with a flag anyone could pass.
RUN_ID="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TASK_DB="meshtest_${RUN_ID}"
echo "── provisioning the disposable native database $TASK_DB ──"
docker run --rm --network "$NET" \
  -e MESH_TEST_RUN_ID="$RUN_ID" -e PYTHONPATH=/srv \
  -v "$PWD/tests:/srv/tests:ro" \
  --entrypoint python "$MESH_IMAGE" -c "
import sys; sys.path.insert(0, '/srv')
from tests import disposable_database as dd
print('  provisioned', dd.provision('postgresql://mesh:x@${PG}:5432/mesh').database)
" || exit 1

echo "── running the five-engine native-to-terminal matrix inside $MESH_IMAGE ──"
docker run --rm --network "$NET" \
  -e OMPI_ALLOW_RUN_AS_ROOT=1 -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
  -e PIPELINE_BACKEND=deferred \
  -e MESH_TEST_RUN_ID="$RUN_ID" \
  -e POSTGRES_HOST="$PG" -e POSTGRES_USER=mesh -e POSTGRES_PASSWORD=x -e POSTGRES_DB="$TASK_DB" \
  -e REDIS_URL="redis://$RD:6379/0" \
  -e MINIO_ENDPOINT="$MN:9000" -e MINIO_ACCESS_KEY=minioadmin -e MINIO_SECRET_KEY=minioadmin \
  -e MINIO_BUCKET=fm-nt-jobs \
  -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x \
  -e FM_TERMINAL_EVIDENCE_DIR=/evid \
  -v "$PWD/tests:/srv/tests:ro" -v "$PWD/pyproject.toml:/srv/pyproject.toml:ro" \
  -v "$EVID":/evid \
  --entrypoint python "$MESH_IMAGE" \
  -m pytest -rA -p no:cacheprovider -c /srv/pyproject.toml \
  -o testpaths=/srv/tests/native -o cache_dir=/tmp/pytest_cache \
  -m native_terminal /srv/tests/native
rc=$?
echo "── native-to-terminal matrix rc=$rc (evidence: $EVID) ──"
exit $rc
