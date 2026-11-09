#!/bin/bash
# FIX RUN 1 on the pitch-8 deliverable. Four things, none of them a remesh.
#
# 1. THE AIR PATCHES POINT AT A REGION WE DO NOT SHIP. chip_to_air (1,492,860 faces) and
#    solid_to_air (1,213,523) are typed mappedWall with sampleRegion air, and the delivery is
#    fluid + solid + chip only. 2,706,383 faces of the delivered mesh reference a region that
#    is not there, so the case cannot be constructed as supplied. Retype them to wall.
#
# 2. FIVE OF THE TEN QUALITY CRITERIA WERE NEVER EVALUATED on the three unlayered regions,
#    because those checkMesh runs carried no -meshQuality. Run it. Before and after the retype
#    on the chip, so the retype is shown to be what moves the return code.
#
# 3. THE LAYERED COOLANT FAILS ONE MESH CHECK, 2,936 faces. Measured 2026-11-08: 89.14% of the
#    bad angles sit on the coarse plenum wall within 1.2 mm of the z faces of the anisotropic
#    stretch box, the shared face carries the full base area of a layer cell, and the angle is
#    atan(drift/thickness) with drift about 4.5% of the CELL WIDTH. So:
#      V1  relativeSizes true  -> thickness tracks cell width, so the angle should collapse to
#          about atan(0.045/0.165) = 15 deg everywhere. The channel layers stay 4.15 um.
#      V2  no normal smoothing -> if the drift is smoothing, the drift itself goes away.
#      V3  both.
#    Three runs, each measured with -meshQuality, and the winner is whichever reports fewest
#    faces in error without losing coverage. V0 is not run: bl8 at 2,936 is the control.
#
# 4. 52.9% OF THE WALL CLOCK WAS SERIAL tar and reconstruct. pigz across the cores, and no
#    reconstruct at all, since the boundary index and quality sidecar are built off the
#    decomposed mesh.
#
# The chip is deliberately NOT remeshed. chip.stl is genuinely two bodies and the mesh is right
# about that, but chip_to_solid is 100% at y = +0.02011, which is body 1's top face alone, so
# body 2 (12,987 mm3, 78% of the chip volume) is bonded to nothing and the 70 um gap between
# them is air in our mesh. Whether that gap is a bond line is the customer's answer, not ours,
# and refining it before they answer would be guessing.
set -u
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
B=gs://dev-rehaan-artifacts-224734058693/corintis
W=/mnt/work
RANKS=0   # set from the archive's own decomposition in stage 1, never guessed
FOAM="source /usr/lib/openfoam/openfoam2412/etc/bashrc"
T=$W/FIX_TIMES.txt
own(){ chown -R 1000:1000 "$1" 2>/dev/null; chmod -R a+rwX "$1" 2>/dev/null; }
stamp(){ echo "$1 $2 $(( $(date +%s) - $3 ))" >> $T; }
dk(){ docker run --rm --cpus="$1" -v "$W/case":/case -w /case "$IMG" bash -lc "$FOAM; $2"; }

mkdir -p $W/case/system $W/dl
: > $T
echo "################ FIX RUN 1  $(date -u) ################"
echo "cores $(nproc)  mem $(free -g | awk '/^Mem/{print $2}')GB  disk $(df -h $W | tail -1 | awk '{print $4}')"

############################################################ 0. restore
echo
echo "=== 0. restore the three decomposed regions ==="
t0=$(date +%s)
for r in fluid solid chip; do
  gsutil -q cp "$B/mesh/cbox8/cbox8_${r}.tar.gz" $W/dl/ || { echo "FATAL: no ${r} archive"; exit 1; }
done
gsutil -q cp "$B/mesh/cbox8/cbox8_meta.tar.gz" $W/dl/
stamp download 0 $t0
t0=$(date +%s)
cd $W/case
for r in fluid solid chip; do
  tar xzf $W/dl/cbox8_${r}.tar.gz
  echo "   ${r} extracted"
