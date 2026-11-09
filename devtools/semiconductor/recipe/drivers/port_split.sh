#!/bin/bash
# Split cbox2b's single 4,140-face fluid_to_air into port_a and port_b, as the shipped mesh carries
# them. Runs ON corintis-farfield, detached, after the sidecar chain releases the mesh.
#
# WHY. cbox2 ships port_a (1,759 faces) and port_b (1,706) as separate patches of type `patch`, so a
# solver can put an inlet on one and an outlet on the other. cbox2b has one fluid_to_air of 4,140 of
# type mappedWall. cbox2's own RESULT.txt shows its run ALSO produced a single fluid_to_air, of
# 3,465 = 1,759 + 1,706 faces, so this is a post-processing step that was applied there and not
# here. Without it the mesh cannot carry a flow boundary condition at all.
#
# WHERE THE CUT GOES. Measured off cbox2's own index: port_a spans z 73.507 to 81.252 mm and port_b
# z 39.357 to 47.279 mm, completely disjoint, and cbox2b's merged patch spans 39.414 to 81.277. So
# z = 60 mm separates them with 13 mm of clearance either side. It is not a guess.
#
# ON A COPY, DELIBERATELY. createPatch -overwrite rewrites boundary and reorders faces. The mesh
# itself is on this disk only, since the archive policy here is sidecars rather than meshes, and the
# disk self-deletes. So the work happens on a copy and is swapped in only after checkMesh agrees.
set -u
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
C=/mnt/work/cbox2b
W=/mnt/work/fsplit
B=gs://dev-rehaan-artifacts-224734058693
T=$W/SPLIT_TIMES.txt
own(){ chown -R 1000:1000 "$1"; chmod -R a+rwX "$1"; }

echo "=== 0. wait for the sidecar chain, which is reading this mesh ==="
for i in $(seq 1 480); do
  pgrep -f "bin/finis[h]_bg" >/dev/null 2>&1 || break
  [ $((i % 10)) -eq 1 ] && echo "   chain still running, $((i*30))s"
  sleep 30
done
echo "   clear at $(date -u)"
cat $C/FINISH_TIMES.txt 2>/dev/null

echo
echo "=== 1. copy the coolant region into its own case ==="
rm -rf $W; mkdir -p $W/constant $W/system
t0=$(date +%s)
cp -r $C/constant/fluid/polyMesh $W/constant/polyMesh
rm -rf $W/constant/polyMesh/boundaryIndex $W/constant/polyMesh/boundaryQuality \
       $W/constant/polyMesh/sets
cp $C/system/controlDict $C/system/fvSchemes $C/system/fvSolution \
   $C/system/meshQualityDict $W/system/ 2>/dev/null
: > $T; echo "copy 0 $(( $(date +%s) - t0 ))" >> $T
du -sh $W

cat > $W/system/topoSetDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object topoSetDict; }
actions
(
    { name portA; type faceSet; action new;    source patchToFace; patch fluid_to_air; }
    { name portA; type faceSet; action subset; source boxToFace;
      box (-1 -1 0.060) (1 1 1); }
    { name portB; type faceSet; action new;    source patchToFace; patch fluid_to_air; }
    { name portB; type faceSet; action subset; source boxToFace;
      box (-1 -1 -1) (1 1 0.060); }
);
DICT

cat > $W/system/createPatchDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object createPatchDict; }
pointSync false;
patches
(
    { name port_a; patchInfo { type patch; } constructFrom set; set portA; }
    { name port_b; patchInfo { type patch; } constructFrom set; set portB; }
);
DICT
own $W

echo
echo "=== 2. carve the two face sets. 1,759 and 1,706 is what the shipped mesh has; the counts"
echo "===    here will be larger in the same proportion, because the cells are finer ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; topoSet -constant > topoSet.log 2>&1"
echo "topoSet $? $(( $(date +%s) - t0 ))" >> $T
own $W
grep -E "faceSet port. now size|FATAL|Cannot find" $W/topoSet.log | head -6

echo
echo "=== 3. make them patches ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; createPatch -overwrite > createPatch.log 2>&1"
rc=$?; echo "createPatch $rc $(( $(date +%s) - t0 ))" >> $T
own $W
echo "   rc=$rc"
grep -E "Adding new patch|Removing patch|moved|FATAL|Writing repatched" $W/createPatch.log | head -12

