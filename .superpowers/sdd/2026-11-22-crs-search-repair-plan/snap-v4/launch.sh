#!/bin/bash
# ONE command for a CRS v4 snapshot on SEVERAL VMs in SEVERAL REGIONS (lane B14/B17). Resolves the commit, checks
# everything that needs no VM and no model, creates the VMs the plan asks for, stages inputs on each, runs that VM's
# own (arm group, shard) units, syncs partial rows every 10 min, regroups and reports, DELETES every VM, and EXITS
# NON-ZERO if any of that failed.
#
#   bash snap-v4/launch.sh <commit> [v4] [--dry-run]
#
# A separate launcher, so snap-v1r/launch.sh keeps running its wave untouched. What is new here:
#   * PER-ARM GROUPS, each sized to its own measured work; search_ai is capped at the in-flight ceiling by prep.py.
#   * MULTI-REGION: REGIONS is a profile variable, not a hardcoded zone list. Every VM is c2d in every region, so
#     one benchmark's wall_s stays comparable.
#   * REGROUP: post.py presents the groups as one wave per split, so report.py's gates apply unchanged.
# MEASURED cross-region cost (B17, one c2d VM per region): a verdict-cache get is 345 ms from us-east1 against
# 110 ms in us-central1, input staging 71 MB in 6 s against 5 s, and the 4.34 GB image pulls in 50 s in BOTH.
#
# WRITTEN FOR BASH 3.2 (macOS): no mapfile, no `declare -A`, no `wait -n`. The VM list is a file and each VM's
# zone, exit code and outcome are files, because an array of arrays is exactly what this needed and could not have.
set -u -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(git -C "$HERE" rev-parse --show-toplevel)
COMMIT_ARG=${1:?usage: launch.sh <commit> [profile] [--dry-run]}; PROFILE=v4; DRY=0
for x in "${@:2}"; do case $x in --dry-run) DRY=1;; *) PROFILE=$x;; esac; done
KEYS="REGIONS TARGET_MIN TOTAL_USD MAX_RUN IMAGE IMAGE_COMMIT OPS ARM_GROUPS FILE_TIMEOUT_S SEARCH_WALL_S VCPU_PER_SHARD"
for k in $KEYS; do eval "OVR_$k=\${$k:-}"; done
source "$HERE/profiles/$PROFILE.env"
for k in $KEYS; do eval "v=\$OVR_$k"; [ -n "$v" ] && eval "$k=\$v"; done
OPS=${OPS:-}
PROJECT=hexera-dev; BUCKET=dev-crsv3-transfer-224734058693; SECRET=openai-api-key
TS=$(date -u +%Y%m%d%H%M); RUN=${SNAP_RUNS:-$HOME/archductor/snap-runs}/$LABEL-$TS
SYNC=gs://$BUCKET/eval/snap-$LABEL-$TS/partial-sync
FAILURES=0
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$RUN/launch.log"; }
die() { log "STOP: $*" >&2; exit 1; }
fail() { FAILURES=$((FAILURES + 1)); log "FAILURE: $*"; }
mkdir -p "$RUN/splits" "$RUN/tools"

# ---------------------------------------------------------------- 1. commit
COMMIT=$(git -C "$REPO" rev-parse --verify "$COMMIT_ARG^{commit}") || die "cannot resolve $COMMIT_ARG"
log "profile=$PROFILE commit=$COMMIT regions=$REGIONS run dir $RUN"
HELDOUT=${HELDOUT:-0}
if [ "$HELDOUT" = 1 ]; then
  # THE FREEZE GUARD (lane C14), before ANYTHING else - dry run included: the held-out wave runs once, on the commit a
  # freeze tag names, and that commit must carry every REQUIRE_COMMITS entry (B18's anytime rule). A refusal, not a
  # warning: the held-out files are spent the moment they run.
  REQ_ARGS=""; for r in ${REQUIRE_COMMITS:-}; do REQ_ARGS="$REQ_ARGS --require $r"; done
  FROZEN=$(python3 -I "$HERE/freeze_guard.py" --repo "$REPO" --commit "$COMMIT" \
    --pattern "${FREEZE_PATTERN:?a held-out profile sets FREEZE_PATTERN}" $REQ_ARGS 2>"$RUN/freeze_guard.err") \
    || die "$(cat "$RUN/freeze_guard.err")"
  log "freeze guard: $FROZEN"
