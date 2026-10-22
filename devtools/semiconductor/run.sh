#!/usr/bin/env bash
# Responsibility: Run semi.py inside the mesh image, where OpenFOAM is installed.
# Boundaries: plumbing. It passes arguments through and decides nothing.
#
# OpenFOAM is not installed on the host and should not be: the mesh image is the one place its
# version is pinned. Everything else about a run - the part, the resolution, the ports - is
# semi.py's business.
#
#     devtools/semiconductor/run.sh list
#     devtools/semiconductor/run.sh check unit_cell_100um --across 5
#
# Run it from the repository root. The repository mounts at /work and runs land in ./runs, where
# they can be looked at afterwards.
#
# THE OPENFOAM BASHRC IS NOT SOURCED HERE, deliberately. OpenFOAM's etc/bashrc reads the shell's
# positional parameters as its own options - that is how `source etc/bashrc int64` works - so
# sourcing it in a shell that is holding this script's arguments hands it `check` and `--across`
# as configuration. It does not complain: it segfaults. engines/cfmesh/foam_exec sources it per
# command anyway, in a scrubbed environment, which is where it belongs.
set -euo pipefail

IMAGE="${HEXERA_MESH_IMAGE:-hexera-mesh:2412}"

if [ ! -d "devtools/semiconductor" ]; then
  echo "run this from the repository root (no devtools/semiconductor here)" >&2
  exit 2
fi

# Quote every argument into the command string rather than passing them as positional parameters,
# for the reason above.
quoted=""
for arg in "$@"; do quoted="$quoted $(printf '%q' "$arg")"; done

# --user THE CALLER, not root. A run writes its mesh, its logs and its reports into the mounted
# tree, and a container running as root leaves every one of them owned by root: the first serial
# solve left 262 files across 131 time directories that the person who started it could not
# delete. HOME has to be somewhere writable for that uid, hence /tmp.
#
# --network none because nothing in a meshing or solving run should be reaching the network. MPI
# still works: Open MPI falls back to shared memory and loopback on a single node.
exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp --network none \
  -v "$PWD":/work -w /work --entrypoint bash "$IMAGE" -c \
  "export PYTHONPATH=/work/src:/work/devtools/semiconductor
   export OPENFOAM_BASHRC=\"\${OPENFOAM_BASHRC:-/usr/lib/openfoam/openfoam2412/etc/bashrc}\"
   exec python3 /work/devtools/semiconductor/semi.py$quoted"
