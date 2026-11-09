#!/bin/bash
# COUPLED PLATE + CHIP, BOX DOMAIN, FOUR ZONES.
#   usage: couple_box.sh <label> <background_cell_m> <coolant_lvl> <fine_lvl> <chip_lvl> <skin_lvl> <ranks> <threads>
#   usage: couple_box.sh <label> <bg> <lcool> <lfine> <lchip> <lskin> <ranks> <threads> [lfine2] [lchip2]
#   cbox2, measured:        0.000352056 1 4 2 1 8 12         413,273,323 cells
#   cbox5, the extra detail:   ...same... 8 12 5 0           623M predicted, and it resolves
#                                                            the 41 um thin gap
#   DO NOT raise the chip entry to +3 at its 3.6 mm thickness: measured 941M, over the 700M
#   ceiling, because the 88 um air shell around the chip is 2.434e-05 m3 and x8 is 250M cells.
#   coarse proof of the set algebra: couple_box.sh cboxp 0.0014 0 1 1 0 8 4
#
# WHY A BOX DOMAIN. Five measured refutations, in order:
#   concatenated disjoint shells       chipCells 0, mesh bbox was the plate only
#   concatenated, 0.7 mm overlap       chipCells 0, and the mesh y_min ROSE to 20.77 mm
#   concatenated, 2.1 mm overlap       chipCells 0, and y_min rose to 22.21 mm
#   OCC fuse, plate's 6 channelled solids             died with no traceback
#   OCC fuse, sewn 27,696-face envelope + chip        died with no traceback
# cfMesh takes exactly ONE shell as its domain, and a stray second shell corrupts the octree
# rather than being ignored. So the domain is a padded box and topoSet does the rest.
#
# THE ONE COST, said plainly: the plate's outer skin becomes a staircase instead of a conforming
# surface, mitigated by refining block.stl one level. The coolant and chip interfaces are
# unaffected - they were always staircase cuts, which IS the conjugate method.
#
# THE FOUR ZONES. includeCut cannot be used at all - it is mutually exclusive with
# useSurfaceOrientation, which is the only parallel-safe form of surfaceToCell - so cut cells can
# never be claimed by a surface and the AIR has to be defined POSITIVELY as "strictly outside both
# bodies" instead of as the leftover:
#   outChip    strictly outside the chip
#   airCells   strictly outside the envelope, SUBSET to outChip
#   chipCells  strictly inside the chip
#   fluidCells strictly inside the coolant, minus air, minus chip
#   plateCells everything else, so the PLATE absorbs every cut cell
# The plate absorbing the cut layer is what stops a one-cell AIR FILM forming across the
# chip-to-plate thermal path. The chip is also lifted 24.267 um so it bites into the envelope.
# Measured on cbox2: the four zones sum to 413,273,323 and splitMeshRegions did not complain,
# which is the proof that every cell landed in exactly one zone.
#
# air is split off as a region and simply not reconstructed or delivered.

set -u
LAB=${1:-cbox3}
BG=${2:-0.000352056}
LCOOL=${3:-1}
LFINE=${4:-4}
LCHIP=${5:-3}
LSKIN=${6:-1}
RANKS=${7:-8}
THREADS=${8:-12}
LFINE2=${9:-0}     # extra microchannel level on a 2-cell band; 0 = off
LCHIP2=${10:-0}    # extra chip-surface level on a 20-cell band; 0 = off
DOM=${11:-cb_domain_box.stl}   # the domain box; a tighter one buys a smaller octree root
W=/mnt/work
c=$W/$LAB
B=gs://dev-rehaan-artifacts-224734058693
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
MPIBUF=1000000000
CAP=14400
LOG=/var/log/$LAB.log
PAR_RANKS=8            # what a serial mesh is decomposed into for the rest of the chain
CPUS=$(nproc)           # docker refuses --cpus above the host core count: 96 killed this on a 32-core box