fi
git -C "$REPO" archive --format=tar "$COMMIT" > "$RUN/code.tar" || die "git archive failed"
mkdir -p "$RUN/code" && tar xf "$RUN/code.tar" -C "$RUN/code" devtools src || die "extract failed"
# IMPORT PRE-FLIGHT, before any VM: byte-compile the engine and the eval code of THIS commit. A SyntaxError (the
# ws_kernel docstring that broke 36261625..b53614732) fails here in a second instead of on every unit twenty
# minutes into a wave. A full import needs OCP and the image; the compile catches what the image cannot fix.
python3 -I -m compileall -q "$RUN/code/src/meshpipeline/cad/repair_v4" "$RUN/code/devtools/validation/v4_eval" \
  > "$RUN/compile.log" 2>&1 || die "commit $COMMIT does NOT compile (see $RUN/compile.log): $(grep -m1 -E 'Error' "$RUN/compile.log")"
log "pre-flight: repair_v4 and v4_eval compile at $COMMIT"

# ---------------------------------------------------------------- 2. the op budget is the COMMIT's own
DEFAULT_OPS=$(cd "$RUN/code" && PYTHONPATH="$RUN/code/src" python3 -c "
import sys; sys.path.insert(0,'devtools/validation/v4_eval')
import evalspec; print(int(evalspec.SHARED_BUDGET.ops))") || die "cannot read evalspec.SHARED_BUDGET.ops from $COMMIT"
if [ -z "$OPS" ]; then
  log "ops: the commit's own default ($DEFAULT_OPS); run_arm is not given --ops"
else
  [ "$OPS" -le "$DEFAULT_OPS" ] || die "OPS=$OPS is above the commit's Budget.ops $DEFAULT_OPS: run_arm only LOWERS it"
  log "ops: $OPS (BELOW the commit's default $DEFAULT_OPS - set OPS= empty unless you mean to lower it)"
fi

# ---------------------------------------------------------------- 3. splits
resolve_split() { # spec override out
  local spec=$1 override=$2 out=$3
  case $spec in COMMIT:*) spec="$COMMIT:${spec#COMMIT:}";; esac   # a split frozen IN the run commit
  if [ -n "$override" ]; then [ -f "$override" ] || die "split file $override not found"; cp "$override" "$out"; echo "file:$override"; return; fi
  if git -C "$REPO" cat-file -e "$spec" 2>/dev/null; then git -C "$REPO" show "$spec" > "$out"; echo "git:$spec@$(git -C "$REPO" rev-parse "${spec%%:*}")"; return; fi
  die "split $spec is not written yet (pass DEV_SPLIT=/path or CLEAN_SPLIT=/path)"
}
DEV_SRC=$(resolve_split "$DEV_SPLIT_SPEC" "${DEV_SPLIT:-}" "$RUN/splits/dev.json") || exit 1
CLEAN_SRC=$(resolve_split "$CLEAN_SPLIT_SPEC" "${CLEAN_SPLIT:-}" "$RUN/splits/clean.json") || exit 1
log "dev split $DEV_SRC; clean split $CLEAN_SRC"

# ---------------------------------------------------------------- 4. inputs, groups, VM plan, caps (prep.py)
if [ "$HELDOUT" = 1 ]; then
  # NO WILDCARD over the inputs prefix: it would list every held-out object name. Each URL is built from a split's
  # own sha256 + basename. The calibration split (dev, NOT held-out) and the clean control are listed explicitly; the
  # HELD-OUT objects are listed only on a REAL run (sizes for staging and packing). A dry run touches no held-out
  # object at all and plans them at a nominal size, so its shard counts are indicative, not final.
  CALIB_SRC=$(resolve_split "${CALIB_SPLIT_SPEC:?a held-out profile sets CALIB_SPLIT_SPEC}" "" "$RUN/splits/calib.json") || exit 1
  python3 -I -c "
import json,sys
from pathlib import PurePosixPath as P
b=sys.argv[1]
def urls(f):
    return [f'gs://{b}/eval/v4-reprobe/inputs/{x[\"sha256\"]}/{P(x[\"name\"]).name}' for x in json.load(open(f))['files']]