done
stamp extract 0 $t0
own $W/case
echo "   processor dirs: $(ls -d $W/case/processor* 2>/dev/null | wc -l)"
echo "   regions in processor0: $(ls $W/case/processor0/constant 2>/dev/null | tr '\n' ' ')"
du -sh $W/case

############################################################ 1. dicts
echo
echo "=== 1. system dicts ==="
cat > $W/case/system/controlDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application     chtMultiRegionFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
writeFormat     ascii;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;
DICT
cat > $W/case/system/meshQualityDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object meshQualityDict; }
maxNonOrtho          65;
maxBoundarySkewness  20;
maxInternalSkewness   4;
maxConcave           80;
minVol             1e-20;
minTetQuality      1e-30;
minArea               -1;
minTwist            0.02;
minDeterminant     0.001;
minFaceWeight       0.02;
minVolRatio         0.01;
minTriangleTwist      -1;
nSmoothScale           4;
errorReduction      0.75;
DICT
cat > $W/case/system/fvSchemes <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
DICT
cat > $W/case/system/fvSolution <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
solvers { ".*" { solver PCG; preconditioner DIC; tolerance 1e-6; relTol 0; } }
DICT
cat > $W/case/system/decomposeParDict <<DICT
FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains $(ls -d $W/case/processor* | wc -l);
method scotch;
DICT
for r in fluid solid chip; do
  mkdir -p $W/case/system/$r
  cp $W/case/system/{controlDict,meshQualityDict,fvSchemes,fvSolution,decomposeParDict} $W/case/system/$r/
done
own $W/case
NP=$(ls -d $W/case/processor* | wc -l)
RANKS=$NP          # the mesh is already decomposed; re-decomposing 300M cells would cost more
                   # than it saves, and -np must equal the processor count or FOAM aborts
echo "   nProcs $NP, so RANKS=$RANKS"
if [ "$NP" -lt 2 ]; then echo "FATAL: no processor dirs"; exit 1; fi
echo "   patches now:"
for r in fluid solid chip; do
  echo "   --- $r"
  grep -E "^    [a-zA-Z_]+$|^        type|sampleRegion" $W/case/processor0/constant/$r/polyMesh/boundary \
    | paste - - - 2>/dev/null | grep -v processor | head -6
done

############################################################ 2. chip check BEFORE the retype
echo
echo "=== 2. chip -meshQuality BEFORE the retype, to show what the retype does ==="
t0=$(date +%s)
dk 32 "mpirun --allow-run-as-root --oversubscribe -np $NP checkMesh -parallel -constant -region chip -meshQuality > chip_before.log 2>&1"
rc=$?; stamp chip_before $rc $t0; own $W/case
echo "   rc=$rc"
grep -E "^Mesh OK|^Failed|FOAM FATAL|Cannot find|not found|unknown region" $W/case/chip_before.log | head -5

############################################################ 3. retype the dangling air patches
echo
echo "=== 3. retype *_to_air from mappedWall to wall (they point at a region we do not ship) ==="
t0=$(date +%s)
cat > $W/retype.py <<'PY'
import glob, io, re, sys
n_files = n_patch = n_faces = 0
for path in sorted(glob.glob("/mnt/work/case/processor*/constant/*/polyMesh/boundary")):
    t = io.open(path, encoding="utf-8", errors="replace").read()
    # only the air-facing patches; fluid_to_air is left alone because it becomes the ports
    out, changed = [], False
    lines = t.split("\n")
    i = 0
    cur = None
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*)\s*$", ln)
        if m:
            cur = m.group(1)
        if cur and cur.endswith("_to_air") and not cur.startswith("fluid"):
            if re.match(r"^\s*type\s+mappedWall\s*;\s*$", ln):
                out.append(re.sub(r"mappedWall", "wall", ln)); changed = True; n_patch += 1
                i += 1; continue
            if re.match(r"^\s*(sampleMode|sampleRegion|samplePatch|offsetMode|offset)\b", ln):
                i += 1; continue
            m2 = re.match(r"^\s*nFaces\s+(\d+)\s*;", ln)
            if m2:
                n_faces += int(m2.group(1))
        out.append(ln); i += 1
    if changed:
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
        n_files += 1
