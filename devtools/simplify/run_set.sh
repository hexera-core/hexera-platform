#!/bin/bash
# Responsibility: Run the P1 ground truth (harness.py --keep-fields) and the T1/T2 local check (local.py) over a list of corpus parts on the bench, one part at a time, skipping finished ones.
# Usage: run_set.sh <list file: one path per line, relative to CORPUS> <results dir> [CORPUS=~/bench/corpus2/corpus]
set -u
LIST=$1; OUT=$2; CORPUS=${3:-$HOME/bench/corpus2/corpus}
PY=/opt/bench/venv/bin/python
cd ~/bench
# wait for any bench job already holding the cores
while pgrep -f "harness[.]py|local[.]py" >/dev/null; do sleep 30; done
while read -r rel; do
  [ -z "$rel" ] && continue
  n=$(basename "$rel"); n=${n%.*}
  d="$OUT/$n"
  echo "=== $n $(date -u +%H:%M:%S)"
  if [ ! -f "$d/result.json" ]; then
    timeout 5400 $PY -I code/harness.py "$CORPUS/$rel" --out "$d" --jobs 16 --keep-fields 2>&1 | grep -vE '^\*\*\*|^Warning' | grep -vE ': ok elements' | tail -3
  fi
  if [ -f "$d/result.json" ] && [ ! -f "$d/local_k3_r2.json" ]; then
    timeout 5400 $PY -I code/local.py "$CORPUS/$rel" --harness "$d" --k 3 --refine 2 --jobs 16 2>&1 | grep -vE '^\*\*\*|^Warning|: ok$' | tail -3
  fi
done < "$LIST"
echo "=== done $(date -u +%H:%M:%S)"