open(sys.argv[2],'w').write('\n'.join(urls(sys.argv[3])+urls(sys.argv[4]))+'\n')
open(sys.argv[5],'w').write('\n'.join(urls(sys.argv[6]))+'\n')
" "$BUCKET" "$RUN/ls_open.txt" "$RUN/splits/calib.json" "$RUN/splits/clean.json" "$RUN/ls_sealed.txt" "$RUN/splits/dev.json" \
    || die "cannot build the input URLs from the splits"
  : > "$RUN/inputs_ls.txt"
  tr '\n' '\0' < "$RUN/ls_open.txt" | xargs -0 -n 50 gcloud storage ls -l >> "$RUN/inputs_ls.txt" 2>"$RUN/inputs_ls.err" \
    || die "cannot list the calibration/clean inputs (see $RUN/inputs_ls.err)"
  if [ "$DRY" = 1 ]; then
    sed 's|^|1000000  1970-01-01T00:00:00Z  |' "$RUN/ls_sealed.txt" >> "$RUN/inputs_ls.txt"
    log "held-out inputs: $(wc -l < "$RUN/ls_sealed.txt" | tr -d ' ') objects NOT touched (dry run: nominal 1 MB each)"
  else
    tr '\n' '\0' < "$RUN/ls_sealed.txt" | xargs -0 -n 50 gcloud storage ls -l >> "$RUN/inputs_ls.txt" 2>>"$RUN/inputs_ls.err" \
      || die "cannot list the held-out inputs by name"
  fi
else
  gcloud storage ls -l "gs://$BUCKET/eval/v4-reprobe/inputs/**" > "$RUN/inputs_ls.txt" 2>"$RUN/inputs_ls.err" || die "cannot list inputs"
fi
[ -s "$RUN/inputs_ls.txt" ] || die "the inputs listing is empty"
PRIOR="$REPO/$PRIOR_ROWS"
[ -d "$PRIOR" ] || die "PRIOR_ROWS $PRIOR does not exist: the shard plan is sized on a prior wave's MEASURED walls"
python3 -I "$HERE/prep.py" --code "$RUN/code" --profile "$PROFILE" --dev-split "$RUN/splits/dev.json" \
  --clean-split "$RUN/splits/clean.json" --dev-wave "$DEV_WAVE" --clean-wave "$CLEAN_WAVE" \
  --total-usd "$TOTAL_USD" --file-usd-cap "$FILE_USD_CAP" --inputs-ls "$RUN/inputs_ls.txt" \
  --prior-rows "$PRIOR" --regions $REGIONS --target-min "$TARGET_MIN" --route "$ROUTE" \
  --label "$LABEL" --ts "$TS" ${OPS:+--ops "$OPS"} ${ARM_GROUPS:+--groups "$ARM_GROUPS"} \
  ${FILE_TIMEOUT_S:+--file-timeout-s "$FILE_TIMEOUT_S"} ${SEARCH_WALL_S:+--search-wall-s "$SEARCH_WALL_S"} \
  ${VCPU_PER_SHARD:+--vcpu-per-shard "$VCPU_PER_SHARD"} ${DEV_SPLIT_NAME:+--dev-split-name "$DEV_SPLIT_NAME"} \
  --out "$RUN/splits" | tee -a "$RUN/launch.log"
[ "${PIPESTATUS[0]}" = 0 ] || die "prep refused"
cp "$RUN/splits/launch_manifest.json" "$RUN/splits/vm_plan.json" "$RUN/"

# ---------------------------------------------------------------- 5. quota, per region, against the plan
python3 -I -c "
import json,sys,collections
plan=json.load(open(sys.argv[1]))
need=collections.Counter()
for v in plan['vms']: need[v['region']] += int(v['vcpu'])
for r,n in sorted(need.items()): print(r,n)
" "$RUN/vm_plan.json" > "$RUN/quota_need.txt" || die "cannot read the VM plan"
[ -s "$RUN/quota_need.txt" ] || die "the VM plan asks for no VM"
while read -r REGION NEED; do
  gcloud compute regions describe "$REGION" --project $PROJECT --format=json </dev/null \
    > "$RUN/quota_$REGION.json" || die "cannot read $REGION quota"
  python3 -I -c "
import json,sys
q={x['metric']:x for x in json.load(open(sys.argv[1]))['quotas']}
need=int(sys.argv[2]); region=sys.argv[3]; bad=[]
for m in ('C2D_CPUS','CPUS'):
    free=q[m]['limit']-q[m]['usage']
    print(f'{region} {m}: limit {q[m][\"limit\"]:.0f} usage {q[m][\"usage\"]:.0f} free {free:.0f}, need {need}')
    if free < need: bad.append(f'{m} free {free:.0f} < {need}')
