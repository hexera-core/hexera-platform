#!/usr/bin/env bash
# Responsibility: Run the integration tier inside the worker container against a database provisioned for THIS run.
# Boundaries: it provisions and drops one task database; it never resets, truncates or reuses the shared one.

# The tier drops and rebuilds the public schema on every test. Pointed at the long-lived
# development database that is a data-loss event, and it was: seven pre-existing simulation_jobs
# rows were destroyed that way. So this runner never hands the suite a database it did not create
# for this run, and stamps the one it creates with a run identity the suite re-proves from the
# live server before any destructive statement.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

RUN_ID="$(python3 -c 'import uuid;print(uuid.uuid4().hex[:16])')"
TASK_DB="meshtest_${RUN_ID}"

# revision preflight
# An image built from an older tree runs older code. A green tier against it proves nothing about
# the commit under test - and this is not hypothetical: the running worker image predated
# application/execution_publisher.py while that module was under audit.
#
# The whole decision lives in preflight.py, which reads and does nothing else. It used to live
# here, and the refusal it printed was an UNQUOTED heredoc containing `make rebuild` in backticks:
# bash substitutes commands in an unquoted heredoc body, so producing the complaint about a stale
# image rebuilt and replaced the four application services. The guard fired correctly and mutated
# the stack in the same breath. Refusal text is data now, and this script runs nothing before the
# stamp is accepted.
WORKER="$(python3 tests/integration/preflight.py)" || exit 1
echo "  worker $WORKER matches the committed tree"

# provision one disposable database
ADMIN_URL_SH='postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$POSTGRES_DB?sslmode=disable'
TASK_URL_SH="postgresql://\$POSTGRES_USER:\$POSTGRES_PASSWORD@postgres:5432/${TASK_DB}?sslmode=disable"

# The geometry-source suite materialises real files through a writable scratch root. It is a
# test-side filesystem coordinate, so the runner owns it: derived from this run's identity,
# created before collection, and removed with the task database.
API_ROOT_DIR="/tmp/mesh-api-root-${RUN_ID}"
docker exec "$WORKER" sh -c "mkdir -p '$API_ROOT_DIR' && test -w '$API_ROOT_DIR'" \
  || { echo "FATAL: could not create a writable scratch root at $API_ROOT_DIR in the worker" >&2; exit 1; }

drop_scratch_root() {
  docker exec "$WORKER" rm -rf "$API_ROOT_DIR" >/dev/null 2>&1 || true
}

drop_task_db() {
  drop_scratch_root
  docker exec -e MESH_TEST_RUN_ID="$RUN_ID" "$WORKER" sh -c "
    PYTHONPATH=/srv python -c \"
import os, sys
sys.path.insert(0, '/srv')
from tests import disposable_database as dd
admin = os.path.expandvars('$ADMIN_URL_SH')
task  = os.path.expandvars('$TASK_URL_SH')
try:
    dd.drop(dd.authorize(task), admin)
    print('  dropped task database $TASK_DB')
except Exception as exc:
    print('  task database $TASK_DB not dropped: %s' % exc)
\"" || true
}

