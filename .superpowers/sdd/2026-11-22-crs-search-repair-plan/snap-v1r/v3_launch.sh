#!/bin/bash
# Launch snapshot v3 BY HAND on the coordinator's GO:   v3_launch.sh <SHA>
# Refuses unless v2's VM is gone and v2's doc exists. Writes the v3 header notes (provenance side by side, the crash-fix
# caveat, Lane A's two data findings), launches with MAX_RUN (7h; 8h if v2 was stopped), arms the deadline guard at
# creation + MAX_RUN - 30 min, and reports. Logs to v3_launch.log.
set -u
SHA_ARG=${1:?usage: v3_launch.sh <SHA>}
L=/Users/kitts/archductor/snap-runs/v3_launch.log
W=/Users/kitts/archductor/sdd-worktrees/snap-next
V2OUT=/Users/kitts/archductor/snap-runs/v2-launch-2.out
log() { echo "$(date -u +%FT%TZ) $*" | tee -a $L; }
V2RUN=$(grep -o 'run dir .*' $V2OUT | head -1 | sed 's/run dir //')
if gcloud compute instances list --project hexera-dev --filter='name=dev-crsv4-snap-v2' --format='value(name)' | grep -q .; then
  log "NOT launching v3: dev-crsv4-snap-v2 still exists (stop/finish v2 first)"; exit 1; fi
if [ ! -f "$V2RUN/vm.json" ] || [ ! -f /Users/kitts/archductor/sdd-worktrees/snap-v2/docs/benchmarks/crs-v4-dev-snapshot-v2.md ]; then
  log "NOT launching v3: v2's collection or doc is missing"; exit 1; fi
if [ -f "$V2RUN/out/deadline_summary.json" ]; then MAX_RUN=8h; why="v2 was stopped before finishing"; else MAX_RUN=7h; why="v2 finished on its own"; fi
cd $W || exit 1
git fetch -q 2>>$L; TIP=$(git rev-parse --verify "$SHA_ARG^{commit}") || { log "NOT launching v3: cannot resolve $SHA_ARG"; exit 1; }
V2COMMIT=$(grep '^COMMIT=' "$V2RUN/run.env" | cut -d= -f2)
SD=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
SD2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
log "GO: v3 at $TIP (from $SHA_ARG); MAX_RUN=$MAX_RUN ($why); dev split sha $SD (v2: $SD2), clean $SC (v2: $SC2)"
P=$W/.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/prior-v2
rm -rf $P && python3 -I /Users/kitts/archductor/snap-runs/build_prior.py "$V2RUN/out" $P >> $L 2>&1
NOTES=/Users/kitts/archductor/snap-runs/v3-notes.md
cat > $NOTES <<NOTE
## READ FIRST: the THIRD wave on the v2 SPLITS, at its OWN commit. NOT a rerun of v2.

| | this wave (v3) | v2 |
|---|---|---|
| run commit | \`$TIP\` | \`$V2COMMIT\` |
| dev split \`PROVISIONAL_dev_v2.json\` sha256 | \`$SD\` | \`$SD2\` |
| clean split \`PROVISIONAL_clean_v2.json\` sha256 | \`$SC\` | \`$SC2\` |
| WaveDefs | \`snap_v2_dev\` / \`snap_v2_clean\` (they name the SPLITS, not the wave) | same |
| wave ids | \`snap-v3-v2splits-{dev,clean}-<ts>\` (in every PROVENANCE, ledger and row job id) | \`snap-v2-{dev,clean}-202611240348\` |

- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default (Lane D: ops never bind).
- v3's code carries the **rollback_detail crash fix** (v2 ran WITH that crash), Lane D's gmsh_prod padding fix and the power-of-two quantum fix, aimed and reordered defeature, Lane C's no-op decline, whole-part recognised defeature off by default, per-file search traces, located faces in every row, and every (file, arm)'s delivered BREP.
- **More measured (file, arm) pairs here than in v2 is partly the crash fix, not repair ability**: see "Measured (file, arm) pairs: this wave vs v2".
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): \`4c1fc7d5579d\` and \`e63710eae340\` are the same part (477 faces, diagonal 437.112 mm, identical face 9). One defect is double-counted in every dev_v2 rate, v2's and v3's alike. The split is unchanged so v3 matches v2.
- **Coincident duplicate faces** (Lane A, A11): in \`7e573047c1a6\` faces 108 and 109 are coincident duplicates (identical area 9032.0789 mm2, aspect 230:1); a mesher naming both is naming two copies of one face.
- Image mesh build (toolchain) \`1d6ed735\`; run commit, bind-mounted, \`$TIP\`.
NOTE
MAX_RUN=$MAX_RUN NOTES_MD=$NOTES BASELINE_RUN="$V2RUN" BASELINE_LABEL=v2 nohup bash -c "bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/launch.sh $TIP v3; echo \"launch rc=\$?\"" > /Users/kitts/archductor/snap-runs/v3-launch.out 2>&1 < /dev/null &
log "v3 launcher started"
for i in $(seq 1 60); do
  line=$(grep -o 'VM dev-crsv4-snap-v3 up in [^ ]* at [^ ]*' /Users/kitts/archductor/snap-runs/v3-launch.out 2>/dev/null | head -1)
  [ -n "$line" ] && break; grep -q 'STOP:' /Users/kitts/archductor/snap-runs/v3-launch.out 2>/dev/null && break; sleep 20
done
if [ -n "$line" ]; then
  created=${line##* }; h=${MAX_RUN%h}
  at=$(python3 -I -c "from datetime import datetime,timedelta;t=datetime.fromisoformat('$created'.replace('Z','+00:00'))+timedelta(hours=$h,minutes=-30);print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))")
  nohup bash /Users/kitts/archductor/snap-runs/deadline_guard.sh dev-crsv4-snap-v3 $at > /dev/null 2>&1 < /dev/null &
  log "v3 VM up at $created; reaper $MAX_RUN; deadline guard armed for $at"
else
  log "v3 VM did not come up: $(grep 'STOP:' /Users/kitts/archductor/snap-runs/v3-launch.out | tail -1)"
fi
