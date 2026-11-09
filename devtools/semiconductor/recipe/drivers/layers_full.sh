#!/bin/bash
# PHASE 2b: three boundary layers on the whole coolant region. Runs ON corintis-farfield, detached.
#
# WHY NOW. The 2a gate passed at 99.82% coverage in the fine zone and 89.69% across the refinement
# cliff, so the route is alive on this part. The audit also removed the plan's reason for deferring
# layers: the "two errors cancel to within 2%" argument was wrong, because Nusselt is about 4% HIGH
# rather than 8.7% low, so area and gradient errors COMPOUND to +12 to +16% on total heat. Fixing
# the near-wall gradient is therefore worth doing rather than neutral. And cbox2b itself exists on
# this disk alone, which self-deletes, so this is the window.
#
# THREE LAYERS, NOT FOUR. 4 fluid-side layers are 609.1M cells and break the plan's 600M line. 3 are
# 583.4M at 100% coverage and, at the coverage the gate actually measured, about 572M.
#
# THE RECIPE IS THE GATE'S, UNCHANGED, so the results are comparable: 3 layers, ER 1.5,
# firstLayerThickness 4.63e-06, maxThicknessToMedialRatio 0.6, minVol 1e-20.
#
# ONLY fluid_to_solid IS NAMED. The research measured that naming the region's other patches at
# nSurfaceLayers 0 gives 93.1% coverage with polyhedra, while saying nothing about them gives 100%
# and all-hex. port_a and port_b are therefore left unnamed on purpose.
#
# maxGlobalCells IS CAPPED AT 2^31-1. The first attempt set it to 4e9 and FOAM exited with
# "Expected integral (int32), value out-of-range: double 4e+09" after 689 s, 665 of which it had
# already spent reading the mesh and running the edge-intersection pass. The label size caps the
# dict entry as well as the mesh.
#
# ON A COPY. /mnt/work/fsplit already holds the port-split coolant region as its own case. The
# phase-1 deliverable in cbox2b is not touched by any of this.
set -u
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
W=/mnt/work/bl3
SRC=/mnt/work/fsplit
B=gs://dev-rehaan-artifacts-224734058693
T=$W/LAYER_TIMES.txt
own(){ chown -R 1000:1000 "$1"; chmod -R a+rwX "$1"; }

rm -rf $W; mkdir -p $W
echo "=== 0. the case, from the port-split coolant region ==="
t0=$(date +%s)
cp -r $SRC/constant $SRC/system $W/
rm -rf $W/constant/polyMesh/boundaryIndex $W/constant/polyMesh/boundaryQuality \
       $W/constant/polyMesh/sets
: > $T; echo "copy 0 $(( $(date +%s) - t0 ))" >> $T
du -sh $W
grep -m1 note $W/constant/polyMesh/owner
awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary | grep -E "^    [a-zA-Z_]|nFaces" | head -10

cat > $W/system/snappyHexMeshDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object snappyHexMeshDict; }
castellatedMesh false;
snap            false;
addLayers       true;
geometry        {}
castellatedMeshControls
{
    maxLocalCells        400000000;
    maxGlobalCells      2000000000;
    minRefinementCells           0;
    maxLoadUnbalance          0.10;
    nCellsBetweenLevels          1;
    features ();
    refinementSurfaces {}
    resolveFeatureAngle 30;
    refinementRegions {}
    locationInMesh (0.006796 0.022553 0.041859);
    allowFreeStandingZoneFaces false;
}
snapControls { nSmoothPatch 3; tolerance 2.0; nSolveIter 30; nRelaxIter 5; }
addLayersControls
{
    relativeSizes false;
    layers { fluid_to_solid { nSurfaceLayers 3; } }
    expansionRatio          1.5;
    firstLayerThickness     4.63e-06;
    minThickness            4.0e-07;
    nGrow 0;
    featureAngle              180;
    maxFaceThicknessRatio     0.5;
    nSmoothSurfaceNormals       1;
    nSmoothThickness           10;
    minMedialAxisAngle         90;
    maxThicknessToMedialRatio 0.6;
    nSmoothNormals              3;
    slipFeatureAngle           30;
    nRelaxIter                  5;
    nBufferCellsNoExtrude       0;
    nLayerIter                 50;
    nRelaxedIter               20;
}
meshQualityControls
{
    #include "meshQualityDict"
    relaxed { maxNonOrtho 75; }
    nSmoothScale 4;
    errorReduction 0.75;
}
writeFlags ();
mergeTolerance 1e-6;
DICT
own $W
grep minVol $W/system/meshQualityDict

