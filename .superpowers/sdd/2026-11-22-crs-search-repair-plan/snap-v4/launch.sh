#!/bin/bash
# ONE command for a CRS v4 snapshot on SEVERAL VMs in SEVERAL REGIONS (lane B14/B17). Resolves the commit, checks
# everything that needs no VM and no model, creates the VMs the plan asks for, stages inputs on each, runs that VM's
# own (arm group, shard) units, syncs partial rows every 10 min, regroups and reports, DELETES every VM.
#
#   bash snap-v4/launch.sh <commit> [v4] [--dry-run]
#
# This is NOT snap-v1r/launch.sh with flags: it is a separate launcher, so the single-VM one keeps running v3c
# untouched. What is new:
#   * PER-ARM GROUPS. One shard set per arm, each sized to its own measured work, because a shard that ran all four
#     arms in sequence made the slowest shard a sum of four (v2: 2.7 h; v1r: one shard 3.5 h behind). search_ai is
#     capped at the in-flight ceiling (37) by prep.py, which model_throttle.for_shard would otherwise refuse.
#   * MULTI-REGION. REGIONS is a profile variable, not a hardcoded us-central1 zone list. Every VM is c2d in every
#     region, so one benchmark's wall_s stays comparable.
#   * REGROUP. post.py presents the groups as one wave per split, so report.py's partition and arms gates apply
#     unchanged (see regroup.py).
# MEASURED cross-region cost (B17, one c2d VM per region): a verdict-cache get is 345 ms from us-east1 against
# 110 ms in us-central1 (exists + download, two round trips), input staging 71 MB in 6 s against 5 s, and the
# 4.34 GB image pulls in 50 s in BOTH regions. At ~35 gets per file that is ~8 s per file of ~1700 s of work.
set -u -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(git -C "$HERE" rev-parse --show-toplevel)
COMMIT_ARG=${1:?usage: launch.sh <commit> [profile] [--dry-run]}; PROFILE=v4; DRY=0
for x in "${@:2}"; do case $x in --dry-run) DRY=1;; *) PROFILE=$x;; esac; done
KEYS="REGIONS TARGET_MIN TOTAL_USD MAX_RUN IMAGE IMAGE_COMMIT OPS"
for k in $KEYS; do eval "OVR_$k=\${$k:-}"; done
source "$HERE/profiles/$PROFILE.env"
for k in $KEYS; do eval "v=\$OVR_$k"; [ -n "$v" ] && eval "$k=\$v"; done
PROJECT=hexera-dev; BUCKET=dev-crsv3-transfer-224734058693
TS=$(date -u +%Y%m%d%H%M); RUN=${SNAP_RUNS:-$HOME/archductor/snap-runs}/$LABEL-$TS
SYNC=gs://$BUCKET/eval/snap-$LABEL-$TS/partial-sync
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$RUN/launch.log"; }
die() { log "STOP: $*" >&2; exit 1; }
mkdir -p "$RUN/splits" "$RUN/tools"

# ---------------------------------------------------------------- 1. commit
COMMIT=$(git -C "$REPO" rev-parse --verify "$COMMIT_ARG^{commit}") || die "cannot resolve $COMMIT_ARG"
log "profile=$PROFILE commit=$COMMIT regions=$REGIONS run dir $RUN"
git -C "$REPO" archive --format=tar "$COMMIT" > "$RUN/code.tar" || die "git archive failed"
mkdir -p "$RUN/code" && tar xf "$RUN/code.tar" -C "$RUN/code" devtools src || die "extract failed"

# ---------------------------------------------------------------- 2. splits
resolve_split() { # spec override out
  local spec=$1 override=$2 out=$3
  if [ -n "$override" ]; then [ -f "$override" ] || die "split file $override not found"; cp "$override" "$out"; echo "file:$override"; return; fi
  if git -C "$REPO" cat-file -e "$spec" 2>/dev/null; then git -C "$REPO" show "$spec" > "$out"; echo "git:$spec@$(git -C "$REPO" rev-parse "${spec%%:*}")"; return; fi
  die "split $spec is not written yet (pass DEV_SPLIT=/path or CLEAN_SPLIT=/path)"
}
DEV_SRC=$(resolve_split "$DEV_SPLIT_SPEC" "${DEV_SPLIT:-}" "$RUN/splits/dev.json") || exit 1
CLEAN_SRC=$(resolve_split "$CLEAN_SPLIT_SPEC" "${CLEAN_SPLIT:-}" "$RUN/splits/clean.json") || exit 1
log "dev split $DEV_SRC; clean split $CLEAN_SRC"

