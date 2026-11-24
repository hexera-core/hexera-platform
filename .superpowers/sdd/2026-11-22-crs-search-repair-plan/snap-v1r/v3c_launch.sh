#!/bin/bash
# Launch snapshot v3c (v3 on the aimed-ops commit) BY HAND:   v3c_launch.sh <SHA>
# Refuses unless v2's VM is gone and v2's doc exists. Writes the v3 header notes (provenance side by side, the crash-fix
# caveat, Lane A's two data findings), launches with MAX_RUN (7h; 8h if v2 was stopped), arms the deadline guard at
# creation + MAX_RUN - 30 min, and reports. Logs to v3_launch.log.
set -u
SHA_ARG=${1:?usage: v3_launch.sh <SHA>}
L=/Users/kitts/archductor/snap-runs/v3c_launch.log
W=/Users/kitts/archductor/sdd-worktrees/snap-next
V2OUT=/Users/kitts/archductor/snap-runs/v2-launch-2.out
log() { echo "$(date -u +%FT%TZ) $*" | tee -a $L; }
V2RUN=$(grep -o 'run dir .*' $V2OUT | head -1 | sed 's/run dir //')
V3RUN=/Users/kitts/archductor/snap-runs/v3b-202611240606
if gcloud compute instances list --project hexera-dev --filter='name=dev-crsv4-snap-v3b' --format='value(name)' | grep -q .; then
  log "NOT launching v3c: dev-crsv4-snap-v3b still exists (its stop must finish first)"; exit 1; fi
if [ ! -f "$V3RUN/vm.json" ] || [ ! -f /Users/kitts/archductor/sdd-worktrees/snap-next/docs/benchmarks/crs-v4-dev-snapshot-v3b.md ]; then
  log "NOT launching v3c: the stopped v3b's collection or doc is missing"; exit 1; fi
MAX_RUN=8h; why="as ordered (8 h; guard at creation + 7.5 h)"
cd $W || exit 1
git fetch -q 2>>$L; TIP=$(git rev-parse --verify "$SHA_ARG^{commit}") || { log "NOT launching v3: cannot resolve $SHA_ARG"; exit 1; }
V2COMMIT=$(grep '^COMMIT=' "$V2RUN/run.env" | cut -d= -f2)
SD=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC=$(git show $TIP:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
SD2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json | shasum -a 256 | cut -c1-64)
SC2=$(git show $V2COMMIT:devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_clean_v2.json | shasum -a 256 | cut -c1-64)
log "GO: v3c at $TIP (from $SHA_ARG); MAX_RUN=$MAX_RUN ($why); dev split sha $SD (v2: $SD2), clean $SC (v2: $SC2)"
P=$W/.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/prior-v2
rm -rf $P && python3 -I /Users/kitts/archductor/snap-runs/build_prior.py "$V2RUN/out" $P >> $L 2>&1
NOTES=/Users/kitts/archductor/snap-runs/v3c-notes.md
cat > $NOTES <<NOTE
## READ FIRST: v3c is the FIRST wave with the AIMED OPS, on the v2 SPLITS. Not a rerun of v2, v3 or v3b.

| | v3c (this wave) | v3b (stopped, superseded) | v3 (stopped, regression) | v2 |
|---|---|---|---|---|
| run commit | \`$TIP\` | \`e2b25ddf6496f26add2d23f8a10159661f4a1c95\` | \`323d659f8037b760a32436b6531f38ee99c2b59e\` | \`$V2COMMIT\` |
| dev / clean split sha256 | \`$SD\` / \`$SC\` | same files | same files | \`$SD2\` / \`$SC2\` |
| wave ids | \`snap-v3c-v2splits-{dev,clean}-<ts>\` | \`snap-v3b-v2splits-*-202611240606\` | \`snap-v3-v2splits-*-202611240544\` | \`snap-v2-*-202611240348\` |
| what it can measure | aimed ops: contact family, slivers, rebuild_face / split_angle at named faces | engine before aimed ops (contact candidates all rejected) | crippled search (A10 regression) + body-op ceiling | crash + regression + ceiling |

- **What v3c adds over v3b:** \`e23af4b3\` (Lane A): the contact row builders now give \`remove_duplicate_faces\` its params, so contact candidates validate (contacts are 18 of 35 located failures); \`rebuild_face\` and \`split_angle\` are aimed at the faces the mesher named. \`4c74661e\` (Lane D, D18): \`unify_same_domain\` / \`remove_small\` aimed at the elongated face.
- Also on this commit: the A10 layer-3 regression fix (\`c96fd583\`), Lane C11's empty-compound fix (a body op CAN end clean), the rollback_detail crash fix, \`located\` in every row, the delivered BREP per (file, arm), search traces.
- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default.
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): \`4c1fc7d5579d\` and \`e63710eae340\`; one defect is double-counted in every dev_v2 rate. **Coincident duplicate faces** in \`7e573047c1a6\` (faces 108 and 109). The split is unchanged.
- "Measured (file, arm) pairs: this wave vs v2" separates pairs v2 lost to harness crashes from this wave's.
- Image mesh build (toolchain) \`1d6ed735\`; run commit, bind-mounted, \`$TIP\`.
NOTE
MAX_RUN=$MAX_RUN NOTES_MD=$NOTES BASELINE_RUN="$V2RUN" BASELINE_LABEL=v2 nohup bash -c "bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/launch.sh $TIP v3c; echo \"launch rc=\$?\"" > /Users/kitts/archductor/snap-runs/v3c-launch.out 2>&1 < /dev/null &
log "v3c launcher started"
for i in $(seq 1 60); do
  line=$(grep -o 'VM dev-crsv4-snap-v3c up in [^ ]* at [^ ]*' /Users/kitts/archductor/snap-runs/v3c-launch.out 2>/dev/null | head -1)
  [ -n "$line" ] && break; grep -q 'STOP:' /Users/kitts/archductor/snap-runs/v3c-launch.out 2>/dev/null && break; sleep 20
done
if [ -n "$line" ]; then
  created=${line##* }; h=${MAX_RUN%h}
  at=$(python3 -I -c "from datetime import datetime,timedelta;t=datetime.fromisoformat('$created'.replace('Z','+00:00'))+timedelta(hours=$h,minutes=-30);print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))")
  nohup bash /Users/kitts/archductor/snap-runs/deadline_guard.sh dev-crsv4-snap-v3c $at > /dev/null 2>&1 < /dev/null &
  log "v3c VM up at $created; reaper $MAX_RUN; deadline guard armed for $at"
else
  log "v3c VM did not come up: $(grep 'STOP:' /Users/kitts/archductor/snap-runs/v3c-launch.out | tail -1)"
fi
