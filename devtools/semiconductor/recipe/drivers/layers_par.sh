#!/bin/bash
# PHASE 2b, SECOND ATTEMPT: the same layers, in parallel, with the iteration count cut.
#
# WHY IT IS BEING REDONE. The first attempt ran snappyHexMesh in SERIAL and burned 52,021 s, 14.4
# hours, on one core of a 96 core box before I stopped it at 89.77% coverage with 4,022 illegal
# faces still outstanding. Nothing about layers is inherently slow here. Three things were wrong:
#   1. one core, so 16 ranks is the first and biggest change
#   2. nLayerIter 50, when coverage had effectively converged by about pass 10: passes 10 to 34
#      bought roughly half a percent of coverage and cost ten hours
#   3. nRelaxedIter 20, so it spent 20 passes on the strict quality bar before ever relaxing
#
# WHAT IS DELIBERATELY UNCHANGED, so the result is comparable to the gate and to attempt one:
# 3 layers, expansionRatio 1.5, firstLayerThickness 4.63e-06, maxThicknessToMedialRatio 0.6,
# minVol 1e-20, and only fluid_to_solid named.
#
# COVERAGE EXPECTATION, from measurement not hope. The 2a gate got 99.82% in the uniformly fine
# zone and 89.69% across a refinement cliff. Attempt one settled at 89.77% over the whole region.
# So about 90% is the honest expectation and the fine zone is where the layers actually matter.
set -u
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
W=/mnt/work/bl3p
SRC=/mnt/work/fsplit
RANKS=16
T=$W/TIMES.txt
own(){ chown -R 1000:1000 "$1" 2>/dev/null; chmod -R a+rwX "$1" 2>/dev/null; }

rm -rf $W; mkdir -p $W
echo "=== 0. the case, from the port-split coolant region ==="
t0=$(date +%s)
cp -r $SRC/constant $SRC/system $W/
rm -rf $W/constant/polyMesh/boundaryIndex $W/constant/polyMesh/boundaryQuality $W/constant/polyMesh/sets
: > $T; echo "copy 0 $(( $(date +%s) - t0 ))" >> $T
grep -m1 note $W/constant/polyMesh/owner
awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary | grep -E "^    [a-zA-Z_]|nFaces" | head -8

cat > $W/system/decomposeParDict <<DICT
FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains $RANKS;
method          hierarchical;
coeffs { n (4 2 2); }
DICT

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
    nLayerIter                 10;
    nRelaxedIter                3;
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
echo "=== 1. decomposePar into $RANKS ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; decomposePar -constant -force > decompose.log 2>&1"
rc=$?; echo "decompose $rc $(( $(date +%s) - t0 ))" >> $T
own $W
echo "   rc=$rc in $(( $(date +%s) - t0 ))s"
grep -E "Processor [0-9]+|Number of cells" $W/decompose.log 2>/dev/null | head -4
tail -3 $W/decompose.log 2>/dev/null

echo
echo "=== 2. layers, $RANKS ranks, 10 iterations, relaxing at 3 ==="
t0=$(date +%s)
docker run --rm --cpus=64 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; \
   mpirun --allow-run-as-root --oversubscribe -np $RANKS snappyHexMesh -parallel -overwrite \
   > snappy.log 2>&1"
rc=$?; echo "layers $rc $(( $(date +%s) - t0 ))" >> $T
own $W
echo "   rc=$rc in $(( $(date +%s) - t0 ))s, log $(stat -c %s $W/snappy.log 2>/dev/null) B"
echo "--- passes ---"; grep -cE "Extruding .* out of" $W/snappy.log 2>/dev/null
echo "--- coverage, first and last ---"
grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $W/snappy.log 2>/dev/null | head -1
grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $W/snappy.log 2>/dev/null | tail -1
grep -oE "Added [0-9]+ out of [0-9]+ cells \([0-9.]+%\)" $W/snappy.log 2>/dev/null | tail -1
grep -oE "Detected [0-9]+ illegal faces" $W/snappy.log 2>/dev/null | tail -1
echo "--- the layer table ---"
sed -n "/patch *faces *layers *overall thickness/,+6p" $W/snappy.log 2>/dev/null | tail -8
echo "--- verdict ---"
grep -E "Finished meshing|Layer mesh :" $W/snappy.log 2>/dev/null | tail -3
grep -E "FOAM FATAL|error" $W/snappy.log 2>/dev/null | head -4

echo
echo "=== 3. reconstruct ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; reconstructParMesh -constant > recon.log 2>&1"
rc=$?; echo "reconstruct $rc $(( $(date +%s) - t0 ))" >> $T
own $W
echo "   rc=$rc in $(( $(date +%s) - t0 ))s"
grep -m1 note $W/constant/polyMesh/owner 2>/dev/null
awk '/^[0-9]+$/{f=1} f' $W/constant/polyMesh/boundary 2>/dev/null | grep -E "^    [a-zA-Z_]|nFaces" | head -8

echo
echo "=== 4. checkMesh on the layered mesh ==="
t0=$(date +%s)
docker run --rm --cpus=32 -v "$W":/case -w /case "$IMG" bash -lc \
  "source /usr/lib/openfoam/openfoam2412/etc/bashrc; checkMesh -constant > check.log 2>&1; \
   checkMesh -constant -meshQuality > quality.log 2>&1"
echo "checkMesh $? $(( $(date +%s) - t0 ))" >> $T
own $W
grep -E "cells:|faces:|points:|hexahedra|prisms|polyhedra|Mesh OK|\*\*\*|non-orthogonality|skewness|aspect ratio|Number of regions|Total volume" \
  $W/check.log 2>/dev/null | head -20
sed -n "/Checking faces in error/,+11p" $W/quality.log 2>/dev/null | head -12
grep -E "^Mesh OK|^Failed" $W/quality.log 2>/dev/null | head -2

echo
df -h /mnt/work | tail -1
cat $T
echo "LAYERSPAR DONE $(date -u)"