# THE STORAGE HALF OF DISPOSABILITY. The task database was always disposable; the bucket was not,
# so a tier that wrote objects wrote them into the shared bucket and, when its database went away,
# left them referenced by nothing - outside retention and outside reconciliation. Measured: one
# campaign stranded 496 objects that way. The bucket is now task-owned like the database, and its
# removal is proven rather than attempted.
CLEANUP_FAILED=0
LEDGER=""
# Declared BEFORE the trap that reads them. `set -u` is on, so a failure between installing the
# trap and resolving these would otherwise make the cleanup itself die on an unbound variable -
# the one moment cleanup most needs to work.
TASK_BUCKET=""
PROTECTED_BUCKET=""
PROTECTED_BEFORE=""
MULTIPART_BEFORE=""
drop_task_storage() {
  # Without the ledger there is no proof of which bucket this run owns, and cleanup must fail
  # rather than delete something it cannot check.
  if [ -z "$TASK_BUCKET" ] || [ ! -f "${LEDGER:-/nonexistent}" ]; then
    if [ -n "$TASK_BUCKET" ]; then
      echo "FATAL: the validation ledger is missing; refusing to delete $TASK_BUCKET" >&2
      CLEANUP_FAILED=1
    fi
    return 0
  fi
  docker exec -e MP_BEFORE="$MULTIPART_BEFORE" "$WORKER" sh -c "
    PYTHONPATH=/srv MINIO_BUCKET='$TASK_BUCKET' python -c \"
import sys
sys.path.insert(0, '/srv')
from tests import disposable_object_storage as dos
counts = dos.drop('$TASK_BUCKET')
print('  removed task bucket $TASK_BUCKET %s' % counts)
after = dos.fingerprint('$PROTECTED_BUCKET')
if after != '$PROTECTED_BEFORE':
    raise SystemExit('  PROTECTED BUCKET CHANGED: $PROTECTED_BEFORE -> %s' % after)
# Multipart uploads are counted rather than fingerprinted: the listing is server-wide, so the
# question that can be answered honestly is whether THIS run left more behind than it found.
import os
before_ids = {x for x in os.environ.get('MP_BEFORE', '').split(',') if x}
# Against the PROTECTED bucket, which still exists: the task bucket has just been deleted, and
# incomplete_uploads() returns [] for a bucket that is gone - so asking it there made this guard
# vacuous and let a run add seven uploads while reporting none.
added = sorted({u for _, u in dos.incomplete_uploads('$PROTECTED_BUCKET')} - before_ids)
if added:
    raise SystemExit('  RUN LEFT %d IN-PROGRESS MULTIPART UPLOAD(S): %s' % (len(added), added))
print('  protected bucket $PROTECTED_BUCKET unchanged (%s); no new multipart uploads' % after[:16])
\"" || CLEANUP_FAILED=1
}

finish() {
  local rc=$?
  drop_task_db
  drop_task_storage
  # A cleanup error is a FAILED RUN, never a warning: the whole point of the task bucket is that
  # its disappearance is provable, and an unproven cleanup is residue nobody will look for.
  [ -n "$LEDGER" ] && rm -f "$LEDGER"
  if [ "$CLEANUP_FAILED" -ne 0 ]; then
    echo "FATAL: validation storage cleanup failed - the run is not contained" >&2
    exit 1
  fi
  exit "$rc"
}
trap finish EXIT
# A cancelled run leaks exactly as much as a failed one. The default disposition for SIGINT/SIGTERM
# kills the shell without running the EXIT trap, so the signal is turned into an `exit` - which is
# what makes the cleanup above run for Ctrl-C and for a harness that terminates this process.
# 130/143 are the conventional codes for INT and TERM.
trap 'exit 130' INT
trap 'exit 143' TERM

# The protected bucket is whatever the worker is configured with BEFORE this run overrides it, and
# the task bucket is named from this run's identity. Both are resolved through the typed settings
# authority inside the container - never guessed here, and never read from a second environment.
PROTECTED_BUCKET="$(docker exec "$WORKER" sh -c "PYTHONPATH=/srv python -c \"
import sys; sys.path.insert(0,'/srv')
from tests import disposable_object_storage as dos
print(dos.protected_bucket())\"")" || { echo "FATAL: could not resolve the protected bucket" >&2; exit 1; }
TASK_BUCKET="meshtest-${RUN_ID}"
# THE CLEANUP CAPABILITY, WRITTEN BEFORE THE BUCKET EXISTS. The bucket name is the only thing that
# can ever abort this run's uploads - MinIO's abort is bucket-scoped and the listing is not - so it
# is recorded on disk before creation, where a killed child process cannot take it with it.
LEDGER="/tmp/mesh-validation-ledger-${RUN_ID}.json"
printf '{"invocation":"%s","bucket":"%s","endpoint":"%s","state":"declared"}\n' \
  "$RUN_ID" "$TASK_BUCKET" "${MINIO_ENDPOINT:-minio:9000}" > "$LEDGER"
