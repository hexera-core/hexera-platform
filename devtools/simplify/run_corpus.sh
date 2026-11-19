#!/bin/bash
# Run the P1 harness over every corpus part, one part at a time, 16 jobs each.
cd ~/bench
for p in corpus/*.stp; do
  n=$(basename "$p" .stp | sed 's/_asme1_r[a-z]//')
  [ -f "results/$n/result.json" ] && continue
  echo "=== $n $(date -u +%H:%M:%S)"
  timeout 5400 /opt/bench/venv/bin/python -I code/harness.py "$p" --out "results/$n" --jobs 16 --size 30 2>&1 | grep -vE '^\*\*\*|^Warning' | tail -3
done
echo "=== done $(date -u +%H:%M:%S)"
