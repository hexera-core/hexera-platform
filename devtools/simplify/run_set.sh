#!/bin/bash
# Responsibility: Run the P1 ground truth (harness.py --keep-fields), the T1/T2 local check (local.py), the package truth (package.py) and the one-solve driver check over a list of corpus parts on the bench, one part at a time, skipping finished ones.
# Usage: run_set.sh <list file: one path per line, relative to CORPUS> <results dir> [CORPUS=~/bench/corpus2/corpus]
set -u
LIST=$1; OUT=$2; CORPUS=${3:-$HOME/bench/corpus2/corpus}
PY=/opt/bench/venv/bin/python
T=${PART_TIMEOUT:-5400}   # seconds per stage; the heavy parts (ctc_04, the KiCad heat sink) need hours
J=${JOBS:-16}             # parallel jobs; at the wall-rule size a big part needs fewer (memory), each then gets cores / J solver threads
cd ~/bench
# wait for any bench job already holding the cores
while pgrep -f "bench/venv/bin/python .*(harness|local)[.]py" >/dev/null; do sleep 30; done
# the list on fd 3: a solver subprocess reading stdin would otherwise swallow the remaining lines
while read -r rel <&3; do
  [ -z "$rel" ] && continue
  n=$(basename "$rel"); n=${n%.*}
  d="$OUT/$n"
  echo "=== $n $(date -u +%H:%M:%S)"
  if [ ! -f "$d/result.json" ]; then
    timeout $T $PY -I code/harness.py "$CORPUS/$rel" --out "$d" --jobs $J --keep-fields 2>&1 | grep -vE '^\*\*\*|^Warning' | grep -vE ': ok elements' | tail -3
  fi
  if [ -f "$d/result.json" ] && [ ! -f "$d/local_k3_r2.json" ]; then
    timeout $T $PY -I code/local.py "$CORPUS/$rel" --harness "$d" --k 3 --refine 2 --jobs $J 2>&1 | grep -vE '^\*\*\*|^Warning|: ok$' | tail -3
  fi
  # the package truth (do member errors add?) and the one-solve driver the product would run
  if [ -f "$d/result.json" ] && [ ! -f "$d/package_b0.001.json" ]; then
    timeout $T $PY -I code/package.py "$CORPUS/$rel" --harness "$d" --jobs 2 2>&1 | grep -vE '^\*\*\*|^Warning' | tail -3
  fi
  if [ -f "$d/geom/package_all.brep" ] && [ ! -f "$d/local_k3_r2_drv-package_all-s0.json" ]; then
    timeout $T $PY -I code/local.py "$CORPUS/$rel" --harness "$d" --k 3 --refine 2 --jobs $J \
      --driver-brep "$d/geom/package_all.brep" 2>&1 | grep -vE '^\*\*\*|^Warning|: ok$' | tail -2
  fi
done 3< "$LIST"
echo "=== done $(date -u +%H:%M:%S)"