# ---------------------------------------------------------------- 3. inputs, groups, VM plan, caps (prep.py)
gcloud storage ls -l "gs://$BUCKET/eval/v4-reprobe/inputs/**" > "$RUN/inputs_ls.txt" 2>"$RUN/inputs_ls.err" || die "cannot list inputs"
python3 -I "$HERE/prep.py" --code "$RUN/code" --profile "$PROFILE" --dev-split "$RUN/splits/dev.json" \
  --clean-split "$RUN/splits/clean.json" --dev-wave "$DEV_WAVE" --clean-wave "$CLEAN_WAVE" \
  --total-usd "$TOTAL_USD" --file-usd-cap "$FILE_USD_CAP" --inputs-ls "$RUN/inputs_ls.txt" \
  --prior-rows "$REPO/$PRIOR_ROWS" --regions $REGIONS --target-min "$TARGET_MIN" --route "$ROUTE" \
  --label "$LABEL" --ops "$OPS" --out "$RUN/splits" | tee -a "$RUN/launch.log"
[ "${PIPESTATUS[0]}" = 0 ] || die "prep refused"
cp "$RUN/splits/launch_manifest.json" "$RUN/splits/vm_plan.json" "$RUN/"

# ---------------------------------------------------------------- 4. quota, per region, against the plan
python3 -I - "$RUN/vm_plan.json" <<'PY' > "$RUN/quota_need.txt" || die "cannot read the VM plan"
import json,sys,collections
plan=json.load(open(sys.argv[1]))
need=collections.Counter()
for v in plan["vms"]: need[v["region"]] += int(v["vcpu"])
for r,n in sorted(need.items()): print(r,n)
PY
while read -r REGION NEED; do
  gcloud compute regions describe "$REGION" --project $PROJECT --format=json > "$RUN/quota_$REGION.json" || die "cannot read $REGION quota"
  python3 -I - "$RUN/quota_$REGION.json" "$NEED" "$REGION" <<'PY' | tee -a "$RUN/launch.log"
import json,sys
q={x["metric"]:x for x in json.load(open(sys.argv[1]))["quotas"]}
need=int(sys.argv[2]); region=sys.argv[3]
bad=[]
for m in ("C2D_CPUS","CPUS"):
    free=q[m]["limit"]-q[m]["usage"]
    print(f"{region} {m}: limit {q[m]['limit']:.0f} usage {q[m]['usage']:.0f} free {free:.0f}, need {need}")
    if free < need: bad.append(f"{m} free {free:.0f} < {need}")
if bad: sys.exit(f"QUOTA REFUSED in {region}: " + "; ".join(bad))
PY
  [ "${PIPESTATUS[0]}" = 0 ] || die "quota refused in $REGION"
done < "$RUN/quota_need.txt"

IMAGE_REF=${IMAGE:?set IMAGE to a digest-pinned image}
IMAGE_COMMIT=${IMAGE_COMMIT:?set IMAGE_COMMIT to the sha the image label must carry}
LABEL_REV=$(python3 -I "$HERE/../snap-v1r/image_label.py" "$IMAGE_REF" --token "$(gcloud auth print-access-token)" --expect "$IMAGE_COMMIT") \
  || die "image label check refused: $IMAGE_REF label '$LABEL_REV', expected $IMAGE_COMMIT"
log "image $IMAGE_REF label=$LABEL_REV"

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
mapfile -t VM_NAMES < <(python3 -I -c "import json,sys;print('\n'.join(v['name'] for v in json.load(open('$RUN/vm_plan.json'))['vms']))")
log "VMs: ${VM_NAMES[*]}"
if [ "$DRY" = 1 ]; then log "DRY RUN complete: no VM, no model."; cat "$RUN/run.env" | tee -a "$RUN/launch.log"; exit 0; fi

