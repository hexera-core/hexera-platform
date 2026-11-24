#!/bin/bash
# ONE command for a CRS v4 dev snapshot: resolve the commit, check everything that needs no VM and no model, create
# ONE VM (server-side reaper), stage inputs from the bucket (sha256-checked), run all four arms on the dev and clean
# splits, collect, DELETE the VM, write docs/benchmarks/crs-v4-dev-snapshot-<label>.md.
#
#   bash .superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/launch.sh <commit> [v1r|v2] [--dry-run]
#
# <commit>  any ref; resolved to a full SHA and recorded. It must contain the profile's WaveDefs (prep refuses).
# profile   profiles/<name>.env (default v1r). DEV_SPLIT=/path CLEAN_SPLIT=/path override the profile's split specs.
# --dry-run everything up to (not including) the VM: commit, splits, waves, inputs present, shard plans, caps,
#           quota, image digest, projection. No VM, no model, no bucket writes.
# KEEP_VM=1 skips the VM deletion (debug only; the 'max-run-duration' reaper still deletes it).
set -u -o pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(git -C "$HERE" rev-parse --show-toplevel)
COMMIT_ARG=${1:?usage: launch.sh <commit> [profile] [--dry-run]}; PROFILE=v1r; DRY=0
for x in "${@:2}"; do case $x in --dry-run) DRY=1;; *) PROFILE=$x;; esac; done
KEYS="MACHINE VM_USD_PER_H DEV_SHARDS CLEAN_SHARDS TOTAL_USD MAX_RUN IMAGE IMAGE_COMMIT OPS"   # the environment overrides the profile
for k in $KEYS; do eval "OVR_$k=\${$k:-}"; done
source "$HERE/profiles/$PROFILE.env"
for k in $KEYS; do eval "v=\$OVR_$k"; [ -n "$v" ] && eval "$k=\$v"; done
PROJECT=hexera-dev; BUCKET=dev-crsv3-transfer-224734058693
TS=$(date -u +%Y%m%d%H%M); RUN=${SNAP_RUNS:-$HOME/archductor/snap-runs}/$LABEL-$TS
VM=dev-crsv4-snap-$LABEL; ART=$HERE/runs/$LABEL-$TS; DOC=$REPO/docs/benchmarks/crs-v4-dev-snapshot-$LABEL.md
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$RUN/launch.log"; }
die() { log "STOP: $*" >&2; exit 1; }
mkdir -p "$RUN/splits" "$RUN/tools"

# ---------------------------------------------------------------- 1. commit
COMMIT=$(git -C "$REPO" rev-parse --verify "$COMMIT_ARG^{commit}") || die "cannot resolve $COMMIT_ARG"
log "profile=$PROFILE commit=$COMMIT (from $COMMIT_ARG) run dir $RUN"
git -C "$REPO" archive --format=tar "$COMMIT" > "$RUN/code.tar" || die "git archive failed"
mkdir -p "$RUN/code" && tar xf "$RUN/code.tar" -C "$RUN/code" devtools src || die "extract failed"

# ---------------------------------------------------------------- 2. splits (a git ref:path, or a file)
resolve_split() { # spec override out   (a spec "COMMIT:<path>" is read from the run commit itself)
  local spec=$1 override=$2 out=$3
  case $spec in COMMIT:*) spec="$COMMIT:${spec#COMMIT:}";; esac
  if [ -n "$override" ]; then [ -f "$override" ] || die "split file $override not found"; cp "$override" "$out"; echo "file:$override"; return; fi
  if git -C "$REPO" cat-file -e "$spec" 2>/dev/null; then git -C "$REPO" show "$spec" > "$out"; echo "git:$spec@$(git -C "$REPO" rev-parse "${spec%%:*}")"; return; fi
  die "split $spec is not written yet (pass DEV_SPLIT=/path or CLEAN_SPLIT=/path, or wait for the re-probe)"
}
DEV_SRC=$(resolve_split "$DEV_SPLIT_SPEC" "${DEV_SPLIT:-}" "$RUN/splits/dev.json") || exit 1
CLEAN_SRC=$(resolve_split "$CLEAN_SPLIT_SPEC" "${CLEAN_SPLIT:-}" "$RUN/splits/clean.json") || exit 1
log "dev split $DEV_SRC; clean split $CLEAN_SRC"