exec >>"$LOG" 2>&1
echo "================ $LAB starting $(date -u)"
echo "background $BG  coolant +$LCOOL  fine +$LFINE  chip +$LCHIP  skin +$LSKIN  ranks $RANKS x $THREADS"
echo "extra: fine2 +$LFINE2  chip2 +$LCHIP2   domain $DOM"
free -g | head -2; df -h /mnt/work | tail -1

for f in block fluid fluid_fine chip_fit; do
  [ -s "$W/cb_$f.stl" ] || { echo "FATAL: $W/cb_$f.stl missing"; exit 1; }
done
[ -s "$W/$DOM" ] || { echo "FATAL: domain $W/$DOM missing"; exit 1; }
ls -la "$W"/cb_*.stl

rm -rf "$c"; mkdir -p "$c/system" "$c/constant/triSurface"
cp "$W/$DOM"               "$c/constant/triSurface/domain.stl"
cp "$W/cb_block.stl"       "$c/constant/triSurface/block.stl"
cp "$W/cb_fluid.stl"       "$c/constant/triSurface/fluid.stl"
cp "$W/cb_fluid_fine.stl"  "$c/constant/triSurface/fluid_fine.stl"
cp "$W/cb_chip_fit.stl"    "$c/constant/triSurface/chip.stl"

cat > "$c/system/controlDict" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application     chtMultiRegionFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
writeFormat     binary;
writeCompression off;
EOF
cat > "$c/system/decomposeParDict" <<EOF
FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains $RANKS;
method scotch;
EOF
EXTRA=""
if [ "$LFINE2" -gt 0 ]; then
  # A 2-cell band at the finer size, ON TOP of the proven 4-cell band at $LFINE - nothing is
  # coarsened. The 41 um thin gap gets 1.86 cells across at 22 um and 3.7 at 11 um; both walls'
  # bands overlap inside a gap that thin, so the whole gap fills with the fine cell.
  T2=$(awk -v b="$BG" -v l="$LFINE2" 'BEGIN{printf "%.6e", 2*b/(2^l)}')
  EXTRA="$EXTRA
    microchannels_fine
    {
        surfaceFile \"constant/triSurface/fluid_fine.stl\";
        additionalRefinementLevels $LFINE2;
        refinementThickness $T2;
    }"
fi
if [ "$LCHIP2" -gt 0 ]; then
  # A 20-cell band on the chip's surfaces, which puts the finer cell at the chip-to-plate contact
  # and on the chip's air-facing walls without refining the whole silicon block.
  T3=$(awk -v b="$BG" -v l="$LCHIP2" 'BEGIN{printf "%.6e", 20*b/(2^l)}')
  EXTRA="$EXTRA
    chip_contact
    {
        surfaceFile \"constant/triSurface/chip.stl\";
        additionalRefinementLevels $LCHIP2;
        refinementThickness $T3;
    }"