# ---------------------------------------------------------------- 5. the VMs (always deleted on the way out)
declare -A VM_ZONE=()
cleanup() {
  local rc=$?
  for vm in "${!VM_ZONE[@]}"; do
    [ "${KEEP_VM:-0}" = 1 ] && continue
    gcloud compute ssh "$vm" --zone "${VM_ZONE[$vm]}" --project $PROJECT --command 'shred -u ~/snap/secret.env 2>/dev/null; true' >/dev/null 2>&1
    gcloud compute instances delete "$vm" --zone "${VM_ZONE[$vm]}" --project $PROJECT --quiet >> "$RUN/launch.log" 2>&1 &
  done
  wait
  gcloud compute instances list --project $PROJECT --filter="name~dev-crsv4-snap-$LABEL" --format='value(name,status)' \
    > "$RUN/vm_surviving.txt" 2>&1
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

create_vm() { # name
  local vm=$1
  local machine zones
  machine=$(python3 -I -c "import json;print(next(v['machine'] for v in json.load(open('$RUN/vm_plan.json'))['vms'] if v['name']=='$vm'))")
  zones=$(python3 -I -c "import json;print(' '.join(next(v['zones'] for v in json.load(open('$RUN/vm_plan.json'))['vms'] if v['name']=='$vm')))")
  for z in $zones; do
    if gcloud compute instances create "$vm" --project $PROJECT --zone "$z" --machine-type "$machine" \
        --image-family debian-12 --image-project debian-cloud --boot-disk-size 200GB --boot-disk-type pd-balanced \
        --scopes cloud-platform --labels purpose=dev-crsv4,snapshot=$LABEL --max-run-duration="$MAX_RUN" \
        --instance-termination-action=DELETE --metadata-from-file startup-script="$RUN/startup.sh" \
        >> "$RUN/launch.log" 2>&1; then echo "$z"; return 0; fi
  done
  return 1
}
for vm in "${VM_NAMES[@]}"; do
  z=$(create_vm "$vm") || die "could not create $vm in any of its zones"
  VM_ZONE[$vm]=$z
  log "created $vm in $z"
done

push_and_run() { # name zone
  local vm=$1 z=$2
  until gcloud compute ssh "$vm" --zone "$z" --project $PROJECT --command 'test -f /var/snap-ready' >/dev/null 2>&1; do sleep 20; done
  gcloud compute ssh "$vm" --zone "$z" --project $PROJECT --command 'mkdir -p ~/snap/tools' >/dev/null 2>&1
  gcloud compute scp --recurse "$RUN/code" "$RUN/splits" "$RUN/tools" "$RUN/run.env" "$RUN/vm_plan.json" \
    "$vm:~/snap/" --zone "$z" --project $PROJECT --quiet >> "$RUN/launch.log" 2>&1 || return 1
  printf 'OPENAI_API_KEY=%s\n' "${OPENAI_API_KEY:-}" | gcloud compute ssh "$vm" --zone "$z" --project $PROJECT \
    --command 'cat > ~/snap/secret.env && chmod 600 ~/snap/secret.env' >/dev/null 2>&1
  gcloud compute ssh "$vm" --zone "$z" --project $PROJECT --command \
    "export VM_NAME=$vm; nohup bash -c 'while :; do gcloud storage rsync -r -q ~/snap/out $SYNC/out >/dev/null 2>&1; sleep 600; done' >/dev/null 2>&1 & \
     export VM_NAME=$vm; bash ~/snap/tools/vm_run.sh > ~/snap/vm_run.log 2>&1; \
     gcloud storage rsync -r -q ~/snap/out $SYNC/out >/dev/null 2>&1; \
     gcloud storage rsync -r -q ~/snap/ledger $SYNC/ledger >/dev/null 2>&1" >> "$RUN/launch.log" 2>&1
}
log "running ${#VM_NAMES[@]} VMs"
for vm in "${VM_NAMES[@]}"; do push_and_run "$vm" "${VM_ZONE[$vm]}" & done
wait
log "every VM reported RUN_DONE (or died); collecting"

# ---------------------------------------------------------------- 6. collect, regroup, report on ONE VM
FIRST=${VM_NAMES[0]}; FZ=${VM_ZONE[$FIRST]}
gcloud compute ssh "$FIRST" --zone "$FZ" --project $PROJECT --command \
  "gcloud storage rsync -r -q $SYNC/out ~/snap/out && gcloud storage rsync -r -q $SYNC/ledger ~/snap/ledger; \
   sudo docker run --rm -v ~/snap/code:/w:ro -v ~/snap/tools:/tools:ro -v ~/snap/splits:/eval/splits:ro \
     -v ~/snap/out:/eval/out -w /w -e PYTHONPATH=/w/src meshpipeline-mesh:native \
     python /tools/post.py --out /eval/out --splits /eval/splits --manifest /eval/splits/launch_manifest.json \
     > ~/snap/out/post.log 2>&1; echo \$? > ~/snap/out/post.rc; \
   date -u +%FT%TZ > ~/snap/out/ALLDONE; gcloud storage rsync -r -q ~/snap/out $SYNC/out" >> "$RUN/launch.log" 2>&1
mkdir -p "$RUN/out"
gcloud storage rsync -r -q "$SYNC/out" "$RUN/out" >> "$RUN/launch.log" 2>&1
log "collected into $RUN/out; post.rc=$(cat "$RUN/out/post.rc" 2>/dev/null || echo '?')"
log "DONE. partial-sync prefix: $SYNC"