echo "  ledger $LEDGER records bucket $TASK_BUCKET before it is created"

echo "  protected bucket $PROTECTED_BUCKET; this run writes to $TASK_BUCKET"
PROTECTED_BEFORE="$(docker exec "$WORKER" sh -c "PYTHONPATH=/srv python -c \"
import sys; sys.path.insert(0,'/srv')
from tests import disposable_object_storage as dos
dos.require_isolated('$TASK_BUCKET', protected='$PROTECTED_BUCKET')
dos.provision('$TASK_BUCKET', protected='$PROTECTED_BUCKET')
print(dos.fingerprint('$PROTECTED_BUCKET'))\"")" \
  || { echo "FATAL: refused to start - the validation bucket is not provably isolated" >&2; exit 1; }
echo "  protected bucket fingerprint before: ${PROTECTED_BEFORE:0:16}"

# The in-progress multipart uploads the server already had. Recorded as a baseline rather than
# asserted to be empty: the listing is server-wide, so pre-existing entries are not this run's.
# A COMMA-SEPARATED LIST OF IDS, never a Python literal: this value crosses two shell layers on
# its way back in, and embedding a repr there produced a SyntaxError that failed an otherwise
# clean run's cleanup.
MULTIPART_BEFORE="$(docker exec "$WORKER" sh -c "PYTHONPATH=/srv python -c \"
import sys; sys.path.insert(0,'/srv')
from tests import disposable_object_storage as dos
print(','.join(sorted(u for _, u in dos.server_multipart_uploads())))\"")" \
  || { echo "FATAL: could not read the multipart baseline" >&2; exit 1; }

# EVERY participating process must agree, and none may retain the protected bucket. Asserted from
# inside the container against the settings the code will actually read - the API app and the store
# are resolved here exactly as a test would resolve them.
docker exec -e MINIO_BUCKET="$TASK_BUCKET" "$WORKER" sh -c "PYTHONPATH=/srv python -c \"
import sys; sys.path.insert(0,'/srv')
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.object_storage.factory import build_object_store
from tests import disposable_object_storage as dos
resolved = dos.agree(provcfg.MINIO_BUCKET, build_object_store()._bucket)
dos.require_isolated(resolved, protected='$PROTECTED_BUCKET')
print('  every participating process resolves %s' % resolved)\"" \
  || { echo "FATAL: participating processes do not agree on an isolated bucket" >&2; exit 1; }

echo "  provisioning disposable database $TASK_DB (run $RUN_ID)"
docker exec -e MESH_TEST_RUN_ID="$RUN_ID" "$WORKER" sh -c "
  PYTHONPATH=/srv python -c \"
import os, sys
sys.path.insert(0, '/srv')
from tests import disposable_database as dd
dd.provision(os.path.expandvars('$ADMIN_URL_SH'))
\""

# `docker cp` against the ALREADY RESOLVED container, never `docker compose cp`: compose re-reads
# the project, rebuilds anything it considers out of date and recreates the service. That would
# silently make a stale image current in the middle of the run the freshness check just guarded,
# and it leaves the recreated containers stopped.
docker cp "tests/fixtures/external/geometry/elbow90_fluid.step" "$WORKER:/tmp/elbow90_fluid.step" >/dev/null 2>&1 || true

docker exec -e MESH_TEST_RUN_ID="$RUN_ID" -e API_ROOT="$API_ROOT_DIR" -e MINIO_BUCKET="$TASK_BUCKET" "$WORKER" sh -c "
  DATABASE_URL=\"$TASK_URL_SH\" PYTHONPATH=/srv exec python -m pytest -q -rs -o asyncio_mode=auto ${PYTEST_ARGS:-/srv/tests/integration}"