echo
echo "=== 1. add the layers. about 2.0 h expected, extrapolated from the gate's 136 s on"
echo "===    495,965 faces against 25,685,941 here ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; snappyHexMesh -overwrite > snappy.log 2>&1"
rc=$?; echo "snappy $rc $(( $(date +%s) - t0 ))" >> $T
own $W
echo "   rc=$rc after $(( $(date +%s) - t0 ))s, log $(stat -c %s $W/snappy.log) B"
echo "--- coverage, first and last ---"
grep -E "Extruding [0-9]+ out of" $W/snappy.log | head -2
grep -E "Extruding [0-9]+ out of" $W/snappy.log | tail -2
grep -E "Added [0-9]+ out of" $W/snappy.log | tail -2
echo "--- the layer table ---"
sed -n "/patch *faces *layers *overall thickness/,+6p" $W/snappy.log | tail -8
echo "--- verdict ---"
grep -E "Finished meshing" $W/snappy.log | tail -2
grep -E "Layer mesh :" $W/snappy.log | tail -2
echo "--- faces in error ---"
sed -n "/Checking faces in error/,+11p" $W/snappy.log | tail -12

echo
echo "=== 2. the interface must not have moved off 25,685,941 ==="
awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary | grep -E "^    [a-zA-Z_]|nFaces" | head -10

echo
echo "=== 3. checkMesh, then -meshQuality with the corrected minVol ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; checkMesh -constant > check.log 2>&1"
echo "checkMesh $? $(( $(date +%s) - t0 ))" >> $T
own $W
grep -E "cells:|faces:|points:|hexahedra|prisms|polyhedra|Mesh OK|\*\*\*|non-orthogonality|skewness|aspect ratio|Number of regions|Total volume" \
  $W/check.log | head -20
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; checkMesh -constant -meshQuality > quality.log 2>&1"
echo "quality $? $(( $(date +%s) - t0 ))" >> $T
own $W
sed -n "/Checking faces in error/,+11p" $W/quality.log | head -12
grep -E "Mesh OK|Failed" $W/quality.log | head -3

echo
echo "=== 4. index it, so the layered mesh can be looked at ==="
t0=$(date +%s)
env PYTHONPATH=/opt/mp/src /opt/mp/venv/bin/python /mnt/work/bin/idx_fluid.py \
  $W/constant/polyMesh fluid
echo "index $? $(( $(date +%s) - t0 ))" >> $T
own $W

echo
echo "=== 5. the record and the sidecar, up ==="
t0=$(date +%s)
R=/mnt/work/bl3_record; rm -rf $R; mkdir -p $R
cp $W/*.log $W/*.txt $R/ 2>/dev/null
cp -r $W/system $R/ 2>/dev/null
cp $W/constant/polyMesh/boundary $R/fluid.boundary 2>/dev/null
tar -C /mnt/work -czf /mnt/work/bl3_record.tar.gz bl3_record
gcloud storage cp /mnt/work/bl3_record.tar.gz "$B/corintis/runs/bl3/bl3_record.tar.gz" 2>&1 | tail -1
d=$W/constant/polyMesh
gcloud storage cp "$d/boundary" "$B/corintis/sidecar/bl3/constant/fluid/polyMesh/boundary" 2>&1 | tail -1
gcloud storage rsync -r "$d/boundaryIndex" \
  "$B/corintis/sidecar/bl3/constant/fluid/polyMesh/boundaryIndex" 2>&1 | tail -2
echo "upload $? $(( $(date +%s) - t0 ))" >> $T
rm -rf /mnt/work/vbl3; mkdir -p /mnt/work/vbl3
gcloud storage cp "$B/corintis/runs/bl3/bl3_record.tar.gz" /mnt/work/vbl3/ 2>&1 | tail -1
tar -C /mnt/work/vbl3 -xzf /mnt/work/vbl3/bl3_record.tar.gz
diff -r $R /mnt/work/vbl3/bl3_record >/dev/null && echo "   RECORD VERIFIED, byte identical" \
  || echo "   RECORD DIFFERS"
rm -rf /mnt/work/vbl3

echo
df -h /mnt/work | tail -1
cat $T
echo "LAYERS DONE $(date -u)"