fi
cat > "$c/system/meshDict" <<EOF
FoamFile { version 2.0; format ascii; class dictionary; object meshDict; }
surfaceFile "constant/triSurface/domain.stl";
maxCellSize $BG;
surfaceMeshRefinement
{
    plate_skin
    {
        surfaceFile "constant/triSurface/block.stl";
        additionalRefinementLevels $LSKIN;
        refinementThickness $BG;
    }
    coolant
    {
        surfaceFile "constant/triSurface/fluid.stl";
        additionalRefinementLevels $LCOOL;
        refinementThickness $BG;
    }
    microchannels
    {
        surfaceFile "constant/triSurface/fluid_fine.stl";
        additionalRefinementLevels $LFINE;
        refinementThickness 8.8014e-05;
    }
    chip
    {
        surfaceFile "constant/triSurface/chip.stl";
        additionalRefinementLevels $LCHIP;
        refinementThickness 0.0036;
    }
$EXTRA
}
anisotropicSources
{
    microchannels
    {
        type box;
        centre (0.02267671418 0.02752817822 0.0424025383);
        lengthX 0.05985824678;
        lengthY 0.01218805644;
        lengthZ 0.05133793399;
        scaleX 1;
        scaleY 3;
        scaleZ 1;
    }
}
EOF
cat > "$c/system/topoSetDict" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object topoSetDict; }
// EVERY source here uses useSurfaceOrientation, because it is the ONLY parallel-safe form.
// Measured the hard way, in order:
//   includeCut true + useSurfaceOrientation -> "You cannot specify both"
//   includeCut true with a seed point       -> "outsidePoint ... is not inside any cell" on 7 of
//                                              8 ranks, because each rank owns only part of the
//                                              mesh so the seed sits in a cell on exactly one.
// So cut cells can never be claimed by a surface, and the AIR has to be defined POSITIVELY as
// "strictly outside both bodies" rather than as the leftover. The plate then absorbs every cut
// cell, including the layer at the chip-to-plate contact - which is what stops a one-cell AIR FILM
// forming across the thermal path. The chip interface therefore sits one cell inside the chip's
// true surface, which is the same staircase the coolant interface has always had.
actions
(
    // 1. strictly outside the chip
    {
        name outChip; type cellSet; action new; source surfaceToCell;
        file "constant/triSurface/chip.stl";
        useSurfaceOrientation true;
        outsidePoints ((-1e6 -1e6 -1e6));
        includeCut false; includeInside false; includeOutside true;
        nearDistance -1;  curvature -100;
    }
    // 2. air = strictly outside the plate envelope AND strictly outside the chip
    {
        name airCells; type cellSet; action new; source surfaceToCell;
        file "constant/triSurface/block.stl";
        useSurfaceOrientation true;
        outsidePoints ((-1e6 -1e6 -1e6));
        includeCut false; includeInside false; includeOutside true;
        nearDistance -1;  curvature -100;
    }
    { name airCells; type cellSet; action subset; source cellToCell; set outChip; }
    { name air; type cellZoneSet; action new; source setToCellZone; set airCells; }

    // 3. the chip
    {
        name chipCells; type cellSet; action new; source surfaceToCell;
        file "constant/triSurface/chip.stl";
        useSurfaceOrientation true;
        outsidePoints ((-1e6 -1e6 -1e6));
        includeCut false; includeInside true; includeOutside false;
        nearDistance -1;  curvature -100;
    }
    { name chip; type cellZoneSet; action new; source setToCellZone; set chipCells; }

    // 4. the coolant, held out of the air and out of the chip
    {
        name fluidCells; type cellSet; action new; source surfaceToCell;
        file "constant/triSurface/fluid.stl";
        useSurfaceOrientation true;
        outsidePoints ((-1e6 -1e6 -1e6));
        includeCut false; includeInside true; includeOutside false;
        nearDistance -1;  curvature -100;
    }
    { name fluidCells; type cellSet; action subtract; source cellToCell; set airCells; }
    { name fluidCells; type cellSet; action subtract; source cellToCell; set chipCells; }
    { name fluid; type cellZoneSet; action new; source setToCellZone; set fluidCells; }

    // 5. the plate metal is everything that is left, cut cells and all
    { name plateCells; type cellSet; action new;      source boxToCell; box (-1e6 -1e6 -1e6) (1e6 1e6 1e6); }
    { name plateCells; type cellSet; action subtract; source cellToCell; set airCells; }
    { name plateCells; type cellSet; action subtract; source cellToCell; set chipCells; }
    { name plateCells; type cellSet; action subtract; source cellToCell; set fluidCells; }
    { name solid; type cellZoneSet; action new; source setToCellZone; set plateCells; }
);
EOF
cat > "$c/system/fvSchemes" <<'EOF'
FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
EOF
printf 'FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }\nsolvers {}\n' > "$c/system/fvSolution"
chown -R 1000:1000 "$c"; chmod -R a+rwX "$c"

MPI="MPI_BUFFER_SIZE=$MPIBUF OMP_NUM_THREADS=$THREADS mpirun --allow-run-as-root --oversubscribe -x MPI_BUFFER_SIZE -x OMP_NUM_THREADS -np $RANKS"
: > "$c/TIMES.txt"