print("retyped %d patches across %d boundary files, %d faces" % (n_patch, n_files, n_faces))
PY
python3 /mnt/work/retype.py
stamp retype 0 $t0
own $W/case
echo "   chip patches after:"
grep -E "^    [a-zA-Z_]+$|^        type" $W/case/processor0/constant/chip/polyMesh/boundary | paste - - | grep -v processor
echo "   solid patches after:"
grep -E "^    [a-zA-Z_]+$|^        type" $W/case/processor0/constant/solid/polyMesh/boundary | paste - - | grep -v processor
echo "   any mappedWall left pointing at air:"
grep -l "sampleRegion    air" $W/case/processor*/constant/*/polyMesh/boundary 2>/dev/null | wc -l

############################################################ 4. the quality pass that was never run
echo
echo "=== 4. checkMesh -parallel -meshQuality on all three unlayered regions ==="
for r in fluid solid chip; do
  t0=$(date +%s)
  dk 32 "mpirun --allow-run-as-root --oversubscribe -np $NP checkMesh -parallel -constant -region $r -meshQuality > q_$r.log 2>&1"
  rc=$?; stamp quality_$r $rc $t0; own $W/case
  echo "--- $r  rc=$rc"
  grep -E "cells:|Number of regions|non-orthogonality Max|Max skewness|Max aspect|Total volume" $W/case/q_$r.log | head -6
  sed -n "/Checking faces in error/,+11p" $W/case/q_$r.log | head -12
  grep -E "^Mesh OK|^Failed" $W/case/q_$r.log | head -2
done

############################################################ 5. port split on the coolant if needed
echo
echo "=== 5. ports ==="
if grep -q "port_a" $W/case/processor0/constant/fluid/polyMesh/boundary; then
  echo "   already split, nothing to do"
else
  echo "   fluid_to_air present, splitting into port_a and port_b"
  cat > $W/case/system/fluid/topoSetDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object topoSetDict; }
actions
(
    { name portA; type faceSet; action new;    source patchToFace; patch fluid_to_air; }
    { name portA; type faceSet; action subset; source boxToFace; box (-1 -1 0.060) (1 1 1); }
    { name portB; type faceSet; action new;    source patchToFace; patch fluid_to_air; }
    { name portB; type faceSet; action subset; source boxToFace; box (-1 -1 -1) (1 1 0.060); }
);
DICT
  cat > $W/case/system/fluid/createPatchDict <<'DICT'
FoamFile { version 2.0; format ascii; class dictionary; object createPatchDict; }
pointSync false;
patches
(
    { name port_a; patchInfo { type patch; } constructFrom set; set portA; }
    { name port_b; patchInfo { type patch; } constructFrom set; set portB; }
);
DICT
  own $W/case
  t0=$(date +%s)
  dk 32 "mpirun --allow-run-as-root --oversubscribe -np $NP topoSet -parallel -constant -region fluid > p8_topoSet.log 2>&1"
  stamp port_topoSet $? $t0; own $W/case
  t0=$(date +%s)
  dk 32 "mpirun --allow-run-as-root --oversubscribe -np $NP createPatch -parallel -constant -region fluid -overwrite > p8_createPatch.log 2>&1"
  stamp port_createPatch $? $t0; own $W/case
  grep -E "Read [0-9]+ faces|Removed|Adding new patch" $W/case/p8_createPatch.log | head -8
fi
grep -E "^    [a-zA-Z_]+$|^        type|nFaces" $W/case/processor0/constant/fluid/polyMesh/boundary | paste - - - | grep -v processor

############################################################ 6. the layer variants
echo
echo "########## 6. LAYER VARIANTS ##########"
mkdir -p $W/fluidbase
cp -r $W/case/system $W/fluidbase/
for p in $W/case/processor*; do
  pn=$(basename $p)
  mkdir -p $W/fluidbase/$pn/constant
  cp -r $p/constant/fluid $W/fluidbase/$pn/constant/
done
# a fluid-only case has the region at the top level, not under constant/fluid
for p in $W/fluidbase/processor*; do
  mv $p/constant/fluid/polyMesh $p/constant/polyMesh
  rm -rf $p/constant/fluid
done
cp $W/case/system/fluid/controlDict $W/fluidbase/system/controlDict
cp $W/case/system/fluid/meshQualityDict $W/fluidbase/system/meshQualityDict
own $W/fluidbase
echo "   fluid-only base built, $(du -sh $W/fluidbase | cut -f1)"
grep -m1 note $W/fluidbase/processor0/constant/polyMesh/owner 2>/dev/null | head -c 200; echo

snappydict () {  # $1=dir  $2=relativeSizes  $3=firstLayerThickness  $4=nSmoothSurfaceNormals  $5=nSmoothNormals
cat > $1/system/snappyHexMeshDict <<DICT
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
    relativeSizes $2;
    layers { fluid_to_solid { nSurfaceLayers 3; } }
    expansionRatio          1.2;
    firstLayerThickness     $3;
    minThickness            3.0e-06;
    nGrow 0;
    featureAngle              180;
    maxFaceThicknessRatio     0.5;
    nSmoothSurfaceNormals     $4;
    nSmoothThickness           10;
    minMedialAxisAngle         90;
    maxThicknessToMedialRatio 0.6;
    nSmoothNormals            $5;
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
}

runvariant () {   # $1=name  $2=relativeSizes  $3=firstLayerThickness  $4=nSSN  $5=nSN  $6=note
  local V=$W/$1
  echo
  echo "===== VARIANT $1 : $6"
  echo "      relativeSizes $2  firstLayerThickness $3  nSmoothSurfaceNormals $4  nSmoothNormals $5"
  rm -rf $V; mkdir -p $V
  cp -r $W/fluidbase/system $V/
  for p in $W/fluidbase/processor*; do cp -r $p $V/; done
  rm -rf $V/processor*/constant/polyMesh/sets
  snappydict $V $2 $3 $4 $5
  chown -R 1000:1000 $V 2>/dev/null; chmod -R a+rwX $V 2>/dev/null
  local t0=$(date +%s)
  docker run --rm --cpus=56 -v "$V":/case -w /case "$IMG" bash -lc \
    "$FOAM; mpirun --allow-run-as-root --oversubscribe -np $RANKS snappyHexMesh -parallel -overwrite > snappy.log 2>&1"
  local rc=$?; stamp "layers_$1" $rc $t0
  chown -R 1000:1000 $V 2>/dev/null; chmod -R a+rwX $V 2>/dev/null
  echo "      layers rc=$rc in $(( $(date +%s) - t0 ))s"
  echo "      coverage first/last:"
  grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $V/snappy.log 2>/dev/null | head -1 | sed 's/^/        /'
  grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $V/snappy.log 2>/dev/null | tail -1 | sed 's/^/        /'
  grep -oE "Added [0-9]+ out of [0-9]+ cells \([0-9.]+%\)" $V/snappy.log 2>/dev/null | tail -1 | sed 's/^/        /'
  echo "      layer table:"
  sed -n "/patch *faces *layers *overall thickness/,+5p" $V/snappy.log 2>/dev/null | tail -4 | sed 's/^/        /'
  grep -E "Layer mesh :|Finished meshing" $V/snappy.log 2>/dev/null | tail -2 | sed 's/^/        /'
  grep -E "FOAM FATAL" $V/snappy.log 2>/dev/null | head -3 | sed 's/^/        /'
  t0=$(date +%s)
  docker run --rm --cpus=32 -v "$V":/case -w /case "$IMG" bash -lc \
    "$FOAM; mpirun --allow-run-as-root --oversubscribe -np $RANKS checkMesh -parallel -constant -meshQuality > quality.log 2>&1"
  rc=$?; stamp "quality_$1" $rc $t0
  chown -R 1000:1000 $V 2>/dev/null; chmod -R a+rwX $V 2>/dev/null
  echo "      --- QUALITY"
  grep -E "cells:|Number of regions|non-orthogonality Max|severely non-orth|Max skewness|Max aspect|Total volume" $V/quality.log 2>/dev/null | head -8 | sed 's/^/        /'
  sed -n "/Checking faces in error/,+11p" $V/quality.log 2>/dev/null | head -12 | sed 's/^/        /'
  grep -E "^Mesh OK|^Failed" $V/quality.log 2>/dev/null | head -2 | sed 's/^/        /'
  gsutil -q cp $V/snappy.log  "$B/mesh/fix1/$1.snappy.log"  2>/dev/null
  gsutil -q cp $V/quality.log "$B/mesh/fix1/$1.quality.log" 2>/dev/null
  df -h $W | tail -1 | sed 's/^/        disk /'
}

