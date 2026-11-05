#!/bin/bash
# WHY ws_par300 WAS 58x FASTER THAN ws_shell ON MORE CELLS - and whether the fast one is usable.
#
#   ws_shell  270,531,894 cells  138,476 s  331 bad faces after 340 smoothing iterations
#   ws_par300 336,223,062 cells    2,374 s  every bad-face line reading 0, BUT the coolant came
#                                           out in 5 disconnected pieces, "ok": false
#
# ws_par300 changed two things at once and nobody separated them: no fluid_fine band, and a
# smaller stretch. Bad faces are born at the back-scale (0 at log line 361, 1,358 at line 392),
# so a bigger stretch means a harder squash - but the fine band is also what puts 22 um cells
# against the channel walls, and dropping it is the obvious way to end up with five pieces.
# So the question is not "which is fast" but "is either fast AND still one connected passage".
#
# WHAT THE FIRST ATTEMPT GOT WRONG, both fixed here:
#   * decomposePar. cfMesh has its own parallel prep, `preparePar`; decomposePar wants a 0/
#     directory and a mesh, neither of which exists before meshing, so all four runs died in
#     about a second.
#   * MPI_BUFFER_SIZE. Setting it in the launching shell sets it for mpirun and nothing else -
#     each rank reads its own environment at Pstream init - so without `-x` it stays at
#     controlDict's 20 MB and the build dies in cfMesh's load balancing. Taken verbatim from
#     conjugate.mpi_command rather than retyped.
#
# RANKS. The engine's own ranks_for(270,531,894) returns 2, not 8. ws_shell ran 8 because the box
# had 8 cores, and omp_threads = cores // ranks made that 1 thread for an optimiser that has
# OpenMP all through it. Here: 2 ranks x 16 threads on 32 cores. `base` is therefore also the
# measurement of that fix on its own, against ws_shell's 38.5 h.
set -uo pipefail
B=gs://dev-rehaan-artifacts-224734058693
STL=$B/corintis/meshlab/stl
OUT=$B/corintis/meshlab/RESULTS.txt
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/mesh:semi-e7149685-20261029-1324
BASHRC=/usr/lib/openfoam/openfoam2412/etc/bashrc
W=/mnt/work
R=/tmp/results.txt
CAP=${CAP:-14400}
RANKS=2
THREADS=16
MPIB=1000000000

say() { echo "[lab] $*"; }
push() { gcloud storage cp "$R" "$OUT" --quiet >/dev/null 2>&1; }

{
  echo "meshing time experiment"
  echo "started  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host     $(hostname), $(nproc) cores, $(free -g | awk '/^Mem:/{print $2}') GB"
  echo "config   $RANKS ranks x $THREADS OMP threads  (ws_shell ran 8 x 1)"
  echo "cap      ${CAP}s per run"
  echo
  echo "reference  ws_shell  band=1 stretch=6  270,531,894 cells  138,476 s  331 bad  (8x1 ranks)"
  echo "reference  ws_par300 band=0 stretch=3  336,223,062 cells    2,374 s    0 bad  BUT 5 pieces"
  echo
} > "$R"
push

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq docker.io >/dev/null 2>&1
say "docker $(docker --version 2>&1 | head -1)"
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet >/dev/null 2>&1
if ! docker pull "$IMG" >/dev/null 2>&1; then
  echo "IMAGE PULL FAILED: $IMG" >> "$R"; push; exit 1
fi
say "image pulled"

mkdir -p "$W" && cd "$W" || exit 1
gcloud storage cp "$STL/ws_fitted_constant_triSurface__*.stl" "$W/" --quiet 2>&1 | tail -1
BLOCK=$W/ws_fitted_constant_triSurface__block.stl
FLUID=$W/ws_fitted_constant_triSurface__fluid.stl
FINE=$W/ws_fitted_constant_triSurface__fluid_fine.stl
for f in "$BLOCK" "$FLUID" "$FINE"; do
  [ -s "$f" ] || { echo "MISSING $f" >> "$R"; push; exit 1; }
done
say "stls ok"

MAXCELL=0.000352056