# ---------------------------------------------------------------- 2b. the op budget: the profile's OPS, else the commit's own
DEFAULT_OPS=$(cd "$RUN/code/devtools/validation/v4_eval" && python3 -I -c "import sys; sys.path.insert(0,'.'); import evalspec; print(evalspec.SHARED_BUDGET.ops)") \
  || die "cannot read evalspec.SHARED_BUDGET.ops from $COMMIT"
if [ -n "${OPS:-}" ]; then OPS_SOURCE="profile/env (the commit's default is $DEFAULT_OPS)"; else OPS=$DEFAULT_OPS; OPS_SOURCE="the commit's evalspec.SHARED_BUDGET.ops"; fi
[ "$OPS" -le "$DEFAULT_OPS" ] || die "--ops $OPS is above the commit's Budget.ops $DEFAULT_OPS: run_arm only LOWERS the budget"
log "op budget: --ops $OPS applied ops ($OPS_SOURCE)"
# ---------------------------------------------------------------- 3. inputs present, plans, caps (prep.py)
gcloud storage ls -l "gs://$BUCKET/eval/v4-reprobe/inputs/**" > "$RUN/inputs_ls.txt" 2>"$RUN/inputs_ls.err" || die "cannot list inputs"
python3 -I "$HERE/prep.py" --code "$RUN/code" --profile "$PROFILE" --dev-split "$RUN/splits/dev.json" \
  --clean-split "$RUN/splits/clean.json" --dev-wave "$DEV_WAVE" --clean-wave "$CLEAN_WAVE" --dev-shards "$DEV_SHARDS" \
  --clean-shards "$CLEAN_SHARDS" --total-usd "$TOTAL_USD" --file-usd-cap "$FILE_USD_CAP" --inputs-ls "$RUN/inputs_ls.txt" \
  --prior-rows "$REPO/$PRIOR_ROWS" --vm-usd-per-h "$VM_USD_PER_H" --ops "$OPS" --prior-ops "${PRIOR_OPS:-24}" \
  --max-run "$MAX_RUN" --out "$RUN/splits" | tee -a "$RUN/launch.log"
[ "${PIPESTATUS[0]}" = 0 ] || die "prep refused"
cp "$RUN/splits/launch_manifest.json" "$RUN/"
DEV_N=$(python3 -I -c "import json;print(json.load(open('$RUN/splits/launch_manifest.json'))['waves']['dev']['n_shards'])")
CLEAN_N=$(python3 -I -c "import json;print(json.load(open('$RUN/splits/launch_manifest.json'))['waves']['clean']['n_shards'])")

