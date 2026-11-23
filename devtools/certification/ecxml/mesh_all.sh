#!/usr/bin/env bash
# Mesh EVERY model in the lab, twice, and compare the two meshes byte for byte.
#
# "Deterministic" at the plan stage is a claim about numbers in memory. This is the claim that
# matters to a customer: the same file, meshed twice, gives the same mesh - the same points, the
# same faces, the same region boundaries, the same thermal model. Written in ASCII so a diff is a
# diff and not a float32 layout question.
#
# A model the mesher refuses is a PASS as long as it refuses the same way twice. A model that
# meshes once and refuses once, or meshes differently, is the failure this is looking for.
set -uo pipefail
# Where this lab lives, worked out from this script rather than from one person's home.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${HEXERA_REPO:-$(cd "$HERE/../../.." && pwd)}"
LAB="${HEXERA_ECXML_LAB:-$REPO/.ecxml_lab}"
RESULTS=$LAB/mesh_results
mkdir -p "$RESULTS"
: > "$RESULTS/summary.tsv"

# every file, in a stable order
mapfile -t FILES < <(cd "$LAB/models" && find . -name '*.ecxml' | sed 's|^\./||; s|\.ecxml$||' | sort)

echo "meshing ${#FILES[@]} models, twice each"
for w in "${FILES[@]}"; do
  slug="${w//\//_}"
  for pass in a b; do
    log="$RESULTS/$slug.$pass.log"
    start=$(date +%s)
    timeout 1200 bash "$HERE/mesh_one.sh" "$w" --level draft --ascii > "$log" 2>&1
    rc=$?
    end=$(date +%s)
    mv "$LAB/cases/$slug" "$LAB/cases/$slug.$pass" 2>/dev/null
    echo -e "$w\t$pass\t$rc\t$((end - start))" >> "$RESULTS/summary.tsv"
  done

  # the comparison: every mesh file and the thermal model, in both passes
  A="$LAB/cases/$slug.a"
  B="$LAB/cases/$slug.b"
  if [ -d "$A" ] && [ -d "$B" ]; then
    # the report carries timings, which are not part of the mesh; drop it from the comparison
    if diff -r -q -x 'snapgrid_report.json' "$A" "$B" > "$RESULTS/$slug.diff" 2>&1; then
      verdict=IDENTICAL
    else
      verdict=DIFFERS
    fi
  else
    verdict=NO_MESH
  fi
  ra=$(awk -F'\t' -v w="$w" '$1==w && $2=="a" {print $3}' "$RESULTS/summary.tsv")
  rb=$(awk -F'\t' -v w="$w" '$1==w && $2=="b" {print $3}' "$RESULTS/summary.tsv")
  printf '%-34s rc %s/%s  %s\n' "$w" "$ra" "$rb" "$verdict"
  echo -e "$w\tVERDICT\t$verdict\t$ra\t$rb" >> "$RESULTS/summary.tsv"
done
echo "=== all done at $(date -u +%H:%M:%S) ==="