mkcase() {   # $1 dir, $2 band(1/0), $3 scaleY
  local c="$1"
  mkdir -p "$c/system" "$c/constant/triSurface"
  cp "$BLOCK" "$c/constant/triSurface/block.stl"
  cp "$FLUID" "$c/constant/triSurface/fluid.stl"
  cp "$FINE"  "$c/constant/triSurface/fluid_fine.stl"
  {
    echo 'FoamFile { version 2.0; format ascii; class dictionary; object meshDict; }'
    echo "surfaceFile \"constant/triSurface/block.stl\";"
    echo "maxCellSize $MAXCELL;"
    echo 'surfaceMeshRefinement'
    echo '{'
    echo '    coolant'
    echo '    {'
    echo '        surfaceFile "constant/triSurface/fluid.stl";'
    echo '        additionalRefinementLevels 1;'
    echo "        refinementThickness $MAXCELL;"
    echo '    }'
    [ "$2" = "1" ] && {
      echo '    microchannels'
      echo '    {'
      echo '        surfaceFile "constant/triSurface/fluid_fine.stl";'
      echo '        additionalRefinementLevels 4;'
      echo '        refinementThickness 8.8014e-05;'
      echo '    }'; }
    echo '}'
    echo 'anisotropicSources'
    echo '{'
    echo '    microchannels'
    echo '    {'
    echo '        type box;'
    echo '        centre (0.02267671418 0.02752817822 0.0424025383);'
    echo '        lengthX 0.05985824678;'
    echo '        lengthY 0.01218805644;'
    echo '        lengthZ 0.05133793399;'
    echo '        scaleX 1;'
    echo "        scaleY $3;"
    echo '        scaleZ 1;'
    echo '    }'
    echo '}'
  } > "$c/system/meshDict"
  cat > "$c/system/controlDict" <<'CD'
FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application cartesianMesh;
startFrom startTime; startTime 0; stopAt endTime; endTime 1; deltaT 1;
writeControl timeStep; writeInterval 1; writeFormat ascii; writePrecision 6;
CD
  cat > "$c/system/decomposeParDict" <<CP
FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }
numberOfSubdomains $RANKS;
method scotch;
CP
}

run() {   # $1 label, $2 band, $3 scaleY
  local lab="$1" c="$W/$1" t0 t1 rc secs
  say "=== $lab  band=$2 scaleY=$3"
  rm -rf "$c"; mkcase "$c" "$2" "$3"
  t0=$(date +%s)
  timeout "$CAP" docker run --rm -v "$c":/case -w /case "$IMG" bash -lc \
    "source $BASHRC >/dev/null 2>&1 || true; \
     preparePar > preparePar.log 2>&1; \
     MPI_BUFFER_SIZE=$MPIB OMP_NUM_THREADS=$THREADS mpirun --allow-run-as-root --oversubscribe \
       -x MPI_BUFFER_SIZE -x OMP_NUM_THREADS -np $RANKS cartesianMesh -parallel \
       > cartesianMesh.log 2>&1; \
     MPI_BUFFER_SIZE=$MPIB OMP_NUM_THREADS=$THREADS mpirun --allow-run-as-root --oversubscribe \
       -x MPI_BUFFER_SIZE -x OMP_NUM_THREADS -np $RANKS checkMesh -parallel -constant \
       > checkMesh.log 2>&1" > "$c/docker.log" 2>&1
  rc=$?
  t1=$(date +%s); secs=$(( t1 - t0 ))

  local cells bad iters regions nonortho
  cells=$(grep -oE "Total number of cells [0-9]+" "$c/cartesianMesh.log" 2>/dev/null | tail -1 | grep -oE "[0-9]+$")
  bad=$(grep -oE "Number of bad faces is [0-9]+" "$c/cartesianMesh.log" 2>/dev/null | tail -1 | grep -oE "[0-9]+$")
  iters=$(grep -c "Number of bad faces is" "$c/cartesianMesh.log" 2>/dev/null)
  regions=$(grep -oE "Number of regions: [0-9]+" "$c/checkMesh.log" 2>/dev/null | tail -1 | grep -oE "[0-9]+$")
  nonortho=$(grep -oE "Max non-orthogonality = [0-9.]+" "$c/checkMesh.log" 2>/dev/null | tail -1 | grep -oE "[0-9.]+$")

  if [ "$rc" = "124" ]; then
    printf '%-7s band=%s sY=%s  CAP %ss HIT   cells=%-12s bad=%-6s iters=%s\n' \
      "$lab" "$2" "$3" "$CAP" "${cells:-?}" "${bad:-?}" "${iters:-0}" >> "$R"
  else
    printf '%-7s band=%s sY=%s  %7ss  cells=%-12s bad=%-6s iters=%-4s regions=%-4s nonOrtho=%s\n' \
      "$lab" "$2" "$3" "$secs" "${cells:-?}" "${bad:-?}" "${iters:-0}" "${regions:-?}" "${nonortho:-?}" >> "$R"
    [ -z "$cells" ] && { echo "         (no cells - docker said:)" >> "$R"
                         head -4 "$c/docker.log" 2>/dev/null | sed 's/^/         /' >> "$R"
                         tail -6 "$c/cartesianMesh.log" 2>/dev/null | sed 's/^/         /' >> "$R"; }
  fi
  push
  for l in cartesianMesh checkMesh preparePar docker; do
    [ -f "$c/$l.log" ] && gcloud storage cp "$c/$l.log" "$B/corintis/meshlab/${lab}_${l}.log" --quiet >/dev/null 2>&1
  done
  rm -rf "$c/processor"* 2>/dev/null
}

run both   0 3
run noband 0 6
run s3     1 3
run base   1 6

echo "" >> "$R"
echo "finished $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$R"
push
say "ALL DONE"
