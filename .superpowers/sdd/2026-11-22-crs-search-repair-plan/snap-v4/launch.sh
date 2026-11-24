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
KEYS="REGIONS TARGET_MIN TOTAL_USD MAX_RUN IMAGE IMAGE_COMMIT OPS"
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
git -C "$REPO" archive --format=tar "$COMMIT" > "$RUN/code.tar" || die "git archive failed"
mkdir -p "$RUN/code" && tar xf "$RUN/code.tar" -C "$RUN/code" devtools src || die "extract failed"

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
  if [ -n "$override" ]; then [ -f "$override" ] || die "split file $override not found"; cp "$override" "$out"; echo "file:$override"; return; fi
  if git -C "$REPO" cat-file -e "$spec" 2>/dev/null; then git -C "$REPO" show "$spec" > "$out"; echo "git:$spec@$(git -C "$REPO" rev-parse "${spec%%:*}")"; return; fi
  die "split $spec is not written yet (pass DEV_SPLIT=/path or CLEAN_SPLIT=/path)"
}
DEV_SRC=$(resolve_split "$DEV_SPLIT_SPEC" "${DEV_SPLIT:-}" "$RUN/splits/dev.json") || exit 1
CLEAN_SRC=$(resolve_split "$CLEAN_SPLIT_SPEC" "${CLEAN_SPLIT:-}" "$RUN/splits/clean.json") || exit 1
log "dev split $DEV_SRC; clean split $CLEAN_SRC"

# ---------------------------------------------------------------- 4. inputs, groups, VM plan, caps (prep.py)
gcloud storage ls -l "gs://$BUCKET/eval/v4-reprobe/inputs/**" > "$RUN/inputs_ls.txt" 2>"$RUN/inputs_ls.err" || die "cannot list inputs"
[ -s "$RUN/inputs_ls.txt" ] || die "the inputs listing is empty"
PRIOR="$REPO/$PRIOR_ROWS"
[ -d "$PRIOR" ] || die "PRIOR_ROWS $PRIOR does not exist: the shard plan is sized on a prior wave's MEASURED walls"
python3 -I "$HERE/prep.py" --code "$RUN/code" --profile "$PROFILE" --dev-split "$RUN/splits/dev.json" \
  --clean-split "$RUN/splits/clean.json" --dev-wave "$DEV_WAVE" --clean-wave "$CLEAN_WAVE" \
  --total-usd "$TOTAL_USD" --file-usd-cap "$FILE_USD_CAP" --inputs-ls "$RUN/inputs_ls.txt" \
  --prior-rows "$PRIOR" --regions $REGIONS --target-min "$TARGET_MIN" --route "$ROUTE" \
  --label "$LABEL" --ts "$TS" ${OPS:+--ops "$OPS"} --out "$RUN/splits" | tee -a "$RUN/launch.log"
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
  gcloud compute regions describe "$REGION" --project $PROJECT --format=json > "$RUN/quota_$REGION.json" \
    || die "cannot read $REGION quota"
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
MACHINE=multi
OPS=$OPS
FILE_USD_CAP=$FILE_USD_CAP
ROUTE=$ROUTE
BUCKET=$BUCKET
CACHE=gs://$BUCKET/eval/snap-$LABEL-$TS/verdict-cache
SYNC=$SYNC
EOF
cp "$HERE/vm_run.sh" "$HERE/post.py" "$HERE/regroup.py" "$RUN/tools/"
python3 -I -c "
import json,sys
print('\n'.join(v['name'] for v in json.load(open(sys.argv[1]))['vms']))
" "$RUN/vm_plan.json" > "$RUN/vms.txt" || die "cannot list the plan's VMs"
log "VMs: $(tr '\n' ' ' < "$RUN/vms.txt")"
if [ "$DRY" = 1 ]; then
  log "DRY RUN complete: no VM created, no model called; the key was length-checked through a pipe, never stored."
  cat "$RUN/run.env" | tee -a "$RUN/launch.log"
  exit 0
fi

