#!/bin/bash
# Build a mesh image from THIS branch by layering its wheel onto the proven mesh image.
#
# WHY NOT THE Dockerfile's `mesh` TARGET. That target builds the native floor from scratch:
# OpenFOAM 2412 from the ESI apt repository (SourceForge mirrors, which the Dockerfile itself
# calls "the least reliable thing in this build"), vmtk through conda, and fTetWild from source.
# ~2 GB and slow. None of it changed on this branch - only the Python package did - so the floor
# is reused and the wheel is swapped on top of it.
set -euo pipefail
cd ~/hexera-platform-semi

BASE=us-central1-docker.pkg.dev/hexera/mesh/mesh:ff752ca1df02
SHA=$(git rev-parse --short HEAD)
STAMP=$(date +%Y%m%d-%H%M)
TAG=us-central1-docker.pkg.dev/hexera/mesh/mesh:semi-${SHA}-${STAMP}

echo "########## who the base image runs as ##########"
USERSPEC=$(docker inspect "$BASE" --format '{{.Config.User}}')
echo "USER = '${USERSPEC}'"

echo "########## the wheel ##########"
# ALWAYS REBUILT. This used to be `[ -f dist/...whl ] || build`, which skipped the build whenever a
# wheel from any earlier run was still in dist/ - so every image after the first shipped STALE
# source while carrying a tag named after the current commit. Measured: the image tagged
# semi-32417451 had no NO_FIELD_TIME and no prefer_finished, both committed two and three commits
# before it, and two live end-to-end runs reproduced bugs that were already fixed. dist/ goes
# first, and the smoke test below now checks a value from the newest commit rather than only that
# the package imports.
rm -rf dist build src/*.egg-info
.venv/bin/python -m build --wheel 2>&1 | tail -2
ls -la dist/*.whl

mkdir -p /tmp/semi-image && rm -f /tmp/semi-image/*.whl
cp dist/*.whl /tmp/semi-image/

# AS ROOT FOR THE INSTALL, then back. Run as the image's own user, pip says "Defaulting to user
# installation because normal site-packages is not writeable" and the wheel lands in that user's
# home - which is the BUILD user's home, not necessarily the one Cloud Run runs as. Installing
# into site-packages is what makes the swap the same artefact the base image already carries.
cat > /tmp/semi-image/Dockerfile <<EOF
FROM ${BASE}
USER root
COPY *.whl /tmp/
RUN python3 -m pip install --no-cache-dir --no-deps --force-reinstall \
      --root-user-action=ignore /tmp/*.whl && rm -f /tmp/*.whl
RUN python3 - <<'PY'
import sys
from meshpipeline.engines.registry import all_engine_names
names = sorted(all_engine_names())
print("engines:", names)
if "cfmesh_conjugate" not in names:
    sys.exit("cfmesh_conjugate is NOT in this image")
import meshpipeline
print("installed at", meshpipeline.__file__)
PY
USER ${USERSPEC:-root}
EOF
cat /tmp/semi-image/Dockerfile

echo "########## build ##########"
docker build --progress=plain --build-arg BUILDKIT_INLINE_CACHE=1 \
  -t "$TAG" /tmp/semi-image 2>&1 | grep -vE "^#[0-9]+ (sha256|extracting|transferring)" | tail -30

echo "########## what the image can dispatch ##########"
docker run --rm "$TAG" python3 -c "
from meshpipeline.engines.registry import all_engine_names
print('engines:', sorted(all_engine_names()))
from meshpipeline.engines.cfmesh_conjugate.conjugate import OCTREE_BOXES_PER_PROCESS, MPI_FLAGS
print('ceiling:', f'{OCTREE_BOXES_PER_PROCESS:,}', ' mpi:', MPI_FLAGS)
from meshpipeline.engines.cfmesh_conjugate.solver_case import BLANK
print('case blank:', BLANK)
# WHAT THIS COMMIT ADDED, not only that the package imports. A tag is a promise about the source
# and the only way to keep it is to look for something that was not there before.
import sys
from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE, review_surface as RS
from meshpipeline.adapters.mesh_execution import cloud_run_client as CRC
import inspect
checks = {
  'export excludes the field time': 'NO_FIELD_TIME' in inspect.getsource(FE.export_volume_vtk),
  'a truncated export is not believed': 'prefer_finished' in inspect.getsource(RS._newest_time),
  'the reported cell count raises the ceiling': CRC.result_extraction_limits(
      {'cells': 6582471}).max_archive_bytes > 900_000_000,
  'the tail goes parallel': 'RECONSTRUCT_REGIONS' in dir(
      __import__('meshpipeline.engines.cfmesh_conjugate.conjugate', fromlist=['x'])),
}
for k, v in checks.items():
    print(('  OK   ' if v else '  MISSING ') + k)
if not all(checks.values()):
    sys.exit('this image is STALE - the wheel does not match the source')
import meshpipeline; print('from', meshpipeline.__file__)
"
echo "$TAG" > /tmp/semi-image/TAG
echo "TAG written: $TAG"
