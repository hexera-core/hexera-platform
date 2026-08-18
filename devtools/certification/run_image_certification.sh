#!/usr/bin/env bash
# Responsibility: Certify publication behaviour against a BUILT product image, with no source mount.
# Boundaries: it builds and removes one disposable derivative; it never alters a product image or tag.

# The product runtime images ship no `tests/` and no `devtools/`, and that must stay true. So the
# suite cannot be mounted over them (a mount is a source tree by another name, and it is how a
# stale image passes as fresh). Instead this COPIES the committed suite and the committed
# certification tooling into a throwaway derivative FROM the exact image under test, and runs
# there. The production package is the one the image already installed: nothing is reinstalled,
# nothing is overlaid, and `src/` never enters the derivative.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="$PWD"

BASE_IMAGE="${BASE_IMAGE:?set BASE_IMAGE to the exact image ID under test}"
# `FROM` will not take a bare image ID: a `sha256:` digest reads as a registry reference it
# would try to pull, and a 64-hex string is rejected outright. So the exact ID is pinned under a
# task-owned tag that is PROVEN to resolve to it, immediately before and after the build - which
# is the opposite of building from whatever a mutable product tag happens to point at.
BASE_REF="meshpipeline-certification-base:${RUN_ID:-pending}"
WANT_DIGEST="${WANT_DIGEST:?set WANT_DIGEST to the final committed source digest}"
EVID="${EVID:?set EVID to a directory for the structured results}"
mkdir -p "$EVID"

RUN_ID="$(python3 -c 'import uuid;print(uuid.uuid4().hex[:16])')"
BASE_REF="meshpipeline-certification-base:${RUN_ID}"
DERIVATIVE="meshpipeline-certification:${RUN_ID}"
TASK_DB="meshtest_${RUN_ID}"
# The stack's network is PROJECT-SCOPED, so it is DISCOVERED rather than assumed. A fixed literal
# is exactly what let one project's container resolve another project's `postgres`, and this script
# then certifies against a database it does not own. Found by label, the way Compose itself owns a
# network, using the same project-name rule tests/integration/preflight.py applies.
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PROJECT" | tr '[:upper:]' '[:lower:]' \
  | tr -d '._-')}"
NETWORK="${NETWORK:-$(docker network ls \
  --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" \
  --filter "label=com.docker.compose.network=default" --format '{{.Name}}' | head -1)}"
[ -n "$NETWORK" ] || { echo "no Compose network is labelled as the default network of project \
'${COMPOSE_PROJECT}'. Certification runs against that project's postgres and redis, so it will not \
guess one. Start the stack (make dev-up), or set COMPOSE_PROJECT_NAME / NETWORK." >&2; exit 1; }
REDIS_DB="${REDIS_DB:-15}"

echo "$DERIVATIVE" > "$EVID/derivative.tag"
echo "$BASE_IMAGE"  > "$EVID/base-image.id"
echo "$TASK_DB"     > "$EVID/task-database.name"

# the base image must be the one the caller means, and must be current
STAMP="$(docker run --rm --entrypoint printenv "$BASE_IMAGE" MESH_SOURCE_TREE)"
echo "$STAMP" > "$EVID/base-image.stamp"
if [ "$STAMP" != "$WANT_DIGEST" ]; then
  echo "FATAL: image $BASE_IMAGE is stamped ${STAMP:0:12}, not the requested ${WANT_DIGEST:0:12}." >&2
  echo "       Refusing before any test runs: a tier against an image built from other source" >&2
  echo "       proves nothing about this tree. Rebuild, then run this again." >&2
  exit 1
fi

PKG_DIR="$(docker run --rm --entrypoint python "$BASE_IMAGE" -c \
  'import meshpipeline, os; print(os.path.dirname(meshpipeline.__file__))')"
echo "$PKG_DIR" > "$EVID/installed-package-path"
case "$PKG_DIR" in
  */site-packages/meshpipeline|*/dist-packages/meshpipeline) ;;
  *) echo "FATAL: $BASE_IMAGE imports meshpipeline from $PKG_DIR, not an installed location" >&2
     exit 1 ;;
