#!/bin/bash
# FIX RUN 2: V4, the patch split. Layers on the channel wall only.
#
# WHY THIS AND NOT A DICT KNOB. The 2,349 bad angles on the shipped coolant are 89.14% on the
# coarse plenum wall and 9.88% on channel wall, and nothing in addLayersControls can drop layers
# from COARSE faces: maxFaceThicknessRatio excludes where thickness is LARGE relative to the
# face, which is the wrong direction, and minThickness scales the wrong way too. The only
# mechanism is to stop asking for layers there, which means splitting the patch.
#
# WHAT V1 ALREADY SHOWED. relativeSizes true cut severely non-orthogonal faces from 2,096 to 48
# and the maximum from 76.6676 to 74.1616, which confirms angle = atan(drift/thickness) with
# drift set by cell width. But it pushed interpolation-weight failures from 405 to 2,639, and
# 2,639 is exactly port_a + port_b (1,331 + 1,308), so the thinner stack went bad in a ring
# around the two ports. V4 therefore keeps the SHIPPED absolute thickness and removes the coarse
# wall instead, which should leave the port ring where bl8 had it.
#
# PREDICTION, recorded before the run: about 842 faces in error (2,936 minus the 2,094 coarse
# wall faces), and roughly 2M fewer cells. If it comes out near that, the mechanism is nailed.
#
# The split is by FACE AREA, which is a direct proxy for refinement level: a stretched level-4
# face is 1.8987e-09 m2, a stretched level-3 face is 7.595e-09, so 3.8e-09 separates them with
# an order of magnitude of margin. chanset writes both halves as ASCII faceSets so createPatch
# can take fluid_to_solid to zero and the two halves can be merged back afterwards with no name
# collision. Merging back matters: the delivery must keep ONE fluid_to_solid of 16,619,777 faces
# matching solid_to_fluid, and mappedWall samples geometrically so the face order is free.
set -u
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
B=gs://dev-rehaan-artifacts-224734058693/corintis
W=/mnt/work
FOAM="source /usr/lib/openfoam/openfoam2412/etc/bashrc"
T=$W/V4_TIMES.txt
V=$W/V4
stamp(){ echo "$1 $2 $(( $(date +%s) - $3 ))" >> $T; }
own(){ chown -R 1000:1000 "$1" 2>/dev/null; chmod -R a+rwX "$1" 2>/dev/null; }
dkv(){ docker run --rm --cpus="$1" -v "$V":/case -w /case "$IMG" bash -lc "$FOAM; $2"; }

rm -rf $W/raw $V          # a rerun must not inherit a half-built V4
mkdir -p $W/dl $V
: > $T
echo "################ FIX RUN 2, V4 patch split  $(date -u) ################"
echo "cores $(nproc)  mem $(free -g | awk '/^Mem/{print $2}')GB"

echo
echo "=== 0. the coolant, already port split in the archive ==="
t0=$(date +%s)
gsutil -q cp "$B/mesh/cbox8/cbox8_fluid.tar.gz" $W/dl/ || { echo FATAL; exit 1; }
gsutil -q cp "$B/drivers/chanset.cpp" $W/chanset.cpp || { echo FATAL; exit 1; }
stamp download 0 $t0
t0=$(date +%s)
mkdir -p $W/raw && cd $W/raw && tar xzf $W/dl/cbox8_fluid.tar.gz
stamp extract 0 $t0
# a single-region case keeps polyMesh at the top of constant, not under constant/fluid
for p in $W/raw/processor*; do
  pn=$(basename $p)
  mkdir -p $V/$pn/constant
  mv $p/constant/fluid/polyMesh $V/$pn/constant/polyMesh
done
NP=$(ls -d $V/processor* | wc -l)
RANKS=$NP
mkdir -p $V/system
cat > $V/system/controlDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application     chtMultiRegionFoam;
startFrom       startTime;  startTime 0;  stopAt endTime;  endTime 1;  deltaT 1;
writeControl    timeStep;   writeInterval 1;
writeFormat     ascii;      writeCompression off;
timeFormat general; timePrecision 6; runTimeModifiable true;
DICT
cat > $V/system/meshQualityDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object meshQualityDict; }
maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80;
minVol 1e-20; minTetQuality 1e-30; minArea -1; minTwist 0.02; minDeterminant 0.001;
minFaceWeight 0.02; minVolRatio 0.01; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75;
DICT
cat > $V/system/fvSchemes <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes { default steadyState; } gradSchemes { default Gauss linear; }
divSchemes { default none; } laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; } snGradSchemes { default corrected; }
DICT
cat > $V/system/fvSolution <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers { ".*" { solver PCG; preconditioner DIC; tolerance 1e-6; relTol 0; } }
DICT
cat > $V/system/decomposeParDict <<DICT
FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains $NP;
method scotch;
DICT
own $V
echo "   nProcs $NP"
grep -m1 note $V/processor0/constant/polyMesh/owner | head -c 200; echo
echo "   patches:"
grep -E "^    [a-zA-Z_]+$|^        type|nFaces" $V/processor0/constant/polyMesh/boundary | paste - - - | grep -v processor