echo
echo "=== 4. the boundary it produced ==="
awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary | grep -E "^    [a-zA-Z_]|nFaces|type " | head -20

echo
echo "=== 5. checkMesh, which decides whether this is swapped in ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; checkMesh -constant > check.log 2>&1"
echo "checkMesh $? $(( $(date +%s) - t0 ))" >> $T
own $W
grep -E "cells:|faces:|points:|Mesh OK|\*\*\*|non-orthogonality|skewness|aspect ratio|Number of regions" \
  $W/check.log | head -14

OK=$(grep -c "^Mesh OK" $W/check.log)
NA=$(awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary | grep -A3 "port_a" | grep -oP "nFaces\s+\K[0-9]+" | head -1)
NB=$(awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary | grep -A3 "port_b" | grep -oP "nFaces\s+\K[0-9]+" | head -1)
echo
echo "   Mesh OK lines: $OK   port_a: ${NA:-none}   port_b: ${NB:-none}   sum: $(( ${NA:-0} + ${NB:-0} )) of 4140"

if [ "$OK" -ge 1 ] && [ "$(( ${NA:-0} + ${NB:-0} ))" -eq 4140 ]; then
  echo
  echo "=== 6. swap it in, keeping the old boundary for the record ==="
  cp $C/constant/fluid/polyMesh/boundary /mnt/work/cbox2b_fluid_boundary_before_portsplit
  rm -rf $C/constant/fluid/polyMesh.preportsplit
  mv $C/constant/fluid/polyMesh $C/constant/fluid/polyMesh.preportsplit
  cp -r $W/constant/polyMesh $C/constant/fluid/polyMesh
  # the quality fields belong with the mesh they were written for; they are per CELL and the cells
  # did not change, so they carry over untouched
  cp $C/constant/fluid/polyMesh.preportsplit/../nonOrthoAngle /dev/null 2>/dev/null || true
  own $C/constant/fluid
  echo "   swapped. new boundary:"
  awk '/^[0-9]+$/{f=1} f' $C/constant/fluid/polyMesh/boundary | grep -E "^    [a-zA-Z_]|nFaces" | head -12

  echo
  echo "=== 7. rebuild the coolant sidecar, since the patch ids changed ==="
  t0=$(date +%s)
  env PYTHONPATH=/opt/mp/src /opt/mp/venv/bin/python /mnt/work/bin/idx_fluid.py \
    $C/constant/fluid/polyMesh fluid
  echo "reindex $? $(( $(date +%s) - t0 ))" >> $T
  t0=$(date +%s)
  PYTHONPATH=/opt/mp/src /opt/mp/venv/bin/python - "$C/constant/fluid/polyMesh" fluid <<'PY'
import sys
from pathlib import Path
from meshpipeline.render.boundary_quality import write_quality
q = write_quality(Path(sys.argv[1]), region=sys.argv[2])
print("   quality fluid %s  %s MB" % (sorted(q["basis"]), round(sum(q["bytes"].values()) / 1e6, 1)))
PY
  echo "requality $? $(( $(date +%s) - t0 ))" >> $T
  own $C

  echo
  echo "=== 8. re-upload the coolant sidecar ==="
  d=$C/constant/fluid/polyMesh
  gcloud storage cp "$d/boundary" \
    "$B/corintis/sidecar/cbox2b/constant/fluid/polyMesh/boundary" 2>&1 | tail -1
  gcloud storage rsync -r "$d/boundaryIndex" \
    "$B/corintis/sidecar/cbox2b/constant/fluid/polyMesh/boundaryIndex" 2>&1 | tail -2
  [ -d "$d/boundaryQuality" ] && gcloud storage rsync -r "$d/boundaryQuality" \
    "$B/corintis/sidecar/cbox2b/constant/fluid/polyMesh/boundaryQuality" 2>&1 | tail -2
  echo "   re-uploaded"
  rm -rf $C/constant/fluid/polyMesh.preportsplit
  echo "   old polyMesh removed; the pre-split boundary is kept at"
  echo "   /mnt/work/cbox2b_fluid_boundary_before_portsplit"
else
  echo
  echo "=== NOT SWAPPING IN. checkMesh did not say Mesh OK, or the face counts do not sum to 4,140."
  echo "===  The deliverable mesh is untouched. The attempt is at $W for inspection."
fi

echo
cat $T
echo "PORTSPLIT DONE $(date -u)"