# ---------------------------------------------------------------- 4. quota, name, image
VCPU=${MACHINE##*-}
FREE=$(gcloud compute regions describe us-central1 --project $PROJECT --format=json | python3 -I -c "
import json,sys; q={x['metric']:x for x in json.load(sys.stdin)['quotas']}['C2D_CPUS']; print(int(q['limit']-q['usage']))")
log "C2D_CPUS free in us-central1: $FREE, need $VCPU"
[ "$FREE" -ge "$VCPU" ] || die "C2D quota: $FREE free < $VCPU"
gcloud compute instances list --project $PROJECT --filter="name=$VM" --format='value(name)' | grep -q . && die "a VM named $VM already exists (not mine to touch)"
# the image is pinned by digest and its org.opencontainers.image.revision label must equal IMAGE_COMMIT (the commit
# its mesh build came from; the RUN commit is bind-mounted separately and may differ)
IMAGE_REF=${IMAGE:?set IMAGE (profile or env) to a digest-pinned image}
IMAGE_COMMIT=${IMAGE_COMMIT:?set IMAGE_COMMIT to the full sha the image label must carry}
LABEL_REV=$(python3 -I "$HERE/image_label.py" "$IMAGE_REF" --token "$(gcloud auth print-access-token)" --expect "$IMAGE_COMMIT") \
  || die "image label check refused: $IMAGE_REF label '$LABEL_REV', expected $IMAGE_COMMIT"
log "image $IMAGE_REF, label org.opencontainers.image.revision=$LABEL_REV (== IMAGE_COMMIT)"

WAVE_ID_DEV=snap-$LABEL${WAVE_TAG:+-$WAVE_TAG}-dev-$TS; WAVE_ID_CLEAN=snap-$LABEL${WAVE_TAG:+-$WAVE_TAG}-clean-$TS
cat > "$RUN/run.env" <<EOF
LABEL=$LABEL
COMMIT=$COMMIT
COMMIT_ARG=$COMMIT_ARG
IMAGE_REF=$IMAGE_REF
IMAGE_COMMIT=$IMAGE_COMMIT
MACHINE=$MACHINE
OPS=$OPS
OPS_SOURCE="$OPS_SOURCE"
FILE_USD_CAP=$FILE_USD_CAP
ROUTE=$ROUTE
BUCKET=$BUCKET
CACHE=gs://$BUCKET/eval/snap-$LABEL-$TS/verdict-cache
DEV_WAVE=$DEV_WAVE
CLEAN_WAVE=$CLEAN_WAVE
WAVE_ID_DEV=$WAVE_ID_DEV
WAVE_ID_CLEAN=$WAVE_ID_CLEAN
DEV_N=$DEV_N
CLEAN_N=$CLEAN_N
CAPS_DEV="$(cat "$RUN/splits/caps_dev.txt")"
CAPS_CLEAN="$(cat "$RUN/splits/caps_clean.txt")"
DEV_SRC=$DEV_SRC
CLEAN_SRC=$CLEAN_SRC
EOF
cp "$HERE/vm_run.sh" "$HERE/post.py" "$RUN/tools/"
if [ "$DRY" = 1 ]; then log "DRY RUN complete: no VM created, no model called. run.env:"; cat "$RUN/run.env" | tee -a "$RUN/launch.log"; exit 0; fi

# ---------------------------------------------------------------- 5. the VM (always deleted on the way out)
CREATED=""
cleanup() {
  local rc=$?
  if [ -n "$CREATED" ] && [ "${KEEP_VM:-0}" != 1 ]; then
    gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command 'shred -u ~/snap/secret.env 2>/dev/null; true' >/dev/null 2>&1
    gcloud compute instances delete $VM --zone $ZONE --project $PROJECT --quiet >> "$RUN/launch.log" 2>&1
    echo "{\"name\":\"$VM\",\"zone\":\"$ZONE\",\"max_run\":\"$MAX_RUN\",\"created\":\"$CREATED\",\"deleted\":\"$(date -u +%FT%TZ)\",\"delete_rc\":$?}" > "$RUN/vm_deleted.json"
    log "VM $VM delete requested (rc in vm_deleted.json); listing: $(gcloud compute instances list --project $PROJECT --filter="name=$VM" --format='value(name,status)' 2>&1 | tr '\n' ' ')"
  fi
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
for ZONE in us-central1-b us-central1-a us-central1-c us-central1-f; do
  gcloud compute instances create $VM --project $PROJECT --zone $ZONE --machine-type $MACHINE \
    --provisioning-model=STANDARD --max-run-duration=$MAX_RUN --instance-termination-action=DELETE \
    --labels deployment-id=dev-crsv4,purpose=v4-snapshot,owner=lead-2,snapshot=$LABEL \
    --image-family debian-12 --image-project debian-cloud --boot-disk-size 100GB --boot-disk-type pd-balanced \
    --scopes cloud-platform --metadata-from-file startup-script="$RUN/startup.sh" >> "$RUN/launch.log" 2>&1 \
    && { CREATED=$(date -u +%FT%TZ); break; }
done
[ -n "$CREATED" ] || { ZONE=""; die "no zone had $MACHINE capacity"; }
log "VM $VM up in $ZONE at $CREATED (reaper: max-run $MAX_RUN, DELETE)"
SSH() { gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command "$1" 2>>"$RUN/ssh.err"; }
for i in $(seq 1 40); do SSH 'test -f /var/snap-ready && echo ready' | grep -q ready && break; sleep 30; done
SSH 'test -f /var/snap-ready' || die "VM never got docker + the image"
SSH 'sudo docker inspect --format "{{.Id}} {{json .RepoDigests}}" meshpipeline-mesh:native' | tee "$RUN/image_on_vm.txt"

# ---------------------------------------------------------------- 6. payload, key, run
tar cf "$RUN/payload.tar" -C "$RUN" code.tar splits tools run.env
gcloud compute scp "$RUN/payload.tar" $VM:~/payload.tar --zone $ZONE --project $PROJECT >> "$RUN/launch.log" 2>&1 || die "scp failed"
SSH 'set -e; mkdir -p ~/snap/code; cd ~/snap; tar xf ~/payload.tar; tar xf code.tar -C code; chmod -R a+rX code splits tools' || die "unpack failed"
{ printf 'OPENAI_API_KEY='; gcloud secrets versions access latest --secret openai-api-key --project $PROJECT; echo; } \
  | SSH 'umask 077; cat > ~/snap/secret.env; test -s ~/snap/secret.env && echo key-ok' | grep -q key-ok || die "key not written"
SSH 'cd ~/snap; nohup setsid bash tools/vm_run.sh > driver.log 2>&1 < /dev/null & echo started' | grep -q started || die "driver did not start"
log "running; polling every 2 min for out/ALLDONE"
while true; do
  sleep 120
  st=$(SSH 'cd ~/snap/out 2>/dev/null && { cat STAGE_DONE 2>/dev/null; ls *.rc 2>/dev/null | wc -l; ls ALLDONE 2>/dev/null; cat ABORTED 2>/dev/null; }' | tr '\n' ' ')
  gcloud compute instances describe $VM --zone $ZONE --project $PROJECT --format='value(status)' >/dev/null 2>&1 || die "the VM is gone (reaper?) before ALLDONE"
  log "status: $st"
  echo "$st" | grep -q ALLDONE && break
done

# ---------------------------------------------------------------- 7. collect, delete
SSH 'cd ~/snap; shred -u secret.env 2>/dev/null; ls secret.env 2>/dev/null; tar czf /tmp/collect.tgz --exclude="*/work" out ledger driver.log; find out \( -name search-trace.json -o -name delivered.json -o -name delivered.brep \) > /tmp/traces.txt; tar czf /tmp/traces.tgz -T /tmp/traces.txt; date -u +%FT%TZ > out/COLLECTED' >> "$RUN/launch.log"
gcloud compute scp $VM:/tmp/collect.tgz "$RUN/collect.tgz" --zone $ZONE --project $PROJECT >> "$RUN/launch.log" 2>&1 || die "collect failed"
tar xzf "$RUN/collect.tgz" -C "$RUN" || die "collect archive unreadable"
gcloud compute scp $VM:/tmp/traces.tgz "$RUN/traces.tgz" --zone $ZONE --project $PROJECT >> "$RUN/launch.log" 2>&1 \
  && log "search traces: $(tar tzf "$RUN/traces.tgz" | wc -l | tr -d ' ') file(s) in $RUN/traces.tgz" || log "no search traces collected"
# Lane B's layout: traces/snap-<label>-<split>/<arm>/<sha12>.json (the arm and the input's sha256[:12] in the path)
if [ -s "$RUN/traces.tgz" ]; then
  mkdir -p "$RUN/traces-raw" && tar xzf "$RUN/traces.tgz" -C "$RUN/traces-raw"
  NT=$(python3 -I - "$RUN" "$LABEL" "$WAVE_ID_DEV" "$WAVE_ID_CLEAN" <<'PY'
import shutil, sys
from pathlib import Path
run, label, wid_dev, wid_clean = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
split_of = {wid_dev: "dev", wid_clean: "clean"}
n = 0
ARMS = ("input_only", "staged", "search_det", "search_ai")
for t in (run / "traces-raw").rglob("*"):
    if t.name not in ("search-trace.json", "delivered.json", "delivered.brep"):
        continue
    arm, sha12 = t.parent.name, t.parent.parent.name          # .../<wave_id>/shard-I-of-N/work/<sha12>/<arm>/
    wid = next((p for p in t.parts if p in split_of), None)
    if arm not in ARMS or wid is None or len(sha12) != 12:
        continue
    if t.name == "search-trace.json":
        dst = run / "traces" / f"snap-{label}-{split_of[wid]}" / arm / f"{sha12}.json"
    else:  # the delivered state: states/snap-<label>-<split>/<arm>/<sha12>.{json,brep}
        dst = run / "states" / f"snap-{label}-{split_of[wid]}" / arm / f"{sha12}{t.suffix}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(t, dst)
    n += 1
print(n)
PY
)
  [ -d "$RUN/traces" ] && gcloud storage cp -r -q "$RUN/traces/"* "gs://$BUCKET/eval/snap-$LABEL-$TS/traces/" >> "$RUN/launch.log" 2>&1
  [ -d "$RUN/states" ] && gcloud storage cp -r -q "$RUN/states/"* "gs://$BUCKET/eval/snap-$LABEL-$TS/states/" >> "$RUN/launch.log" 2>&1
  log "traces + delivered states: $NT files -> $RUN/{traces,states}/snap-$LABEL-<split>/<arm>/<sha12>.{json,brep}, uploaded to gs://$BUCKET/eval/snap-$LABEL-$TS/{traces,states}/ ($(du -sh "$RUN/states" 2>/dev/null | cut -f1) of states)"
fi
HOURS=$(python3 -I -c "from datetime import datetime as d,timezone as z;print(round((d.now(z.utc)-d.fromisoformat('$CREATED'.replace('Z','+00:00'))).total_seconds()/3600,2))")
echo "{\"name\":\"$VM\",\"zone\":\"$ZONE\",\"max_run\":\"$MAX_RUN\",\"created\":\"$CREATED\",\"deleted\":\"(see vm_deleted.json)\",\"hours\":$HOURS,\"usd\":$(python3 -c "print(round($HOURS*$VM_USD_PER_H,2))")}" > "$RUN/vm.json"
# delete now; clearing CREATED disarms the trap's delete (it stays armed until this line)
if [ "${KEEP_VM:-0}" != 1 ]; then
  CREATED=""
  gcloud compute instances delete $VM --zone $ZONE --project $PROJECT --quiet >> "$RUN/launch.log" 2>&1; drc=$?
  DELETED=$(date -u +%FT%TZ)
  python3 -I -c "import json;p='$RUN/vm.json';d=json.load(open(p));d['deleted']='$DELETED';d['delete_rc']=$drc;json.dump(d,open(p,'w'))"
  log "VM delete rc=$drc; list: [$(gcloud compute instances list --project $PROJECT --filter="name=$VM" --format='value(name,status)' 2>&1 | tr '\n' ' ')]"
fi

# an ABORTED run (input staging failed) has evidence but no measurement: no doc, no commit
if [ -f "$RUN/out/ABORTED" ]; then die "the run ABORTED on the VM: $(cat "$RUN/out/ABORTED"); evidence in $RUN/out (stage_errors.txt); VM deleted, no doc written"; fi
# ---------------------------------------------------------------- 8. the doc and the artifacts
python3 -I "$HERE/analyze.py" --run "$RUN" --doc "$DOC" ${NOTES_MD:+--notes "$NOTES_MD"} ${MOVED:+--moved "$MOVED"} \
  ${BASELINE_RUN:+--baseline-run "$BASELINE_RUN" --baseline-label "${BASELINE_LABEL:-baseline}"} | tee -a "$RUN/launch.log"
[ "${PIPESTATUS[0]}" = 0 ] && [ -s "$DOC" ] || die "analyze.py FAILED: no doc at $DOC (the collected run is intact in $RUN; rerun analyze by hand)"
[ -n "${NOTES_MD:-}" ] && cp "$NOTES_MD" "$RUN/notes.md"
mkdir -p "$ART"
( cd "$RUN" && find out -path '*/shard-*' \( -name rows.json -o -name EXCLUDED.json -o -name PROVENANCE.json \) -print0 \
  | xargs -0 -I{} sh -c 'mkdir -p "$1/$(dirname "$2")" && cp "$2" "$1/$2"' _ "$ART" {} )
cp -r "$RUN/ledger" "$RUN/out/reports" "$ART/" 2>/dev/null; mkdir -p "$ART/logs" "$ART/splits"
cp "$RUN"/out/*.rc "$RUN"/out/*.start "$RUN"/out/*.end "$RUN"/out/{LAUNCHED,RUN_DONE,ALLDONE,STAGE_DONE,stage_errors.txt,RETRIED} "$ART/logs/" 2>/dev/null
cp -r "$RUN/out/attempt1" "$ART/logs/" 2>/dev/null
for f in "$RUN"/out/*.log; do { head -40 "$f"; echo "... [trimmed $(wc -l < "$f") lines; errors then tail] ..."; grep -n -E 'Traceback|ERROR|REFUSED|file_timeout' "$f" | head -40; tail -40 "$f"; } > "$ART/logs/$(basename "$f")"; done
cp "$RUN"/{launch_manifest.json,run.env,vm.json,vm_deleted.json,launch.log,image_on_vm.txt,notes.md} "$ART/" 2>/dev/null
cp "$RUN"/splits/{dev.json,clean.json,plan_dev.json,plan_clean.json} "$ART/splits/"
if grep -rlE 'sk-[A-Za-z0-9_-]{20,}' "$ART" "$DOC"; then die "a key-shaped string reached the artifacts: not committing"; fi
BR=$(git -C "$REPO" branch --show-current)
case $BR in sdd/*) git -C "$REPO" add "$DOC" && git -C "$REPO" add -f "$ART" && \
  git -C "$REPO" commit -q -m "docs(snap-$LABEL): CRS v4 dev snapshot $LABEL at ${COMMIT:0:8} (PROVISIONAL)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && log "committed $(git -C "$REPO" rev-parse --short HEAD) on $BR";;
  *) log "not on an sdd/* branch ($BR): doc and artifacts written, NOT committed";; esac
log "DONE: $DOC"