esac
docker run --rm --entrypoint python "$BASE_IMAGE" -c '
import hashlib, os, sys
root = sys.argv[1]
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for name in sorted(filenames):
        if name.endswith(".py"):
            full = os.path.join(dirpath, name)
            print(hashlib.sha256(open(full, "rb").read()).hexdigest(), "",
                  os.path.relpath(full, root))
' "$PKG_DIR" | sort > "$EVID/base-image-package.sha256"

# the disposable derivative: the suite and the tooling, never the source
BUILD_DIR="$(mktemp -d)"
cleanup() {
  local rc=$?
  rm -rf "$BUILD_DIR"
  docker image rm -f "$DERIVATIVE" >/dev/null 2>&1 || true
  docker image rm -f "$BASE_REF" >/dev/null 2>&1 || true
  docker exec -e MESH_TEST_RUN_ID="$RUN_ID" "$WORKER" sh -c "
    PYTHONPATH=/srv python -c \"
import os, sys
sys.path.insert(0, '/srv')
from tests import disposable_database as dd
try:
    dd.drop(dd.authorize(os.path.expandvars('$TASK_URL_SH')), os.path.expandvars('$ADMIN_URL_SH'))
    print('  dropped $TASK_DB')
except Exception as exc:
    print('  $TASK_DB NOT dropped: %s' % exc)
\"" 2>/dev/null || echo "  $TASK_DB NOT dropped"
  docker exec "$REDIS_CONTAINER" redis-cli -n "$REDIS_DB" --scan --count 1000 \
    > "$EVID/redis-keys.after" 2>/dev/null || true
  local n=0
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    docker exec "$REDIS_CONTAINER" redis-cli -n "$REDIS_DB" DEL "$key" >/dev/null
    n=$((n + 1))
  done < "$EVID/redis-keys.after"
  echo "  removed derivative $DERIVATIVE and $n task redis keys"
  return $rc
}

WORKER="$(docker compose ps -q worker)"
REDIS_CONTAINER="$(docker compose ps -q redis)"
[ -n "$WORKER" ] && [ -n "$REDIS_CONTAINER" ] || {
  echo "FATAL: the service stack is not running" >&2; exit 1; }
ADMIN_URL_SH='postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$POSTGRES_DB?sslmode=disable'
TASK_URL_SH="postgresql://\$POSTGRES_USER:\$POSTGRES_PASSWORD@postgres:5432/${TASK_DB}?sslmode=disable"
trap cleanup EXIT

docker exec "$REDIS_CONTAINER" redis-cli -n "$REDIS_DB" --scan --count 1000 \
  > "$EVID/redis-keys.before" 2>/dev/null || true
[ ! -s "$EVID/redis-keys.before" ] || {
  echo "FATAL: redis db $REDIS_DB is not empty; refusing to add task keys to it" >&2; exit 1; }

# Pin the exact ID under the task-owned tag, and prove the pin.
docker tag "$BASE_IMAGE" "$BASE_REF"
PINNED="$(docker images --no-trunc --format '{{.ID}}' "$BASE_REF")"
[ "$PINNED" = "$BASE_IMAGE" ] || {
  echo "FATAL: $BASE_REF resolves to $PINNED, not the requested $BASE_IMAGE" >&2; exit 1; }
echo "$BASE_REF -> $PINNED" > "$EVID/base-image.pin"

