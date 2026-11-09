#!/bin/bash
# FIX RUN 3: V5. The angle fix that keeps the coverage, with a thicker relative stack.
#
# WHERE THE FIRST FOUR LANDED.
#   bl8  2348 angle + 405 weight = 2936   coverage 95.553%  max 76.6676
#   V1    485 angle + 2639 weight = 3306  coverage 95.418%  max 74.1616   relativeSizes 0.165
#   V2   2229 angle +  298 weight = 2724  coverage 95.725%  max 76.0512   no smoothing
#   V3    133 angle + 2516 weight = 2841  coverage 95.474%  max 75.6483   both
#   V4      7 angle + 9635 weight = 9647  coverage 91.900%  max 67.2138   patch split
#
# TWO THINGS ARE SETTLED. The angle is solvable: V4 got the maximum under 70 degrees, so ZERO
# severely non-orthogonal faces, and V3 got 10. And every route to it costs interpolation weight,
# either by thinning the stack (V1, V3) or by terminating it (V4).
#
# V4 IS REJECTED ON PHYSICS, not on its face count. Its coverage fell to 91.90% and its stack
# filled only 77.80% of the requested cells, and that shortfall is on the CHANNEL wall, which is
# the only place the boundary layer exists for. Asking for layers on 96% of a surface and not the
# interleaved other 4% fights the mesher, and the mesher backed off over a wide area.
#
# V5 IS V3 WITH A THICKER RELATIVE STACK. V3's mechanism is the one that works and keeps
# coverage: thickness tracking cell width is what collapses the angle, because the angle is
# atan(drift/thickness) and the drift scales with cell WIDTH. Its only cost is 2,516 weight
# failures from a stack that came out thinner than shipped, 1.13e-05 against 1.43e-05. So keep
# the mechanism and raise the coefficient from 0.165 to 0.25, which puts the level-4 stack back
# at or above the shipped thickness while still scaling with the cell.
#
# PREDICTION, recorded before the run: the angle stays near V3's 133 over 65 degrees, the weight
# failures fall from 2,516 toward bl8's 405, coverage stays near 95.5%, and the total comes in
# under bl8's 2,936 for the first time with the angle also fixed.
#
# AND THIS TIME THE EVIDENCE SURVIVES. Both previous boxes self-deleted with their meshes on
# them, so neither V3's nor V4's weight failures could ever be located. This driver runs the
# localiser itself and uploads the positions before the box is allowed to die.
set -u
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
B=gs://dev-rehaan-artifacts-224734058693/corintis
W=/mnt/work
FOAM="source /usr/lib/openfoam/openfoam2412/etc/bashrc"
T=$W/V5_TIMES.txt
V=$W/V5
stamp(){ echo "$1 $2 $(( $(date +%s) - $3 ))" >> $T; }
own(){ chown -R 1000:1000 "$1" 2>/dev/null; chmod -R a+rwX "$1" 2>/dev/null; }
dkv(){ docker run --rm --cpus="$1" -v "$V":/case -w /case "$IMG" bash -lc "$FOAM; $2"; }

rm -rf $W/raw $V
mkdir -p $W/dl $V
: > $T
echo "################ FIX RUN 3, V5  $(date -u) ################"
echo "cores $(nproc)  mem $(free -g | awk '/^Mem/{print $2}')GB"

echo
echo "=== 0. the coolant ==="
t0=$(date +%s)
gsutil -q cp "$B/mesh/cbox8/cbox8_fluid.tar.gz" $W/dl/ || { echo FATAL; exit 1; }
gsutil -q cp "$B/drivers/parbad2.cpp" $W/parbad2.cpp || { echo FATAL; exit 1; }
stamp download 0 $t0
t0=$(date +%s)
mkdir -p $W/raw && cd $W/raw && tar xzf $W/dl/cbox8_fluid.tar.gz
stamp extract 0 $t0
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
application chtMultiRegionFoam;
startFrom startTime; startTime 0; stopAt endTime; endTime 1; deltaT 1;
writeControl timeStep; writeInterval 1;
writeFormat ascii; writeCompression off;
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
grep -m1 note $V/processor0/constant/polyMesh/owner | head -c 190; echo

echo
echo "=== 1. layers: relativeSizes 0.25, no normal smoothing ==="
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
    relativeSizes true;
    layers { fluid_to_solid { nSurfaceLayers 3; } }
    expansionRatio          1.2;
    firstLayerThickness     0.25;
    minThickness            3.0e-06;
    nGrow 0;
    featureAngle              180;
    maxFaceThicknessRatio     0.5;
    nSmoothSurfaceNormals       0;
    nSmoothThickness           10;
    minMedialAxisAngle         90;
    maxThicknessToMedialRatio 0.6;
    nSmoothNormals              0;
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
ITERS=$(grep -cE "Extruding .* out of" $V/snappy.log)
echo "   outer iterations: $ITERS"
if [ "$ITERS" -lt 2 ]; then echo "FATAL: no layers were added"; exit 1; fi
grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $V/snappy.log | head -1 | sed 's/^/     /'
grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $V/snappy.log | tail -1 | sed 's/^/     /'
grep -oE "Added [0-9]+ out of [0-9]+ cells \([0-9.]+%\)" $V/snappy.log | tail -1 | sed 's/^/     /'
echo "   requested thickness:"
sed -n "/patch *faces *layers *avg thickness/,+4p" $V/snappy.log | sed 's/^/     /'
echo "   achieved thickness:"
sed -n "/patch *faces *layers *overall thickness/,+5p" $V/snappy.log | tail -4 | sed 's/^/     /'
grep -E "Layer mesh :|Finished meshing" $V/snappy.log | tail -2 | sed 's/^/     /'