# 4.15e-06 absolute is 0.165 of the 25.1578 um level-4 cell
runvariant V1 true  0.165    1 3 "thickness tracks local cell size"
runvariant V2 false 4.15e-06 0 0 "no normal smoothing, thickness as shipped"
runvariant V3 true  0.165    0 0 "both"

############################################################ 7. summary
echo
echo "########## 7. SUMMARY ##########"
printf "%-10s %14s %14s %14s %12s\n" variant faces_in_error max_nonortho coverage verdict
for v in V1 V2 V3; do
  q=$W/$v/quality.log; s=$W/$v/snappy.log
  fe=$(grep -oE "Writing [0-9]+ faces in error" $q 2>/dev/null | tail -1 | grep -oE "[0-9]+")
  [ -z "${fe:-}" ] && fe=$(grep -c "faces in error" $q 2>/dev/null)
  mo=$(grep -oE "non-orthogonality Max: [0-9.]+" $q 2>/dev/null | tail -1 | grep -oE "[0-9.]+$")
  cv=$(grep -oE "Extruding [0-9]+ out of [0-9]+ faces \([0-9.]+%\)" $s 2>/dev/null | tail -1 | grep -oE "\([0-9.]+%\)")
  vd=$(grep -E "^Mesh OK|^Failed" $q 2>/dev/null | head -1)
  printf "%-10s %14s %14s %14s %12s\n" "$v" "${fe:-?}" "${mo:-?}" "${cv:-?}" "${vd:-?}"