stage () {
  local name=$1 cmd=$2 t0 t1 rc
  echo "[stage] $name start $(date -u)  mem $(free -g | awk '/^Mem:/{print $3"/"$2"GB"}')"
  t0=$(date +%s)
  timeout "$CAP" docker run --rm --cpus=$CPUS -v "$c":/case -w /case "$IMG" bash -lc \
    "source /usr/lib/openfoam/openfoam2412/etc/bashrc; $cmd" > "$c/$name.dockerlog" 2>&1
  rc=$?; t1=$(date +%s)
  echo "$name $rc $(( t1 - t0 ))" >> "$c/TIMES.txt"
  echo "[stage] $name rc=$rc in $(( t1 - t0 ))s"
  chown -R 1000:1000 "$c" 2>/dev/null; chmod -R a+rwX "$c" 2>/dev/null
  [ $rc -eq 0 ] || { echo "[stage] $name FAILED:"; tail -25 "$c/$name.log" "$c/$name.dockerlog" 2>/dev/null; return 1; }
  return 0
}

publish () {
  {
    echo "COUPLED PLATE + CHIP, BOX DOMAIN, FOUR ZONES    $LAB    $(date -u)"
    echo "background $BG m, coolant +$LCOOL, microchannels +$LFINE, chip +$LCHIP, plate skin +$LSKIN"
    echo "$RANKS ranks x $THREADS threads, scaleY 3 over the microchannel band only"
    echo
    echo "  stage            rc    seconds"
    awk '{printf "  %-15s %-5s %8s\n", $1, $2, $3}' "$c/TIMES.txt" 2>/dev/null
    awk '{s+=$3} END {printf "  %-15s %-5s %8d   = %.2f hours\n", "TOTAL", "", s, s/3600}' "$c/TIMES.txt" 2>/dev/null
    echo
    echo "  the plate ALONE on verified surfaces: 326,046,158 cells, mesh 698 s, whole chain 1.62 h,"
    echo "  coolant ONE region, non-ortho 48.62 / 72.78, Mesh OK both. The reference ws_shell was"
    echo "  138,476 s for cartesianMesh alone."
    echo
    echo "=== cells"
    grep -aoE "Total number of cells [0-9]+" "$c/cartesianMesh.log" 2>/dev/null | tail -1
    echo
    echo "=== THE FOUR ZONES as topoSet built them (every cell in exactly one)"
    grep -aE "now size" "$c/topoSet.log" 2>/dev/null | tail -24
    echo
    echo "=== what splitMeshRegions made of them"
    grep -aA12 "Sizes of interfaces between regions" "$c/splitMeshRegions.log" 2>/dev/null | head -16
    grep -aA8 "^Region" "$c/splitMeshRegions.log" 2>/dev/null | head -12
    echo
    echo "=== per region"
    # THE SERIAL PASS FIRST, because it measured the RECONSTRUCTED single-piece mesh, which is what
    # is delivered. The parallel pass sums each processor's local counts, so it over-counts points
    # and faces by the processor-boundary duplicates while agreeing exactly on cells - measured on
    # cbox2: fluid 219,799,339 parallel against 219,500,627 serial, same 68,806,800 cells. Reporting
    # the parallel figures after the serial pass has run, which is what this did, shows the
    # decomposed mesh's numbers for a mesh nobody ships.
    for r in fluid solid chip air; do
      for src in "$c/checkMesh.$r.log" "$c/checkMesh.par.$r.log"; do
        [ -f "$src" ] || continue
        case "$src" in *checkMesh.par.*) which="parallel, on the decomposed mesh" ;;
                       *) which="serial, on the reconstructed mesh" ;; esac
        echo "--- $r   ($which)"
        grep -aE "^ *cells:|^ *points:|^ *faces:|^ *internal faces:|Number of regions|not connected|Boundary openness|non-orthogonality Max|severely non-orth|Max skewness|Max aspect|Total volume|Mesh OK|\*\*\*" \
          "$src" | head -14
        break
      done
    done
    echo
    echo "  EXPECTED volumes: coolant 5.369222e-05, chip 1.629485e-05, plate envelope 2.763643e-04"
    echo "  minus coolant = 2.226721e-04 m3. THE COOLANT MUST BE ONE REGION."
    echo
    echo "=== the interfaces, which must match face for face"
    echo "  (the RECONSTRUCTED boundary files are authoritative. splitMeshRegions' interface table"
    echo "   above counts internal faces between zones summed over processors, so a face on a"
    echo "   processor boundary is counted twice and the table reads slightly high: 21,722,346 for"
    echo "   fluid-solid on cbox2 against 21,708,529 in both reconstructed boundary files, which"
    echo "   agree with each other exactly. Boundary openness OK is what proves nothing was lost.)"
    for r in fluid solid chip; do
      for p in "$c/constant/$r/polyMesh/boundary" "$c/processor0/constant/$r/polyMesh/boundary"; do
        [ -f "$p" ] || continue
        echo "--- $r"
        grep -aE "^ +[a-zA-Z_]+$|nFaces|type +(mappedWall|wall|patch|empty)" "$p" | head -36
        break
      done
    done
    echo
    echo "=== integrity"
    grep -aiE "segmentation|signal 1[01]|FOAM FATAL|Cannot untangle|exited on signal|one and only one" \
      "$c/cartesianMesh.log" "$c"/*.log "$c"/*.dockerlog 2>/dev/null | head -6
    echo "  (lines above are matches, NOT an all-clear. An earlier version of this captioned them"
    echo "   \"nothing above = no crash\" while printing six FOAM FATAL lines: redistributePar fails"
    echo "   on this case with \"Cannot find a patch which is not of type empty, mapped or coupled\""
    echo "   and reconstructParMesh picks it up, which is what recon_*_fb rc=0 in the stage table"
    echo "   means. Read the lines, do not read the caption.)"
    tail -4 "$c/cartesianMesh.log" 2>/dev/null
    echo
    echo "=== meshDict and topoSetDict used"
    cat "$c/system/meshDict"
    df -h /mnt/work | tail -1
  } > "$c/RESULT.txt"
  cat "$c/RESULT.txt"
  gcloud storage cp "$c/RESULT.txt" "$B/corintis/farfield/${LAB}_RESULT.txt" --quiet 2>/dev/null
  for f in cartesianMesh topoSet splitMeshRegions checkMesh.par.fluid checkMesh.par.solid checkMesh.par.chip checkMesh.par.air; do
    [ -f "$c/$f.log" ] && gcloud storage cp "$c/$f.log" "$B/corintis/farfield/${LAB}_$f.log" --quiet 2>/dev/null
  done
}

# RANKS 1 MESHES SERIALLY. Every failure at microchannels +5 is an MPI call inside "Refining
# boxes intersecting surface meshes", and the mechanism is a 32-bit overflow in the PARALLEL load
# balancer: meshOctreeModifierLoadDistribution.C sums leaf weights with sumOp<label> on int32 and
# divides to pick a destination rank, so a negative total yields a negative rank - which is exactly
# MPI_ERR_RANK. That function returns immediately when neiProcs().size() == 0, so serial should
# never reach it. SHOULD: an earlier version of this comment called serial "the only route past
# octree level 15" as if measured, and the arm that would have shown it was killed before it
# published anything. scratchpad/serial_probe2.sh measures it properly. There is also no level cap
# in this cfMesh - cube positions are int32 labels, good to level 31 - so "level 15" was never the
# limit either. The mesh is then decomposed and the rest of the chain runs in parallel as usual.
if [ "$RANKS" -le 1 ]; then
  stage mesh "OMP_NUM_THREADS=$THREADS cartesianMesh > cartesianMesh.log 2>&1" || { publish; exit 1; }
else
  stage mesh "preparePar > preparePar.log 2>&1; $MPI cartesianMesh -parallel > cartesianMesh.log 2>&1" || { publish; exit 1; }
fi

# HARD CELL GATE. Rehaan set the ceiling at 700M. Nothing past the mesh runs if it is
# breached: topoSet and splitMeshRegions are the expensive stages and a mesh we cannot
# deliver is not worth their hours or the disk.
NC=$(grep -aoE "Total number of cells [0-9]+" "$c/cartesianMesh.log" | tail -1 | grep -oE "[0-9]+")
echo "[gate] cells ${NC:-unknown} against the 700,000,000 ceiling"
if [ -n "${NC:-}" ] && [ "$NC" -gt 700000000 ]; then
  echo "[gate] OVER THE CEILING by $(( NC - 700000000 )) cells - stopping before topoSet"
  echo "OVER THE 700M CEILING: $NC cells. Stopped before topoSet." >> "$c/TIMES.txt"
  publish; exit 2
fi
if [ "$RANKS" -le 1 ]; then
  RANKS=$PAR_RANKS
  sed -i "s/^numberOfSubdomains .*/numberOfSubdomains $RANKS;/" "$c/system/decomposeParDict"
  MPI="MPI_BUFFER_SIZE=$MPIBUF OMP_NUM_THREADS=$THREADS mpirun --allow-run-as-root --oversubscribe -x MPI_BUFFER_SIZE -x OMP_NUM_THREADS -np $RANKS"
  stage decompose "decomposePar -constant > decomposePar.log 2>&1" || { publish; exit 1; }
