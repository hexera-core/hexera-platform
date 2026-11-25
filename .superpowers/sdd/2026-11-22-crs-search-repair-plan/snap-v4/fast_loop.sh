#!/bin/bash
# THE FAST LOOP (owner directive 2026-11-24), a standing job on this Mac. Every 10 min: fetch; if the feature tip
# changed since the last fast run, at least 60 min have passed since that run STARTED, no fast run is in flight, and
# the tip CONTAINS the FAST-DEV v1 list and the fast WaveDefs (it refuses to run anything else), launch the fast
# profile on that tip from a detached runner worktree, then write the delta against the previous run.
# Never merges, never edits the frozen list. Logs to fast_loop.log; results in runs.tsv and delta-<ts>.md.
set -u
F=/Users/kitts/archductor/snap-runs/fast; L=$F/fast_loop.log
REPO=/Users/kitts/archductor/sdd-worktrees/snap-next; RUNNER=/Users/kitts/archductor/sdd-worktrees/fast-runner
BRANCH=origin/pranavkannepalli/crs-v4-search-repair
log() { echo "$(date -u +%FT%TZ) $*" >> $L; }
[ -d $RUNNER ] || git -C $REPO worktree add --detach $RUNNER $BRANCH >> $L 2>&1
touch $F/runs.tsv
log "fast loop started (pid $$)"
while true; do
  git -C $REPO fetch -q >> $L 2>&1
  TIP=$(git -C $REPO rev-parse $BRANCH)
  LAST_TIP=$(tail -1 $F/runs.tsv | cut -f2); LAST_START=$(tail -1 $F/runs.tsv | cut -f1)
  NOW=$(date +%s); AGE=$(( NOW - ${LAST_START:-0} ))
  if pgrep -f "snap-v4/launch.sh .* fast" >/dev/null; then :                                    # one at a time
  elif [ "$TIP" = "$LAST_TIP" ]; then :                                                           # nothing merged
  elif [ "$AGE" -lt 3600 ]; then :                                                                # at most hourly
  elif ! git -C $REPO merge-base --is-ancestor ebf81bc9e "$TIP"; then
    # the tip must carry the fast-loop FIXES (rows identical to v2, cleanup deletes every VM): without them every
    # unit fails on the split and the cleanup leaks VMs (what the 08:26 run would have done)
    [ "$TIP" != "${WARNED:-}" ] && log "tip $TIP lacks ebf81bc9e (merge sdd/fast-loop): not running"; WARNED=$TIP
  elif ! git -C $REPO cat-file -e "$TIP:devtools/validation/corpora/v4/fast_dev_v1/FAST_dev_v1.json" 2>/dev/null \
       || ! git -C $REPO show "$TIP:devtools/validation/v4_eval/evalspec.py" | grep -q 'fast_v1_dev'; then
    [ "$TIP" != "${WARNED:-}" ] && log "tip $TIP lacks FAST-DEV v1 / fast WaveDefs (merge sdd/fast-loop): not running"; WARNED=$TIP
  else
    git -C $RUNNER checkout -q --detach $TIP >> $L 2>&1 || { log "runner checkout of $TIP failed"; sleep 600; continue; }
    TS=$(date -u +%Y%m%d%H%M); OUT=$F/run-$TS.out
    log "launching fast run on $TIP"
    printf '%s\t%s\t%s\n' "$NOW" "$TIP" "$TS" >> $F/runs.tsv
    # the approved walls and the measured density, explicitly (a tip whose profile predates them must not run at 300 s);
    # and a region with ONE c2d-56's worth of free C2D and CPUS, chosen now (tonight's quota moved three times)
    REG=""
    for cand in us-west2:us-west2-a,us-west2-b,us-west2-c us-west4:us-west4-a,us-west4-b,us-west4-c us-south1:us-south1-a,us-south1-b,us-south1-c us-east4:us-east4-a,us-east4-b,us-east4-c us-west1:us-west1-a,us-west1-b,us-west1-c us-central1:us-central1-b,us-central1-a,us-central1-c us-east1:us-east1-b,us-east1-c,us-east1-d; do
      r=${cand%%:*}; free=$(gcloud compute regions describe $r --project hexera-dev --format=json 2>/dev/null | python3 -I -c "
import json,sys; q={x['metric']:x for x in json.load(sys.stdin)['quotas']}
print(int(min(q['C2D_CPUS']['limit']-q['C2D_CPUS']['usage'], q['CPUS']['limit']-q['CPUS']['usage']))))" 2>/dev/null)
      [ "${free:-0}" -ge 56 ] && { REG="$r:56:${cand#*:}"; break; }
    done
    [ -n "$REG" ] || { log "no region has 56 free C2D+CPUS: not running"; sed -i '' '$d' $F/runs.tsv; sleep 600; continue; }
    log "region $REG; walls 1200/900 s; VCPU_PER_SHARD=1.5"
    ( cd $RUNNER && FILE_TIMEOUT_S=1200 SEARCH_WALL_S=900 VCPU_PER_SHARD=1.5 REGIONS="$REG" \
        bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v4/launch.sh $TIP fast > $OUT 2>&1; echo "launch rc=$?" >> $OUT )
    RUN=$(grep -o 'run dir [^ ]*' $OUT | head -1 | cut -d' ' -f3)
    PREV=$(cat $F/LAST_RUN_DIR 2>/dev/null || true)
    if grep -q '^launch rc=0' $OUT && [ -n "$RUN" ]; then
      python3 -I $F/fast_delta.py ${PREV:+--prev "$PREV"} --cur "$RUN" --md $F/delta-$TS.md > /dev/null 2>>$L \
        && cp $F/delta-$TS.md $F/LATEST_DELTA.md && echo "$RUN" > $F/LAST_RUN_DIR && log "run $TS done: $F/delta-$TS.md"
    else
      log "run $TS FAILED: $(grep -E 'STOP|FAIL|launch rc' $OUT | tail -2 | tr '\n' ' ')"; echo "$TS FAILED" > $F/LATEST_DELTA.md
    fi
  fi
  sleep 600
done