cp -a tests "$BUILD_DIR/tests"
cp -a devtools "$BUILD_DIR/devtools"
cp -a pyproject.toml "$BUILD_DIR/pyproject.toml"
# The one thing that must never arrive:
rm -rf "$BUILD_DIR/src" 2>/dev/null || true
cat > "$BUILD_DIR/Dockerfile" <<DOCKERFILE
# syntax=docker/dockerfile:1
FROM ${BASE_REF}
# Certification-only. The product image is the base and is not modified: no wheel is installed,
# no application file is replaced, and src/ is absent by construction.
COPY tests /srv/tests
COPY devtools /srv/devtools
COPY pyproject.toml /srv/pyproject.toml
WORKDIR /srv
DOCKERFILE
docker build -q -t "$DERIVATIVE" "$BUILD_DIR" > "$EVID/derivative-build.log"
REPINNED="$(docker images --no-trunc --format '{{.ID}}' "$BASE_REF")"
[ "$REPINNED" = "$BASE_IMAGE" ] || {
  echo "FATAL: the base moved during the build: $REPINNED" >&2; exit 1; }
docker images --no-trunc --format '{{.ID}}' "$DERIVATIVE" > "$EVID/derivative.id"

# prove the derivative before trusting a single test in it
docker run --rm --entrypoint python "$DERIVATIVE" -c '
import hashlib, json, os, pathlib, sys
import meshpipeline
package = os.path.dirname(meshpipeline.__file__)
digest = {}
for dirpath, dirnames, filenames in os.walk(package):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for name in sorted(filenames):
        if name.endswith(".py"):
            full = os.path.join(dirpath, name)
            digest[os.path.relpath(full, package)] = hashlib.sha256(
                open(full, "rb").read()).hexdigest()
def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
print(json.dumps({
    "package_file": meshpipeline.__file__,
    "package_dir": package,
    "sys_path_head": sys.path[:4],
    "src_present": os.path.isdir("/srv/src"),
    "modules": len(digest),
    "package_sha256": {k: v for k, v in sorted(digest.items())},
    "manifest_sha256": sha("/srv/devtools/quality/publication_authority_manifest.json"),
    "ledger_sha256": sha("/srv/devtools/quality/publication_certification_ledger.json"),
}))' > "$EVID/derivative-provenance.json"

python3 - "$EVID" "$PROJECT" <<'PY'
import hashlib, json, pathlib, sys
evid, project = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
prov = json.loads((evid / "derivative-provenance.json").read_text())
problems = []
if prov["src_present"]:
    problems.append("the derivative contains /srv/src - a source tree shadowing the wheel")
if "/srv" in [p.rstrip("/") for p in prov["sys_path_head"][:1]] and prov["package_dir"].startswith("/srv"):
    problems.append(f"meshpipeline resolves under /srv: {prov['package_file']}")
if not prov["package_dir"].endswith("packages/meshpipeline"):
    problems.append(f"meshpipeline is not the installed package: {prov['package_dir']}")
base = {}
for line in (evid / "base-image-package.sha256").read_text().splitlines():
    if line.strip():
        h, rel = line.split(None, 1)
        base[rel.strip()] = h
if base != prov["package_sha256"]:
    only_base = sorted(set(base) - set(prov["package_sha256"]))
    only_deriv = sorted(set(prov["package_sha256"]) - set(base))
    changed = sorted(k for k in set(base) & set(prov["package_sha256"])
                     if base[k] != prov["package_sha256"][k])
    problems.append(f"the derivative's package differs from the base image: "
                    f"missing={only_base[:5]} added={only_deriv[:5]} changed={changed[:5]}")
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
manifest = sha(project / "devtools/quality/publication_authority_manifest.json")
ledger = sha(project / "devtools/quality/publication_certification_ledger.json")
if prov["manifest_sha256"] != manifest:
    problems.append("the copied manifest is not the committed manifest")
if prov["ledger_sha256"] != ledger:
    problems.append("the copied ledger is not the committed ledger")
(evid / "derivative-checks.json").write_text(json.dumps(
    {"problems": problems, "modules": prov["modules"],
     "package_dir": prov["package_dir"],
     "manifest_sha256": manifest, "ledger_sha256": ledger}, indent=2) + "\n")
