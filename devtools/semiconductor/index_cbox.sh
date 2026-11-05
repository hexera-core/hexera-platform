#!/bin/bash
# Build the boundary-index sidecar for a coupled workspace, so the mesh can be LOOKED at.
#   usage: index_cbox.sh <workspace-label> [regions...]
#   e.g.   index_cbox.sh cbox2 fluid solid chip
#
# WHY. A detail cut parses the whole boundary otherwise, and this plate delivers 21.7M faces on the
# coolant side alone. The index is that parse done once and written down: measured on the customer
# plate, 105 s and 623 MB for 13.2M faces, under 4% of the mesh on disk. Without it a 413M-cell
# mesh is a result nobody can open.
#
# TWO THINGS HAVE TO HAPPEN IN ORDER, and they belong to different runtimes:
#   checkMesh -writeFields  writes nonOrthoAngle/skewness/aspectRatio per cell, and only the
#                           OpenFOAM container has checkMesh
#   write_index + write_quality  parse polyMesh and reduce those fields, and only the repo has
#                           them - the mesh image carries meshpipeline but NOT meshpipeline.render
# So the fields are written in the container and the reduction runs on the host against a venv.
#
# IT WAITS FOR THE OWNING UNIT TO FINISH. Two checkMesh processes writing into constant/<region>/
# at the same time would clash, and cbox2's own chain ends with three serial checkMesh runs.

set -u
LAB=${1:-cbox2}
shift || true
REGIONS=${*:-fluid solid chip}
W=/mnt/work
c=$W/$LAB
B=gs://dev-rehaan-artifacts-224734058693
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
SRC=/opt/mp/src                       # the repo's src, pushed up beside this script
VENV=/opt/mp/venv
FIELDS="(nonOrthoAngle skewness aspectRatio)"
LOG=/var/log/index_$LAB.log
exec >>"$LOG" 2>&1
echo "================ index $LAB starting $(date -u)  regions: $REGIONS"

# 1. wait for the unit that owns this workspace, if it is still running
for i in $(seq 1 480); do
  systemctl is-active "$LAB" >/dev/null 2>&1 || break
  [ $((i % 20)) -eq 1 ] && echo "[wait] $LAB still active, $((i*30))s waited"
  sleep 30
done
echo "[wait] $LAB is $(systemctl is-active "$LAB" 2>&1), going ahead $(date -u)"

[ -d "$c/constant" ] || { echo "FATAL: no $c/constant"; exit 1; }
: > "$c/INDEX_TIMES.txt"

# 2. the fields, in the container
for r in $REGIONS; do
  [ -d "$c/constant/$r/polyMesh" ] || { echo "[skip] $r has no reconstructed polyMesh"; continue; }
  t0=$(date +%s)
  docker run --rm --cpus="$(nproc)" -v "$c":/case -w /case "$IMG" bash -lc \
    "source /usr/lib/openfoam/openfoam2412/etc/bashrc; checkMesh -constant -region $r -writeFields '$FIELDS' > checkFields.$r.log 2>&1"
  rc=$?
  echo "fields_$r $rc $(( $(date +%s) - t0 ))" >> "$c/INDEX_TIMES.txt"
  echo "[stage] fields $r rc=$rc in $(( $(date +%s) - t0 ))s"
  ls -la "$c/constant/$r"/{nonOrthoAngle,skewness,aspectRatio} 2>/dev/null | awk '{print "   ", $5, $9}'
done
chown -R 1000:1000 "$c" 2>/dev/null; chmod -R a+rwX "$c" 2>/dev/null

# 3. the index and the reduction, on the host
for r in $REGIONS; do
  [ -d "$c/constant/$r/polyMesh" ] || continue
  t0=$(date +%s)
  PYTHONPATH="$SRC" "$VENV/bin/python" - "$c/constant/$r/polyMesh" "$r" <<'PY'
import json
import sys
from pathlib import Path

from meshpipeline.render.boundary_index import write_index
from meshpipeline.render.boundary_quality import FIELD_FILES, write_quality

