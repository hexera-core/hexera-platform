#!/bin/bash
# Launch snapshot v3 the moment v2 is DONE (its launcher exited: doc written, or stopped at its deadline and its doc
# written, VM deleted). One wave at a time. Logs to v3_after_v2.log.
#   - v3's commit = the feature tip resolved AT LAUNCH (fetched, recorded by launch.sh)
#   - prior rows for the shard plan = v2's collected rows (this code's measured pace)
#   - MAX_RUN = 7h if v2 finished on its own, 8h if v2 had to be stopped at its deadline (stated to the coordinator)
#   - the deadline guard for v3 is armed at (VM creation + MAX_RUN - 30 min), same rule as v1r/v2
L=/Users/kitts/archductor/snap-runs/v3_after_v2.log
W=/Users/kitts/archductor/sdd-worktrees/snap-next
V2OUT=/Users/kitts/archductor/snap-runs/v2-launch-2.out
log() { echo "$(date -u +%FT%TZ) $*" >> $L; }
log "waiting for v2's launcher to exit"
until grep -q '^launch rc=' $V2OUT 2>/dev/null; do sleep 60; done
v2rc=$(grep '^launch rc=' $V2OUT | tail -1)
V2RUN=$(grep -o 'run dir .*' $V2OUT | head -1 | sed 's/run dir //')
log "v2 launcher exited: $v2rc; run dir $V2RUN"
if [ -f "$V2RUN/out/deadline_summary.json" ]; then MAX_RUN=8h; why="v2 was stopped at its deadline"; else MAX_RUN=7h; why="v2 finished on its own"; fi
case "$v2rc" in *"rc=0") ;; *) log "NOT launching v3: v2's launcher did NOT exit 0 ($v2rc) (coordinator to decide)"; exit 1;; esac
if gcloud compute instances list --project hexera-dev --filter='name=dev-crsv4-snap-v2' --format='value(name)' | grep -q .; then
  log "NOT launching v3: dev-crsv4-snap-v2 still exists"; exit 1; fi
if [ ! -f "$V2RUN/vm.json" ] || ! ls /Users/kitts/archductor/sdd-worktrees/snap-v2/docs/benchmarks/crs-v4-dev-snapshot-v2.md >/dev/null 2>&1; then
  log "NOT launching v3: v2's collection or doc is missing ($V2RUN/vm.json, docs/benchmarks/crs-v4-dev-snapshot-v2.md)"; exit 1; fi
cd $W || exit 1
git fetch -q 2>>$L; TIP=$(git rev-parse pranavkannepalli/crs-v4-search-repair)
log "tip at launch: $TIP; MAX_RUN=$MAX_RUN ($why)"
P=$W/.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/prior-v2
rm -rf $P && python3 -I /Users/kitts/archductor/snap-runs/build_prior.py "$V2RUN/out" $P >> $L 2>&1
V2COMMIT=$(grep '^COMMIT=' "$V2RUN/run.env" | cut -d= -f2)
SD=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
SD2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
NOTES=/Users/kitts/archductor/snap-runs/v3-notes.md
cat > $NOTES <<NOTE
## READ FIRST: the THIRD wave on the v2 SPLITS, at its OWN commit. NOT a rerun of v2.

| | this wave (v3) | v2 |
|---|---|---|
| run commit | \`$TIP\` | \`$V2COMMIT\` |
| dev split \`PROVISIONAL_dev_v2.json\` sha256 | \`$SD\` | \`$SD2\` |
| clean split \`PROVISIONAL_clean_v2.json\` sha256 | \`$SC\` | (same file path at v2's commit) |
| WaveDefs | \`snap_v2_dev\` / \`snap_v2_clean\` (they name the SPLITS, not the wave) | same |
| wave ids | \`snap-v3-v2splits-{dev,clean}-<ts>\` (in every PROVENANCE, ledger and row job id) | \`snap-v2-{dev,clean}-202611240348\` |

- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default (Lane D: ops never bind).
- v3's code includes Lane D's aimed and reordered defeature, Lane C's no-op decline, the **rollback_detail crash fix** (v2 ran WITH that crash), whole-part recognised defeature off by default, and per-file search traces.
- **More measured (file, arm) pairs here than in v2 is partly the crash fix, not repair ability**: see "Measured (file, arm) pairs: this wave vs v2" below, which counts the pairs v2 lost to harness crashes against this wave's.
- Image mesh build (toolchain) \`1d6ed735\`; run commit, bind-mounted, \`$TIP\`.
NOTE
log "v3 notes written ($NOTES); splits sha dev $SD (v2's $SD2)"
MAX_RUN=$MAX_RUN NOTES_MD=$NOTES BASELINE_RUN="$V2RUN" BASELINE_LABEL=v2 bash -c "bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/launch.sh $TIP v3; echo \"launch rc=\$?\"" > /Users/kitts/archductor/snap-runs/v3-launch.out 2>&1 &
LPID=$!
log "v3 launcher started (pid $LPID)"
# arm the v3 deadline guard once its VM is up
for i in $(seq 1 60); do
  line=$(grep -o 'VM dev-crsv4-snap-v3 up in [^ ]* at [^ ]*' /Users/kitts/archductor/snap-runs/v3-launch.out 2>/dev/null | head -1)
  [ -n "$line" ] && break; grep -q 'STOP:' /Users/kitts/archductor/snap-runs/v3-launch.out 2>/dev/null && break; sleep 20
done
if [ -n "$line" ]; then
  created=${line##* }; h=${MAX_RUN%h}
  at=$(python3 -I -c "from datetime import datetime,timedelta;t=datetime.fromisoformat('$created'.replace('Z','+00:00'))+timedelta(hours=$h,minutes=-30);print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))")
  nohup bash /Users/kitts/archductor/snap-runs/deadline_guard.sh dev-crsv4-snap-v3 $at > /dev/null 2>&1 < /dev/null &
  log "v3 VM up at $created; deadline guard armed for $at (reaper at $created + $MAX_RUN)"
else
  log "v3 VM did not come up (see v3-launch.out); no guard armed"
fi
wait $LPID; log "v3 launcher exited: $(grep '^launch rc=' /Users/kitts/archductor/snap-runs/v3-launch.out | tail -1)"