echo
echo "=== 1. write the two faceSets by face area ==="
t0=$(date +%s)
apt-get install -y -qq g++ >/dev/null 2>&1
g++ -O2 -D_FILE_OFFSET_BITS=64 -o $W/chanset $W/chanset.cpp -lm || { echo "FATAL: build"; exit 1; }
$W/chanset $V fluid_to_solid ftsChan 3.8e-9
rc=$?; stamp chanset $rc $t0
if [ $rc -ne 0 ]; then echo "FATAL: the area gate did not match the measurement, stopping"; exit 1; fi
own $V
echo "   set sizes on proc0: chan $(sed -n '9p' $V/processor0/constant/polyMesh/sets/ftsChan) rest $(sed -n '9p' $V/processor0/constant/polyMesh/sets/ftsChanRest)"

echo
echo "=== 2. split fluid_to_solid into fts_chan and fts_plen ==="
cat > $V/system/createPatchDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object createPatchDict; }
pointSync false;
patches
(
    {
        name fts_chan;
        patchInfo { type mappedWall; inGroups 1(wall); sampleMode nearestPatchFace;
                    sampleRegion solid; samplePatch solid_to_fluid; }
        constructFrom set; set ftsChan;
    }
    {
        name fts_plen;
        patchInfo { type mappedWall; inGroups 1(wall); sampleMode nearestPatchFace;
                    sampleRegion solid; samplePatch solid_to_fluid; }
        constructFrom set; set ftsChanRest;
    }
);
DICT
own $V
t0=$(date +%s)
dkv 32 "mpirun --allow-run-as-root --oversubscribe -np $RANKS createPatch -parallel -overwrite > split.log 2>&1"
rc=$?; stamp split $rc $t0; own $V
echo "   rc=$rc"
grep -E "Read [0-9]+ faces|Adding new patch|Removing patches|Removed" $V/split.log | head -10
echo "   patches now:"
grep -E "^    [a-zA-Z_]+$|^        type|nFaces" $V/processor0/constant/polyMesh/boundary | paste - - - | grep -v processor

# HARD GATE. The first attempt at this run failed because createPatch rejected -constant, so the
# split never happened, and snappyHexMesh then went on to report "Finished meshing without any
# errors" in 143 s having added ZERO layers, because the patch it was told to layer did not
# exist. A silent success is the worst failure mode there is. Do not proceed without the patch.
if [ $rc -ne 0 ]; then echo "FATAL: createPatch failed, see split.log"; sed -n '1,40p' $V/split.log; exit 1; fi
if ! grep -q "fts_chan" $V/processor0/constant/polyMesh/boundary; then
  echo "FATAL: fts_chan is not in the boundary file, so the split did not take. Stopping before"
  echo "       snappyHexMesh can pretend to succeed while adding no layers."
  exit 1
