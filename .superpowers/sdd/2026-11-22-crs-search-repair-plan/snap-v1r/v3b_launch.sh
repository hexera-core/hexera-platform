#!/bin/bash
# Launch snapshot v3b (the relaunch of v3 on the fixed commit) BY HAND:   v3b_launch.sh <SHA>
# Refuses unless v2's VM is gone and v2's doc exists. Writes the v3 header notes (provenance side by side, the crash-fix
# caveat, Lane A's two data findings), launches with MAX_RUN (7h; 8h if v2 was stopped), arms the deadline guard at
# creation + MAX_RUN - 30 min, and reports. Logs to v3_launch.log.
set -u
SHA_ARG=${1:?usage: v3_launch.sh <SHA>}
L=/Users/kitts/archductor/snap-runs/v3b_launch.log
W=/Users/kitts/archductor/sdd-worktrees/snap-next
V2OUT=/Users/kitts/archductor/snap-runs/v2-launch-2.out
log() { echo "$(date -u +%FT%TZ) $*" | tee -a $L; }
V2RUN=$(grep -o 'run dir .*' $V2OUT | head -1 | sed 's/run dir //')
V3RUN=/Users/kitts/archductor/snap-runs/v3-202611240544
if gcloud compute instances list --project hexera-dev --filter='name=dev-crsv4-snap-v3' --format='value(name)' | grep -q .; then
  log "NOT launching v3b: dev-crsv4-snap-v3 still exists (its stop must finish first)"; exit 1; fi
if [ ! -f "$V3RUN/vm.json" ] || [ ! -f /Users/kitts/archductor/sdd-worktrees/snap-next/docs/benchmarks/crs-v4-dev-snapshot-v3.md ]; then
  log "NOT launching v3b: the stopped v3's collection or doc is missing"; exit 1; fi
MAX_RUN=8h; why="as ordered (8 h; guard at creation + 7.5 h)"
cd $W || exit 1
git fetch -q 2>>$L; TIP=$(git rev-parse --verify "$SHA_ARG^{commit}") || { log "NOT launching v3: cannot resolve $SHA_ARG"; exit 1; }
V2COMMIT=$(grep '^COMMIT=' "$V2RUN/run.env" | cut -d= -f2)
SD=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
SD2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
log "GO: v3b at $TIP (from $SHA_ARG); MAX_RUN=$MAX_RUN ($why); dev split sha $SD (v2: $SD2), clean $SC (v2: $SC2)"
P=$W/.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/prior-v2
rm -rf $P && python3 -I /Users/kitts/archductor/snap-runs/build_prior.py "$V2RUN/out" $P >> $L 2>&1
NOTES=/Users/kitts/archductor/snap-runs/v3b-notes.md
cat > $NOTES <<NOTE
## READ FIRST: v3b is the RELAUNCH of v3 on a FIXED commit, on the v2 SPLITS. Not a rerun of v2, and not the first v3 attempt.

| | v3b (this wave) | v3 first attempt (stopped) | v2 |
|---|---|---|---|
| run commit | \`$TIP\` | \`323d659f8037b760a32436b6531f38ee99c2b59e\` | \`$V2COMMIT\` |
| dev split \`PROVISIONAL_dev_v2.json\` sha256 | \`$SD\` | same file | \`$SD2\` |
| clean split \`PROVISIONAL_clean_v2.json\` sha256 | \`$SC\` | same file | \`$SC2\` |
| wave ids | \`snap-v3b-v2splits-{dev,clean}-<ts>\` | \`snap-v3-v2splits-{dev,clean}-202611240544\` | \`snap-v2-{dev,clean}-202611240348\` |
| how it ended | (this doc) | stopped at 06:03Z: search frontier mis-ordered by the A10 regression | stopped early for v3 |

- **The first v3 attempt (`323d659f`) was stopped early because its search arms measured a crippled engine**: the A10 layer-3 regression (first bad `ac7fbf52`, fixed in `c96fd583`) left minus-deviation as the only key on the cheap tier, so the frontier preferred the state that had repaired LESS. That run's doc (`crs-v4-dev-snapshot-v3.md`) keeps its rows as evidence of the bug. **v3b carries the fix.**
- **The body-op ceiling no longer applies.** The first v3 attempt could not end any body op clean (Lane C11's empty compound; netgen_occ `Bnd_Box is void`). v3b's commit carries C11's fix (native proof: two overlapping boxes, 4 of 4 members, clean), so a `cut_by` or `fuse` output CAN reach clean here, and the 42 body-overlap dev files are measured without that cap.
- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default.
- v3b's code also carries the **rollback_detail crash fix** (v2 ran WITH that crash): see "Measured (file, arm) pairs: this wave vs v2", which separates pairs v2 lost to harness crashes from this wave's.
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): \`4c1fc7d5579d\` and \`e63710eae340\` (477 faces, diagonal 437.112 mm, identical face 9); one defect is double-counted in every dev_v2 rate. **Coincident duplicate faces**: in \`7e573047c1a6\` faces 108 and 109 are coincident duplicates. The split is unchanged.
- Every row carries \`located\`; every (file, arm) keeps its delivered BREP; search traces are collected per (split, arm, sha12).
- Image mesh build (toolchain) \`1d6ed735\`; run commit, bind-mounted, \`$TIP\`.
NOTE
MAX_RUN=$MAX_RUN NOTES_MD=$NOTES BASELINE_RUN="$V2RUN" BASELINE_LABEL=v2 nohup bash -c "bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/launch.sh $TIP v3b; echo \"launch rc=\$?\"" > /Users/kitts/archductor/snap-runs/v3b-launch.out 2>&1 < /dev/null &
log "v3b launcher started"
for i in $(seq 1 60); do
  line=$(grep -o 'VM dev-crsv4-snap-v3b up in [^ ]* at [^ ]*' /Users/kitts/archductor/snap-runs/v3b-launch.out 2>/dev/null | head -1)
  [ -n "$line" ] && break; grep -q 'STOP:' /Users/kitts/archductor/snap-runs/v3b-launch.out 2>/dev/null && break; sleep 20
done
if [ -n "$line" ]; then
  created=${line##* }; h=${MAX_RUN%h}
  at=$(python3 -I -c "from datetime import datetime,timedelta;t=datetime.fromisoformat('$created'.replace('Z','+00:00'))+timedelta(hours=$h,minutes=-30);print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))")
  nohup bash /Users/kitts/archductor/snap-runs/deadline_guard.sh dev-crsv4-snap-v3b $at > /dev/null 2>&1 < /dev/null &
  log "v3b VM up at $created; reaper $MAX_RUN; deadline guard armed for $at"
else
  log "v3b VM did not come up: $(grep 'STOP:' /Users/kitts/archductor/snap-runs/v3b-launch.out | tail -1)"
fi