echo
echo "=== 2. quality ==="
t0=$(date +%s)
dkv 32 "mpirun --allow-run-as-root --oversubscribe -np $RANKS checkMesh -parallel -constant -meshQuality > quality.log 2>&1"
stamp quality $? $t0; own $V
grep -E "cells:|points:|Number of regions|non-orthogonality Max|severely non-orth|Max skewness|Max aspect|Total volume|non-orthogonality >|face pyramid volume <|tet quality <|concavity >|skewness >|interpolation weights|volume ratio of neighbour|face twist <|determinant <|faces in error|^Mesh OK|^Failed" \
  $V/quality.log | sed 's/^/     /'

echo
echo "=== 3. LOCALISE, before this box is allowed to die ==="
CELLS=$(grep -oE "^    cells: *[0-9]+" $V/quality.log | head -1 | grep -oE "[0-9]+")
E65=$(grep -oE "non-orthogonality > *65 *degrees *: *[0-9]+" $V/quality.log | head -1 | grep -oE "[0-9]+$")
EW=$(grep -oE "interpolation weights \(0\.\.1\) *< *0\.02 *: *[0-9]+" $V/quality.log | head -1 | grep -oE "[0-9]+$")
echo "   gating on cells=$CELLS  >65=$E65  weight=$EW"
t0=$(date +%s)
docker run --rm -v "$W":/w -w /w "$IMG" bash -lc "g++ -O2 -D_FILE_OFFSET_BITS=64 -o /w/parbad2 /w/parbad2.cpp -lm"
chmod 755 $W/parbad2
$W/parbad2 $V - $W/V5_bad.csv "${CELLS:--1}" -1 "${E65:--1}" "${EW:--1}" 2>&1 | tail -16
stamp localise $? $t0
echo "   rows by kind:"
tail -n +2 $W/V5_bad.csv | cut -d, -f3 | sort | uniq -c | sed 's/^/     /'
echo "   and the same for the ports question: how many bad faces sit within 2 mm of a port"
python3 - <<'PY'
import io
# port_a and port_b are the former fluid_to_air faces, 1,331 + 1,308 = 2,639. If the weight
# failures are a ring around them, they will cluster in two small places.
rows = []
with io.open("/mnt/work/V5_bad.csv") as fh:
    fh.readline()
    for ln in fh:
        p = ln.rstrip().split(",")
        if len(p) < 8: continue
        rows.append((p[2], float(p[5]), float(p[6]), float(p[7])))
w = [r for r in rows if r[0] in ("weight", "both")]
print("     weight-type rows: %d" % len(w))
if w:
    import math
    from collections import Counter
    b = Counter((int(math.floor(r[1]*500)), int(math.floor(r[2]*500)), int(math.floor(r[3]*500))) for r in w)
    print("     distinct 2 mm bins: %d" % len(b))
    tot = 0
    for k, c in b.most_common(8):
        tot += c
        print("       (%6.1f %6.1f %6.1f) mm  %6d  %5.2f%%"
              % (k[0]*2, k[1]*2, k[2]*2, c, 100.0*c/len(w)))
    print("     top 8 bins hold %d of %d = %.1f%%" % (tot, len(w), 100.0*tot/len(w)))
PY

echo
echo "=== 4. ship ==="
gsutil -q cp $V/snappy.log  "$B/mesh/fix1/V5.snappy.log"
gsutil -q cp $V/quality.log "$B/mesh/fix1/V5.quality.log"
gsutil -q cp $W/V5_bad.csv  "$B/mesh/fix1/V5_bad.csv"
gsutil -q cp $T             "$B/mesh/fix1/V5_TIMES.txt"
echo
echo "########## V5 AGAINST THE REST ##########"
echo "bl8  2348 angle +  405 weight = 2936   cov 95.553%  max 76.6676"
echo "V1    485 angle + 2639 weight = 3306   cov 95.418%  max 74.1616"
echo "V2   2229 angle +  298 weight = 2724   cov 95.725%  max 76.0512"
echo "V3    133 angle + 2516 weight = 2841   cov 95.474%  max 75.6483"
echo "V4      7 angle + 9635 weight = 9647   cov 91.900%  max 67.2138"
cat $T
df -h $W | tail -1
echo "V5 DONE $(date -u)"