# ---------------------------------------------------------------- 6. the VMs (always deleted on the way out)
cleanup() {
  local rc=$?
  while read -r vm; do
    [ "${KEEP_VM:-0}" = 1 ] && continue
    [ -f "$RUN/zone.$vm" ] || continue
    z=$(cat "$RUN/zone.$vm")
    gcloud compute ssh "$vm" --zone "$z" --project $PROJECT --command 'shred -u ~/snap/secret.env 2>/dev/null; true' >/dev/null 2>&1
    gcloud compute instances delete "$vm" --zone "$z" --project $PROJECT --quiet >> "$RUN/launch.log" 2>&1 &
  done < "$RUN/vms.txt"
  wait
  gcloud compute instances list --project $PROJECT --filter="labels.snapshot=$LABEL" \
    --format='value(name,status)' > "$RUN/vm_surviving.txt" 2>&1
  log "VM deletes requested; surviving: $(tr '\n' ' ' < "$RUN/vm_surviving.txt")"
  exit $rc
}
trap cleanup EXIT INT TERM

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

while read -r vm; do
  machine=$(vm_field "$vm" machine); zones=$(vm_field "$vm" zones); created=""
  for z in $zones; do
    if gcloud compute instances create "$vm" --project $PROJECT --zone "$z" --machine-type "$machine" \
        --provisioning-model=STANDARD --max-run-duration="$MAX_RUN" --instance-termination-action=DELETE \
        --labels deployment-id=dev-crsv4,purpose=v4-snapshot,owner=lane-b17,snapshot=$LABEL \
        --image-family debian-12 --image-project debian-cloud --boot-disk-size 200GB --boot-disk-type pd-balanced \
        --scopes cloud-platform --metadata-from-file startup-script="$RUN/startup.sh" >> "$RUN/launch.log" 2>&1; then
      echo "$z" > "$RUN/zone.$vm"; created=$z; break
    fi
  done
  [ -n "$created" ] || die "no zone of '$zones' had $machine capacity for $vm"
  log "created $vm in $created"
done < "$RUN/vms.txt"

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
while read -r vm; do ( push_and_run "$vm"; echo $? > "$RUN/rc.$vm" ) & done < "$RUN/vms.txt"
wait
# EVERY VM's outcome is READ. A staging abort on one VM leaves its groups short, and that used to log DONE.
while read -r vm; do
  rc=$(cat "$RUN/rc.$vm" 2>/dev/null || echo missing)
  outcome=$(cat "$RUN/outcome.$vm" 2>/dev/null || echo missing)
  log "VM $vm: rc=$rc outcome=$outcome"
  [ "$rc" = 0 ] && [ "$outcome" = ok ] || fail "$vm did not finish cleanly (rc=$rc outcome=$outcome)"
done < "$RUN/vms.txt"

# ---------------------------------------------------------------- 7. collect, regroup, report on ONE VM
FIRST=$(head -1 "$RUN/vms.txt"); FZ=$(cat "$RUN/zone.$FIRST")
gcloud compute ssh "$FIRST" --zone "$FZ" --project $PROJECT --command \
  "set -e; cd ~/snap; gcloud storage rsync -r -q $SYNC/out out; gcloud storage rsync -r -q $SYNC/ledger ledger || true; \
   sudo docker run --rm -v ~/snap/code:/w:ro -v ~/snap/tools:/tools:ro -v ~/snap/splits:/eval/splits:ro \
     -v ~/snap/out:/eval/out -w /w -e PYTHONPATH=/w/src meshpipeline-mesh:native \
     python /tools/post.py --out /eval/out --splits /eval/splits --manifest /eval/splits/launch_manifest.json \
     > ~/snap/out/post.log 2>&1; echo \$? > ~/snap/out/post.rc; \
   date -u +%FT%TZ > ~/snap/out/ALLDONE; gcloud storage rsync -r -q ~/snap/out $SYNC/out" \
  >> "$RUN/launch.log" 2>&1 || fail "the collect/post step failed on $FIRST"
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