pm, region = Path(sys.argv[1]), sys.argv[2]
meta = write_index(pm, region=region)
print("  index   %-6s faces %-12s points %-12s buckets %-9s %s MB"
      % (region, f"{meta['n_faces']:,}", f"{meta['n_points']:,}",
         f"{meta['grid']['n_buckets']:,}", round(sum(meta["bytes"].values()) / 1e6, 1)))
try:
    q = write_quality(pm, region=region)
    print("  quality %-6s %s  %s MB" % (region, sorted(q["basis"]),
                                        round(sum(q["bytes"].values()) / 1e6, 1)))
    print("          basis %s" % json.dumps(q["basis"]))
    for fname in FIELD_FILES.values():
        f = pm.parent / fname
        if f.is_file():
            f.unlink()
except Exception as exc:                 # noqa: BLE001
    # A region with no quality is a cut with no COLOUR, not a cut that refuses. Named, not absent.
    print("  quality %-6s FAILED: %s" % (region, exc))
PY
  rc=$?
  echo "index_$r $rc $(( $(date +%s) - t0 ))" >> "$c/INDEX_TIMES.txt"
  echo "[stage] index $r rc=$rc in $(( $(date +%s) - t0 ))s"
done

# 4. the report, and the sidecar packed so it can be pulled down on its own
{
  echo "BOUNDARY INDEX for $LAB    $(date -u)"
  echo "regions: $REGIONS"
  echo
  awk '{printf "  %-12s rc=%-3s %6ss\n", $1, $2, $3}' "$c/INDEX_TIMES.txt"
  echo
  for r in $REGIONS; do
    m="$c/constant/$r/polyMesh/boundaryIndex/meta.json"
    [ -f "$m" ] || { echo "--- $r  NO INDEX"; continue; }
    echo "--- $r"
    "$VENV/bin/python" -c "
import json,sys
m=json.load(open('$m'))
print('  version %s  boundary faces %s  internal %s  points %s' % (
    m.get('version'), format(m.get('n_faces'),','), format(m.get('first_face'),','),
    format(m.get('n_points'),',')))
for p in m.get('patches') or []:
    print('    %-20s %-12s %s faces' % (p.get('name'), p.get('type'), format(p.get('n_faces'),',')))
print('  bytes %s' % json.dumps(m.get('bytes')))
"
    q="$c/constant/$r/polyMesh/boundaryQuality/meta.json"
    [ -f "$q" ] && "$VENV/bin/python" -c "
import json
q=json.load(open('$q'))
print('  quality basis %s' % json.dumps(q.get('basis')))
"
  done
  echo
  echo "=== sidecar size, which is what has to travel"
  du -sh "$c"/constant/*/polyMesh/boundaryIndex "$c"/constant/*/polyMesh/boundaryQuality 2>/dev/null
  du -sch "$c"/constant/*/polyMesh/boundaryIndex "$c"/constant/*/polyMesh/boundaryQuality 2>/dev/null | tail -1
  df -h /mnt/work | tail -1
} > "$c/INDEX_RESULT.txt"
cat "$c/INDEX_RESULT.txt"
gcloud storage cp "$c/INDEX_RESULT.txt" "$B/corintis/farfield/${LAB}_INDEX.txt" --quiet 2>/dev/null

# 5. pack the sidecar: the index, the quality, the boundary file and the region dicts, nothing else
PACK=$W/${LAB}_sidecar.tar
rm -f "$PACK" "$PACK.gz"
cd "$c" || exit 1
ARGS=""
for r in $REGIONS; do
  for d in "constant/$r/polyMesh/boundaryIndex" "constant/$r/polyMesh/boundaryQuality" \
           "constant/$r/polyMesh/boundary"; do
    [ -e "$d" ] && ARGS="$ARGS $d"
  done
done
[ -f constant/regionProperties ] && ARGS="$ARGS constant/regionProperties"
# shellcheck disable=SC2086
tar -cf "$PACK" $ARGS && gzip -1 "$PACK"
ls -la "$PACK.gz"
gcloud storage cp "$PACK.gz" "$B/corintis/farfield/${LAB}_sidecar.tar.gz" --quiet
echo "================ index $LAB done $(date -u)"
