#!/bin/bash
# Build couple_fast.sh from couple_box.sh and run the optimised chain end to end.
#
# WHY. MESH_TIME_TO_ONE_HOUR.md quotes 2,735 s = 46 min as the sub-hour chain, and every line of
# that is a stage that ran, but the total is a SUM of stages from a run that also did a lot of other
# things. A sum of measured parts is not a measured whole: it assumes no stage gets slower when the
# ones around it are removed, and it assumes binary writeFormat helps the readers rather than only
# the writer. So run it.
#
# THE FOUR CHANGES from couple_box.sh, and nothing else:
#   1. writeFormat binary, so every stage after the mesher reads 1.74x faster
#   2. the air region's checkPar dropped, since air is not delivered
#   3. no reconstructParMesh at all, because a solver wants the decomposed mesh
#   4. no serial checkMesh, because checkMesh -parallel already ran the same nine criteria
# The mesh itself, the zones, the levels and the rank count are identical to cbox2b.
set -u
SRC=/mnt/work/cbox2b_record/couple_box.sh
DST=/mnt/work/couple_fast.sh

[ -s "$SRC" ] || { echo "FATAL: no $SRC"; exit 1; }
sudo mkdir -p /mnt/work
sudo cp "$SRC" "$DST"

# 1. binary
sudo sed -i 's/^writeFormat     ascii;/writeFormat     binary;/' "$DST"
# 2. drop air from the check loop
sudo sed -i 's/^for r in fluid solid chip air; do$/for r in fluid solid chip; do/' "$DST"
# 3 and 4. stop after the check loop's publish
sudo python3 - "$DST" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p).read()
cut = s.index("# merge only what we deliver")
head = s[:cut]
tail = '''
# STOP HERE. The mesh is split, checked and decomposed, which is what a solver wants. Everything
# couple_box.sh does past this point is reconstructing the mesh so that a serial checkMesh can
# re-run checks the parallel one already passed, and it is 59% of the wall clock.
{
  echo "=== $LAB  OPTIMISED CHAIN, decomposed delivery"
  echo
  printf "%-18s %4s %8s\\n" stage rc seconds
  awk '{printf "%-18s %4s %8s\\n", $1, $2, $3}' "$c/TIMES.txt"
  echo
  awk '{t+=$3} END {printf "TOTAL %d s = %.2f h = %d min %02d s\\n", t, t/3600, t/60, t%60}' "$c/TIMES.txt"
  echo
  echo "cbox2b for comparison, the same mesh the long way:"
  echo "  10,796 s = 3.00 h to a checked mesh, then 10,038 s for the layer pass"
  echo
  grep -aoE "Total number of cells [0-9]+" "$c/cartesianMesh.log" 2>/dev/null | tail -1
  echo
  echo "=== polyMesh on disk, binary"
  du -sb "$c/processor0/constant" 2>/dev/null | awk '{printf "   processor0 %.2f GB\\n", $1/1e9}'
  du -sh "$c" 2>/dev/null | awk '{print "   whole case "$1}'
  head -c 300 "$c/processor0/constant/polyMesh/owner" 2>/dev/null | grep -aE "format|class" | head -2
  echo
  echo "=== every checkMesh -parallel verdict"
  for r in fluid solid chip; do
    printf "   %-6s %s\\n" "$r" "$(grep -aE '^Mesh OK|^Failed' "$c/checkMesh.par.$r.log" 2>/dev/null | tail -1)"
    grep -aE "^ +cells:" "$c/checkMesh.par.$r.log" 2>/dev/null | tail -1
  done
  echo
  df -h /mnt/work | tail -1
} | tee "$c/FAST_RESULT.txt"
gcloud storage cp "$c/FAST_RESULT.txt" "$B/corintis/farfield/${LAB}_FAST_RESULT.txt" --quiet 2>/dev/null
echo "================ $LAB done $(date -u)"
'''
open(p, "w").write(head + tail)
print("   wrote %d lines, was %d" % ((head + tail).count("\\n"), s.count("\\n")))
PYEOF

sudo chmod +x "$DST"
echo "--- the four changes, verified in the file ---"
grep -n "writeFormat" "$DST"
grep -n "^for r in fluid solid chip" "$DST"
grep -c "reconstructParMesh" "$DST" || true
grep -c "checkMesh_" "$DST" || true
echo "--- disk before ---"
df -h /mnt/work | tail -1
echo "--- launching ---"
cd /mnt/work
sudo setsid bash "$DST" cbox2f 0.000322020 1 4 2 1 8 12 > /dev/null 2>&1 < /dev/null &
sleep 25
echo "--- first 20 lines of the log ---"
sudo tail -20 /var/log/cbox2f.log 2>/dev/null
echo MKFAST_LAUNCHED