if problems:
    print("FATAL: the certification derivative is not trustworthy:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)
print(f"  derivative verified: {prov['modules']} installed modules identical to the base image, "
      f"package at {prov['package_dir']}, no source tree present")
PY

# provision one disposable database and run
echo "  provisioning disposable database $TASK_DB (run $RUN_ID)"
docker exec -e MESH_TEST_RUN_ID="$RUN_ID" "$WORKER" sh -c "
  PYTHONPATH=/srv python -c \"
import os, sys
sys.path.insert(0, '/srv')
from tests import disposable_database as dd
dd.provision(os.path.expandvars('$ADMIN_URL_SH'))
\""

set +e
docker run --rm --network "$NETWORK" --env-file "$PROJECT/.env" \
  --entrypoint sh \
  -e MESH_TEST_RUN_ID="$RUN_ID" \
  -e MESH_TASK_DB="$TASK_DB" \
  -e REDIS_URL="redis://redis:6379/${REDIS_DB}" \
  -e API_ROOT="/tmp/api-root-$RUN_ID" \
  -e PROMETHEUS_MULTIPROC_DIR="/tmp/prom_multiproc" \
  -e PYTHONPATH="/srv" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$DERIVATIVE" -c 'mkdir -p "$API_ROOT" "$PROMETHEUS_MULTIPROC_DIR" && cd /srv &&
    export DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$MESH_TASK_DB?sslmode=disable" &&
    exec python -m pytest -q -rs -o asyncio_mode=auto -p no:cacheprovider '"${PYTEST_ARGS:-/srv/tests/integration}" \
  2>&1 | tee "$EVID/pytest.log"
RC=${PIPESTATUS[0]}

# The certification assertion runs again, alone and unbuffered, whatever the caller selected.
# Its structured total is the evidence this whole path exists to produce, so it is never left to
# a caller's choice of arguments - and a run that reaches here without emitting one is refused.
docker run --rm --network "$NETWORK" --env-file "$PROJECT/.env" \
  --entrypoint sh \
  -e MESH_TEST_RUN_ID="$RUN_ID" \
  -e MESH_TASK_DB="$TASK_DB" \
  -e REDIS_URL="redis://redis:6379/${REDIS_DB}" \
  -e API_ROOT="/tmp/api-root-$RUN_ID" \
  -e PROMETHEUS_MULTIPROC_DIR="/tmp/prom_multiproc" \
  -e PYTHONPATH="/srv" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$DERIVATIVE" -c 'mkdir -p "$API_ROOT" "$PROMETHEUS_MULTIPROC_DIR" && cd /srv &&
    export DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$MESH_TASK_DB?sslmode=disable" &&
    exec python -m pytest -q -rs -s -o asyncio_mode=auto -p no:cacheprovider \
      /srv/tests/integration/test_execution_context_coverage.py' \
  2>&1 | tee "$EVID/certification.log"
CERT_RC=${PIPESTATUS[0]}
cat "$EVID/certification.log" >> "$EVID/pytest.log"
[ "$RC" -eq 0 ] || CERT_RC=$RC
RC=$CERT_RC
set -e

# the certification must have EXECUTED
python3 - "$EVID" <<'PY'
import pathlib, re, sys
evid = pathlib.Path(sys.argv[1])
log = (evid / "pytest.log").read_text()
critical = "test_every_execution_owned_publication_context_is_behaviorally_certified"
problems = []
if re.search(rf"SKIPPED.*{re.escape('test_execution_context_coverage')}", log):
    problems.append("the manifest-derived certification skipped; the derivative carries devtools, "
                    "so a skip here means it did not reach the suite")
if "61/61" not in log:
    problems.append("no 61/61 certification line in the output")
(evid / "certification-checks.json").write_text(
    __import__("json").dumps({"problems": problems,
                              "critical_assertion": critical,
                              "certified_line": [ln.strip() for ln in log.splitlines()
                                                 if "EXECUTION CONTEXT COVERAGE" in ln]},
                             indent=2) + "\n")
if problems:
    print("FATAL: image-backed certification did not execute:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)
PY
exit $RC