fi
CHANTOT=$(python3 -c "
import glob,re
t=0
for p in sorted(glob.glob('/mnt/work/V4/processor*/constant/polyMesh/boundary')):
    s=open(p).read()
    m=re.search(r'fts_chan\s*\{[^}]*?nFaces\s+(\d+)', s, re.S)
    if m: t+=int(m.group(1))
print(t)
")
echo "   fts_chan across processors: $CHANTOT"
if [ "$CHANTOT" -lt 15000000 ]; then echo "FATAL: fts_chan is far too small"; exit 1; fi

echo
echo "=== 3. layers on fts_chan ONLY, with the SHIPPED absolute thickness ==="
cat > $V/system/snappyHexMeshDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object snappyHexMeshDict; }
castellatedMesh false;
snap            false;
addLayers       true;
geometry        {}
castellatedMeshControls
{
    maxLocalCells 400000000; maxGlobalCells 2000000000; minRefinementCells 0;
    maxLoadUnbalance 0.10; nCellsBetweenLevels 1; features ();
    refinementSurfaces {} resolveFeatureAngle 30; refinementRegions {}
    locationInMesh (0.006796 0.022553 0.041859);
    allowFreeStandingZoneFaces false;
}
snapControls { nSmoothPatch 3; tolerance 2.0; nSolveIter 30; nRelaxIter 5; }
addLayersControls
{
    relativeSizes false;
    layers { fts_chan { nSurfaceLayers 3; } }
    expansionRatio          1.2;
    firstLayerThickness     4.15e-06;
    minThickness            3.0e-06;
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
own $V
t0=$(date +%s)
docker run --rm --cpus=56 -v "$V":/case -w /case "$IMG" bash -lc \
  "$FOAM; mpirun --allow-run-as-root --oversubscribe -np $RANKS snappyHexMesh -parallel -overwrite > snappy.log 2>&1"
rc=$?; stamp layers $rc $t0; own $V
echo "   rc=$rc in $(( $(date +%s) - t0 ))s"
grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $V/snappy.log | head -1 | sed 's/^/     /'
grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $V/snappy.log | tail -1 | sed 's/^/     /'
grep -oE "Added [0-9]+ out of [0-9]+ cells \([0-9.]+%\)" $V/snappy.log | tail -1 | sed 's/^/     /'
sed -n "/patch *faces *layers *overall thickness/,+5p" $V/snappy.log | tail -4 | sed 's/^/     /'
grep -E "Layer mesh :|Finished meshing" $V/snappy.log | tail -2 | sed 's/^/     /'
grep -E "FOAM FATAL" $V/snappy.log | head -3 | sed 's/^/     /'
# Second gate: layers must actually have been added. Zero outer iterations means snappy was
# asked to layer a patch it could not find and said nothing about it.
ITERS=$(grep -cE "Extruding .* out of" $V/snappy.log)
echo "     outer iterations: $ITERS"
if [ "$ITERS" -lt 2 ]; then echo "FATAL: no layer iterations ran, so no layers were added"; exit 1; fi

echo
echo "=== 4. quality on the split mesh ==="
t0=$(date +%s)
dkv 32 "mpirun --allow-run-as-root --oversubscribe -np $RANKS checkMesh -parallel -constant -meshQuality > quality_split.log 2>&1"
stamp quality_split $? $t0; own $V
grep -E "cells:|Number of regions|non-orthogonality Max|severely non-orth|Max skewness|Max aspect|Total volume|non-orthogonality >|face pyramid volume <|tet quality <|concavity >|skewness >|interpolation weights|volume ratio of neighbour|face twist <|determinant <|faces in error|^Mesh OK|^Failed" \
  $V/quality_split.log | sed 's/^/     /'

echo
echo "=== 5. merge fts_chan and fts_plen back into one fluid_to_solid ==="
cat > $V/system/createPatchDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object createPatchDict; }
pointSync false;
patches
(
    {
        name fluid_to_solid;
        patchInfo { type mappedWall; inGroups 1(wall); sampleMode nearestPatchFace;
                    sampleRegion solid; samplePatch solid_to_fluid; }
        constructFrom patches; patches (fts_chan fts_plen);
    }
);
DICT
own $V
t0=$(date +%s)
dkv 32 "mpirun --allow-run-as-root --oversubscribe -np $RANKS createPatch -parallel -overwrite > merge.log 2>&1"
rc=$?; stamp merge $rc $t0; own $V
echo "   rc=$rc"
grep -E "Adding new patch|Removing patches|Removed|faces" $V/merge.log | head -8
echo "   FINAL patches, which must be fluid_to_solid + port_a + port_b and nothing else:"
grep -E "^    [a-zA-Z_]+$|^        type|nFaces" $V/processor0/constant/polyMesh/boundary | paste - - - | grep -v processor

echo
echo "=== 6. quality on the merged, deliverable mesh ==="
t0=$(date +%s)
dkv 32 "mpirun --allow-run-as-root --oversubscribe -np $RANKS checkMesh -parallel -constant -meshQuality > quality.log 2>&1"
stamp quality_final $? $t0; own $V
grep -E "cells:|points:|Number of regions|non-orthogonality Max|severely non-orth|Max skewness|Max aspect|Total volume|non-orthogonality >|face pyramid volume <|tet quality <|concavity >|skewness >|interpolation weights|volume ratio of neighbour|face twist <|determinant <|faces in error|^Mesh OK|^Failed" \
  $V/quality.log | sed 's/^/     /'
echo "   interface face count, which must still be 16619777:"
python3 - <<'PY'
import glob, re
tot = 0
for p in sorted(glob.glob("/mnt/work/V4/processor*/constant/polyMesh/boundary")):
    t = open(p).read()
    m = re.search(r"fluid_to_solid\s*\{[^}]*?nFaces\s+(\d+)", t, re.S)
    if m: tot += int(m.group(1))
print("     fluid_to_solid total across processors:", tot, "expected 16619777",
      "MATCH" if tot == 16619777 else "MISMATCH")
PY

echo
echo "=== 7. ship the logs ==="
for f in split.log snappy.log quality_split.log merge.log quality.log; do
  gsutil -q cp $V/$f "$B/mesh/fix1/V4.$f" 2>/dev/null
done
gsutil -q cp $T "$B/mesh/fix1/V4_TIMES.txt" 2>/dev/null
echo
echo "########## V4 AGAINST THE REST ##########"
echo "bl8 shipped : 2936 in error (2348 nonortho, 405 weights), max 76.6676, 102,536,327 cells"
echo "V1          : 3306 in error ( 485 nonortho, 2639 weights), max 74.1616, 102,555,966 cells"
echo "V4 predicted:  842 in error (~254 nonortho,  405 weights)"
cat $T
df -h $W | tail -1
echo "V4 DONE $(date -u)"