done
echo
echo "bl8 as shipped, for comparison: 2936 faces in error, max non-ortho 76.6676, coverage (95.553%)"
############################################################ 8. re-ship the corrected regions
echo
echo "########## 8. RE-TAR THE CORRECTED REGIONS WITH pigz ##########"
echo "the retype in stage 3 is independent of which layer variant wins, so it ships now"
for r in fluid solid chip; do
  t0=$(date +%s)
  mkdir -p $W/ship/$r
  for p in $W/case/processor*; do
    pn=$(basename $p)
    mkdir -p $W/ship/$r/$pn/constant
    cp -r $p/constant/$r $W/ship/$r/$pn/constant/
  done
  sz=$( (cd $W/ship/$r && tar -I "pigz -p $(nproc)" -cf $W/cbox8_${r}_fixed.tar.gz processor*) && stat -c %s $W/cbox8_${r}_fixed.tar.gz )
  stamp "pigz_$r" 0 $t0
  echo "   $r  $sz B in $(( $(date +%s) - t0 ))s"
  t0=$(date +%s)
  gsutil -q -o GSUtil:parallel_composite_upload_threshold=150M cp $W/cbox8_${r}_fixed.tar.gz "$B/mesh/cbox8fixed/cbox8_${r}.tar.gz"
  stamp "upload_$r" $? $t0
  rm -rf $W/ship/$r $W/cbox8_${r}_fixed.tar.gz
done
echo "the original cbox8_* objects are untouched; the corrected set is under mesh/cbox8fixed/"
gsutil ls -l "$B/mesh/cbox8fixed/" 2>/dev/null

echo
cat $T
gsutil -q cp $T "$B/mesh/fix1/FIX_TIMES.txt" 2>/dev/null
for r in fluid solid chip; do gsutil -q cp $W/case/q_$r.log "$B/mesh/fix1/q_$r.log" 2>/dev/null; done
gsutil -q cp $W/case/chip_before.log "$B/mesh/fix1/chip_before.log" 2>/dev/null
echo "FIX1 DONE $(date -u)"
