#!/usr/bin/env bash
# Mesh one ECXML model end to end inside the DEPLOYED mesh image, the same way sweep.sh runs the
# layer sweep: the repository at /work, the lab mounted read-only, the caller's uid so the case is
# readable afterwards, no network, and OpenFOAM's bashrc left for foam_exec to source.
#
#   mesh_ecxml.sh <group>/<model> [extra snapgrid arguments...]
#
# The case lands in $LAB/cases/<group>_<model>.
set -uo pipefail
# Where this lab lives, worked out from this script rather than from one person's home.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${HEXERA_REPO:-$(cd "$HERE/../../.." && pwd)}"
LAB="${HEXERA_ECXML_LAB:-$REPO/.ecxml_lab}"
cd "$REPO"
IMAGE="${HEXERA_MESH_IMAGE:-5f88e7de3afd}"

WHICH="${1:?usage: mesh_ecxml.sh <group>/<model> [args...]}"
shift
SLUG="${WHICH//\//_}"
OUT="$LAB/cases/$SLUG"
mkdir -p "$LAB/cases"
rm -rf "$OUT"

quoted=""
for arg in "$@"; do quoted="$quoted $(printf '%q' "$arg")"; done

exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp --network none \
  -v "$PWD":/work -v "$LAB":/lab -w /work --entrypoint bash "$IMAGE" -c \
  "export PYTHONPATH=/work/src
   export OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2412/etc/bashrc
   exec python3 -m meshpipeline.engines.snapgrid.cli \
     /lab/models/${WHICH}.ecxml /lab/cases/${SLUG} \
     --bashrc /usr/lib/openfoam/openfoam2412/etc/bashrc$quoted"