if bad: sys.exit('QUOTA REFUSED in ' + region + ': ' + '; '.join(bad))
" "$RUN/quota_$REGION.json" "$NEED" "$REGION" | tee -a "$RUN/launch.log"
  [ "${PIPESTATUS[0]}" = 0 ] || die "quota refused in $REGION"
done < "$RUN/quota_need.txt"

IMAGE_REF=${IMAGE:?set IMAGE to a digest-pinned image}
IMAGE_COMMIT=${IMAGE_COMMIT:?set IMAGE_COMMIT to the sha the image label must carry}
LABEL_REV=$(python3 -I "$HERE/../snap-v1r/image_label.py" "$IMAGE_REF" --token "$(gcloud auth print-access-token)" --expect "$IMAGE_COMMIT") \
  || die "image label check refused: $IMAGE_REF label '$LABEL_REV', expected $IMAGE_COMMIT"
log "image $IMAGE_REF label=$LABEL_REV"

# the model key must EXIST and have content before a single VM is created: an empty read used to leave every
# search_ai unit exiting 6 and the launcher still printing DONE
# NEVER written to local disk: the length is checked through a pipe, and each VM gets its own fresh read
KEY_LEN=$(gcloud secrets versions access latest --secret "$SECRET" --project $PROJECT 2>"$RUN/key.err" \
  | tr -d '\n' | wc -c | tr -d ' ')
[ "${KEY_LEN:-0}" -ge 20 ] || die "secret $SECRET read back ${KEY_LEN:-0} characters: that is not a key ($(tail -1 "$RUN/key.err" 2>/dev/null))"
log "model key: $KEY_LEN characters from Secret Manager ($SECRET)"