fi
stage topoSet "$MPI topoSet -parallel > topoSet.log 2>&1"                                               || { publish; exit 1; }
stage split   "$MPI splitMeshRegions -cellZonesOnly -overwrite -parallel > splitMeshRegions.log 2>&1"    || { publish; exit 1; }
stage regionDicts "cp -rn processor0/system/. system/ 2>/dev/null; true"
for r in fluid solid chip; do
  stage "checkPar_$r" "$MPI checkMesh -parallel -constant -region $r > checkMesh.par.$r.log 2>&1" || true
done
publish


# STOP HERE. The mesh is split, checked and decomposed, which is what a solver wants. Everything
# couple_box.sh does past this point is reconstructing the mesh so that a serial checkMesh can
# re-run checks the parallel one already passed, and it is 59% of the wall clock.
{
  echo "=== $LAB  OPTIMISED CHAIN, decomposed delivery"
  echo
  printf "%-18s %4s %8s\n" stage rc seconds
  awk '{printf "%-18s %4s %8s\n", $1, $2, $3}' "$c/TIMES.txt"
  echo
  awk '{t+=$3} END {printf "TOTAL %d s = %.2f h = %d min %02d s\n", t, t/3600, t/60, t%60}' "$c/TIMES.txt"
  echo
  echo "cbox2b for comparison, the same mesh the long way:"
  echo "  10,796 s = 3.00 h to a checked mesh, then 10,038 s for the layer pass"
  echo
  grep -aoE "Total number of cells [0-9]+" "$c/cartesianMesh.log" 2>/dev/null | tail -1
  echo
  echo "=== polyMesh on disk, binary"
  du -sb "$c/processor0/constant" 2>/dev/null | awk '{printf "   processor0 %.2f GB\n", $1/1e9}'
  du -sh "$c" 2>/dev/null | awk '{print "   whole case "$1}'
  head -c 300 "$c/processor0/constant/polyMesh/owner" 2>/dev/null | grep -aE "format|class" | head -2
  echo
  echo "=== every checkMesh -parallel verdict"
  for r in fluid solid chip; do
    printf "   %-6s %s\n" "$r" "$(grep -aE '^Mesh OK|^Failed' "$c/checkMesh.par.$r.log" 2>/dev/null | tail -1)"
    grep -aE "^ +cells:" "$c/checkMesh.par.$r.log" 2>/dev/null | tail -1
  done
  echo
  df -h /mnt/work | tail -1
} | tee "$c/FAST_RESULT.txt"
gcloud storage cp "$c/FAST_RESULT.txt" "$B/corintis/farfield/${LAB}_FAST_RESULT.txt" --quiet 2>/dev/null
echo "================ $LAB done $(date -u)"