cat > "$RUN/run.env" <<EOF
LABEL=$LABEL
TS=$TS
COMMIT=$COMMIT
IMAGE_REF=$IMAGE_REF
IMAGE_COMMIT=$IMAGE_COMMIT
MACHINE=multi
OPS=$OPS
FILE_USD_CAP=$FILE_USD_CAP
ROUTE=$ROUTE
BUCKET=$BUCKET
CACHE=gs://$BUCKET/eval/snap-$LABEL-$TS/verdict-cache
SYNC=$SYNC
FILE_TIMEOUT_S=${FILE_TIMEOUT_S:-}
SEARCH_WALL_S=${SEARCH_WALL_S:-}
FROZEN_COMMIT=$( [ "$HELDOUT" = 1 ] && echo "$COMMIT" )
HELDOUT_SLOT=$( [ "$HELDOUT" = 1 ] && echo "gs://$BUCKET/model-ledger/slot/held_out/1" )
EOF
cp "$HERE/vm_run.sh" "$HERE/post.py" "$HERE/regroup.py" "$HERE/collect.sh" "$RUN/tools/"
python3 -I -c "
import json,sys
print('\n'.join(v['name'] for v in json.load(open(sys.argv[1]))['vms']))
" "$RUN/vm_plan.json" > "$RUN/vms.txt" || die "cannot list the plan's VMs"
log "VMs: $(tr '\n' ' ' < "$RUN/vms.txt")"
if [ "$HELDOUT" = 1 ]; then
  # THE ONCE-ONLY SLOT (evalspec.claim_wave_slot's key, value = the held-out group's wave_id, model_ledger's
  # ObjectMarkerStore layout). The dry run READS it and refuses if another wave holds it; a real run CLAIMS it here,
  # with the bucket's create-if-absent (--if-generation-match=0), before any VM exists. vm_run.sh's own plan_wave runs
  # on a fresh local store and only shapes the provenance; this object is the claim the report verifies.
  HO_WID=$(python3 -I -c "
import json,sys
g=[v for v in json.load(open(sys.argv[1]))['groups'].values() if v['split']=='dev']
assert len(g)==1, g
print(g[0]['wave_id'])" "$RUN/launch_manifest.json") || die "the held-out run must be ONE group with one wave id"
  SLOT="gs://$BUCKET/model-ledger/slot/held_out/1"
  HOLDER=$(gcloud storage cat "$SLOT" 2>/dev/null || true)
  if [ -n "$HOLDER" ] && [ "$HOLDER" != "$HO_WID" ]; then
    die "the held_out slot is SPENT: $SLOT holds wave '$HOLDER'. The held-out files run once (evalspec.claim_wave_slot); releasing the slot is a deliberate human act, not a launcher option."
  fi
  log "held_out slot $SLOT: ${HOLDER:-free}; this run's wave id $HO_WID"
fi
if [ "$DRY" = 1 ]; then
  log "DRY RUN complete: no VM created, no model called; the key was length-checked through a pipe, never stored."
  cat "$RUN/run.env" | tee -a "$RUN/launch.log"
  exit 0
fi

# ---------------------------------------------------------------- 6. the VMs (always deleted on the way out)
# EVERY VM, every time, and a survivor is a FAILURE - not a line in the log. The first pilot leaked one for 35
# minutes because `gcloud` inside a `while read` loop CONSUMED the loop's stdin and the second VM was never
# iterated: hence `for ... $(cat)` and `< /dev/null` on every gcloud call in a loop.
delete_vms() {
  for vm in $(cat "$RUN/vms.txt"); do
    [ "${KEEP_VM:-0}" = 1 ] && continue
    # delete this NAME in EVERY zone it exists in, not only the recorded one: a leaked duplicate is never left
    # behind (the fast-loop lane's fix; my pilot 1 leaked exactly that way, a second VM of the same name in
    # another zone after a failed create). `< /dev/null` on every gcloud call because inside a loop reading a
    # file ssh would EAT the rest of the VM list and only the first VM would be deleted.
    for z in $(gcloud compute instances list --project $PROJECT --filter="name=$vm" --format='value(zone.basename())' </dev/null 2>/dev/null); do
      gcloud compute ssh "$vm" --zone "$z" --project $PROJECT --command 'shred -u ~/snap/secret.env 2>/dev/null; true' </dev/null >/dev/null 2>&1
      gcloud compute instances delete "$vm" --zone "$z" --project $PROJECT --quiet </dev/null >> "$RUN/launch.log" 2>&1 &
    done
  done
  wait
}
cleanup() {
  local rc=$?
  delete_vms
  gcloud compute instances list --project $PROJECT --filter="labels.snapshot=$LABEL" \
    --format='value(name,zone,status)' </dev/null > "$RUN/vm_surviving.txt" 2>&1
  if [ -s "$RUN/vm_surviving.txt" ] && [ "${KEEP_VM:-0}" != 1 ]; then
    log "VMs STILL PRESENT after the first delete: $(tr '\n' ' ' < "$RUN/vm_surviving.txt"); retrying"
    delete_vms
    gcloud compute instances list --project $PROJECT --filter="labels.snapshot=$LABEL" \
      --format='value(name,zone,status)' </dev/null > "$RUN/vm_surviving.txt" 2>&1
    if [ -s "$RUN/vm_surviving.txt" ]; then
      log "LEAKED VM(S), DELETE THEM BY HAND NOW: $(tr '\n' ' ' < "$RUN/vm_surviving.txt")"
      [ "$rc" = 0 ] && rc=1
    fi
  fi
  log "VM deletes done; surviving: $(tr '\n' ' ' < "$RUN/vm_surviving.txt")"
  exit $rc
}
trap cleanup EXIT INT TERM
if [ "$HELDOUT" = 1 ] && [ -z "$HOLDER" ]; then
  printf '%s' "$HO_WID" > "$RUN/slot.value"
  gcloud storage cp --if-generation-match=0 "$RUN/slot.value" "$SLOT" >> "$RUN/launch.log" 2>&1 \
    || die "could not CLAIM the held_out slot $SLOT (another launcher may have taken it a moment ago): nothing was run"
  [ "$(gcloud storage cat "$SLOT")" = "$HO_WID" ] || die "the held_out slot read back something other than $HO_WID"
  log "CLAIMED the held_out slot for $HO_WID"
fi

cat > "$RUN/startup.sh" <<EOF
#!/bin/bash
if [ ! -f /var/snap-ready ]; then
  apt-get update -y && apt-get install -y docker.io && systemctl enable --now docker
  gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
  docker pull $IMAGE_REF && docker tag $IMAGE_REF meshpipeline-mesh:native && touch /var/snap-ready
fi
EOF

vm_field() { python3 -I -c "
import json,sys
v=next(v for v in json.load(open(sys.argv[1]))['vms'] if v['name']==sys.argv[2])
f=v[sys.argv[3]]
print(' '.join(f) if isinstance(f,list) else f)
" "$RUN/vm_plan.json" "$1" "$2"; }

for vm in $(cat "$RUN/vms.txt"); do
  machine=$(vm_field "$vm" machine); zones=$(vm_field "$vm" zones); created=""
  for z in $zones; do
    if gcloud compute instances create "$vm" --project $PROJECT --zone "$z" --machine-type "$machine" \
        --provisioning-model=STANDARD --max-run-duration="$MAX_RUN" --instance-termination-action=DELETE \
        --labels deployment-id=dev-crsv4,purpose=v4-snapshot,owner=lane-b17,snapshot=$LABEL \
        --image-family debian-12 --image-project debian-cloud --boot-disk-size 200GB --boot-disk-type pd-balanced \
        --scopes cloud-platform --metadata-from-file startup-script="$RUN/startup.sh" >> "$RUN/launch.log" 2>&1; then
      echo "$z" > "$RUN/zone.$vm"; created=$z; break
    fi
    # A create can exit non-zero AFTER the instance was inserted (an operation timeout, a post-insert error). Trying
    # the next zone then makes a SECOND VM of the same name that nothing keyed on name can see (the B17 pilot leak).
    # So before any other zone: does this name exist anywhere? If it does, ADOPT it and stop.
    found=$(gcloud compute instances list --project $PROJECT --filter="name=$vm" --format='value(zone.basename())' 2>>"$RUN/launch.log")
    if [ -n "$found" ]; then
      [ "$(echo "$found" | wc -l | tr -d ' ')" = 1 ] || die "$vm exists in several zones ($found): refusing to continue"
      log "create of $vm in $z reported failure but the VM exists in $found: adopting it, no other zone tried"
      echo "$found" > "$RUN/zone.$vm"; created=$found; break
    fi
  done
  [ -n "$created" ] || die "no zone of '$zones' had $machine capacity for $vm"
  log "created $vm in $created"
done

push_and_run() { # name
  local vm=$1 z i st
  z=$(cat "$RUN/zone.$vm")
  SSH() { gcloud compute ssh "$vm" --zone "$z" --project $PROJECT --command "$1" 2>>"$RUN/ssh.$vm.err"; }
  # READINESS IS BOUNDED: 40 x 30 s. An unbounded loop here is how a launcher hangs for ever.
  for i in $(seq 1 40); do
    SSH 'test -f /var/snap-ready && echo ready' | grep -q ready && break
    sleep 30
  done
  SSH 'test -f /var/snap-ready && echo ready' | grep -q ready || { echo "never-ready" > "$RUN/outcome.$vm"; return 1; }
  SSH 'mkdir -p ~/snap' >/dev/null 2>&1
  gcloud compute scp --recurse "$RUN/code" "$RUN/splits" "$RUN/tools" "$RUN/run.env" "$RUN/vm_plan.json" \
    "$vm:~/snap/" --zone "$z" --project $PROJECT --quiet >> "$RUN/launch.log" 2>&1 \
    || { echo "scp-failed" > "$RUN/outcome.$vm"; return 1; }
  # the key, from Secret Manager, with its CONTENT verified ON the VM
  { printf 'OPENAI_API_KEY='; gcloud secrets versions access latest --secret "$SECRET" --project $PROJECT; echo; } \
    | SSH 'umask 077; cat > ~/snap/secret.env; grep -qE "^OPENAI_API_KEY=.{20,}$" ~/snap/secret.env && echo key-ok' \
    | grep -q key-ok || { echo "key-not-written" > "$RUN/outcome.$vm"; return 1; }
  SSH "export VM_NAME=$vm; nohup setsid bash -c 'while :; do gcloud storage rsync -r -q ~/snap/out $SYNC/out >/dev/null 2>&1; sleep 600; done' >/dev/null 2>&1 < /dev/null & echo synced" >/dev/null 2>&1
  SSH "cd ~/snap; VM_NAME=$vm nohup setsid bash tools/vm_run.sh > vm_run.$vm.log 2>&1 < /dev/null & echo started" \
    | grep -q started || { echo "driver-did-not-start" > "$RUN/outcome.$vm"; return 1; }
  # poll for this VM's own VMDONE, bounded by the reaper
  for i in $(seq 1 240); do
    sleep 60
    st=$(SSH "cd ~/snap/out 2>/dev/null && { ls $vm.VMDONE 2>/dev/null; cat $vm.ABORTED 2>/dev/null; }" | tr '\n' ' ')
    gcloud compute instances describe "$vm" --zone "$z" --project $PROJECT --format='value(status)' >/dev/null 2>&1 \
      || { echo "vm-vanished" > "$RUN/outcome.$vm"; return 1; }
    case "$st" in *VMDONE*) break;; esac
  done
  SSH "test -f ~/snap/out/$vm.VMDONE && echo done" | grep -q done \
    || { echo "no-VMDONE" > "$RUN/outcome.$vm"; return 1; }
  if SSH "test -f ~/snap/out/$vm.ABORTED && echo aborted" | grep -q aborted; then
    echo "aborted" > "$RUN/outcome.$vm"; return 1
  fi
  SSH "cd ~/snap && gcloud storage rsync -r -q out $SYNC/out && gcloud storage rsync -r -q ledger $SYNC/ledger" \
    >/dev/null 2>&1 || { echo "final-sync-failed" > "$RUN/outcome.$vm"; return 1; }
  echo "ok" > "$RUN/outcome.$vm"
}

log "running $(wc -l < "$RUN/vms.txt" | tr -d ' ') VMs"
for vm in $(cat "$RUN/vms.txt"); do ( push_and_run "$vm" </dev/null; echo $? > "$RUN/rc.$vm" ) & done
wait
# EVERY VM's outcome is READ. A staging abort on one VM leaves its groups short, and that used to log DONE.
for vm in $(cat "$RUN/vms.txt"); do
  rc=$(cat "$RUN/rc.$vm" 2>/dev/null || echo missing)
  outcome=$(cat "$RUN/outcome.$vm" 2>/dev/null || echo missing)
  log "VM $vm: rc=$rc outcome=$outcome"
  [ "$rc" = 0 ] && [ "$outcome" = ok ] || fail "$vm did not finish cleanly (rc=$rc outcome=$outcome)"
done

# ---------------------------------------------------------------- 7. collect, regroup, report on ONE VM
FIRST=$(head -1 "$RUN/vms.txt"); FZ=$(cat "$RUN/zone.$FIRST")
# ONE remote script, not a chained --command string: every step reports its own status into collect.log, so a
# failure here names the step instead of being "the collect/post step failed" with nothing under it (pilot 3).
gcloud compute ssh "$FIRST" --zone "$FZ" --project $PROJECT --command 'bash ~/snap/tools/collect.sh' </dev/null \
  > "$RUN/collect.log" 2>&1
COLLECT_RC=$?
grep '^COLLECT ' "$RUN/collect.log" | while read -r line; do log "$line"; done
[ "$COLLECT_RC" = 0 ] || fail "the collect/post step on $FIRST exited $COLLECT_RC (see collect.log)"
mkdir -p "$RUN/out"
gcloud storage rsync -r -q "$SYNC/out" "$RUN/out" >> "$RUN/launch.log" 2>&1 || fail "could not pull $SYNC/out"

# ---------------------------------------------------------------- 8. read every exit code that matters
POST_RC=$(cat "$RUN/out/post.rc" 2>/dev/null || echo missing)
[ "$POST_RC" = 0 ] || fail "post.py rc=$POST_RC (regroup or report refused; see out/post.log and out/reports/)"
[ -f "$RUN/out/ALLDONE" ] || fail "no ALLDONE marker"
UNIT_BAD=$(cat "$RUN/out"/*.rc 2>/dev/null | grep -vc '^0$' || true)
[ "${UNIT_BAD:-0}" = 0 ] || fail "$UNIT_BAD shard unit(s) exited non-zero (out/*.rc)"
ABORTS=$(ls "$RUN/out"/*.ABORTED 2>/dev/null | wc -l | tr -d ' ')
[ "$ABORTS" = 0 ] || fail "$ABORTS VM(s) aborted staging (out/*.ABORTED)"
log "collected into $RUN/out; post.rc=$POST_RC; failures=$FAILURES"
if [ "$FAILURES" != 0 ]; then
  log "FINISHED WITH $FAILURES FAILURE(S): the wave is NOT a measurement. partial-sync: $SYNC"
  exit 1
fi
log "DONE, every VM and every unit clean. partial-sync prefix: $SYNC"
