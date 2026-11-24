#!/bin/bash
# Snapshot v4 runner, ON one VM of several. Reads ~/snap/run.env and ~/snap/vm_plan.json and runs ONLY the
# (group, shard) units that plan gives THIS VM (by $VM_NAME), each as its own container, all concurrently.
#
# One shard set per ARM GROUP, so a shard runs one arm over its files instead of four in sequence (lane B14). Each
# group has its own wave id, its own --n-shards and its own shard plan; regroup.py presents them as one wave per
# split afterwards, which is what report.py reads.
#
# OPS may be EMPTY, which means "the commit's own Budget.ops": --ops is then not passed at all. Passing an empty
# `--ops ` made argparse exit 2 on every unit, and the pilot caught it.
# Writes out/<wave_id>/shard-<i>-of-<n>/ per unit, out/<vm>.<unit>.rc per unit, and out/<vm>.VMDONE at the end.
# Every VM writes into the SAME bucket prefix, so launch.sh collects one tree.
set -u
T=$HOME/snap; source $T/run.env
IMG=meshpipeline-mesh:native
IMAGE_ID=$(sudo docker inspect --format '{{.Id}}' $IMG)
mkdir -p $T/out $T/ledger $T/inputs; chmod 777 $T/out $T/ledger
date -u +%FT%TZ > $T/out/$VM_NAME.STAGE_START

# --- inputs: <sha256>/<basename>, every byte checked against the split's sha256 (v1r's staging, unchanged)
cut -d' ' -f1,2 $T/splits/inputs.txt | xargs -P 16 -n 2 sh -c 'mkdir -p "$0/inputs/$1" && gcloud storage cp -q "$2" "$0/inputs/$1/" >/dev/null 2>&1 || echo "$1 copy-failed"' $T > $T/out/$VM_NAME.stage_errors.txt
for attempt in 1 2 3; do
  failed=$(grep copy-failed $T/out/$VM_NAME.stage_errors.txt 2>/dev/null | cut -d' ' -f1 | sort -u)
  [ -z "$failed" ] && break
  : > $T/out/$VM_NAME.stage_errors.txt
  for sha in $failed; do url=$(grep "^$sha " $T/splits/inputs.txt | cut -d' ' -f2)
    sleep 2; gcloud storage cp -q "$url" "$T/inputs/$sha/" >/dev/null 2>&1 || echo "$sha copy-failed" >> $T/out/$VM_NAME.stage_errors.txt
  done
done
bad=0; n=0
while read -r sha url; do
  n=$((n+1)); f="$T/inputs/$sha/$(basename "$url")"
  got=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1); [ "$got" = "$sha" ] || { echo "$sha mismatch-or-missing" >> $T/out/$VM_NAME.stage_errors.txt; bad=$((bad+1)); }
done < $T/splits/inputs.txt
chmod -R a+rX $T/inputs
echo "staged=$n bad=$bad" > $T/out/$VM_NAME.STAGE_DONE
if [ "$bad" != 0 ]; then echo "input staging failed: $bad of $n" > $T/out/$VM_NAME.ABORTED; date -u +%FT%TZ > $T/out/$VM_NAME.VMDONE; exit 1; fi

prov() { # wave wave_id n
  sudo docker run --rm -v $T/code:/w:ro -w /w -e PYTHONPATH=/w/src $IMG python -c "
import sys,tempfile; sys.path.insert(0,'/w/devtools/validation/v4_eval')
import evalspec as e, json
from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore
m=FileMarkerStore(tempfile.mkdtemp())
print(json.dumps(e.plan_wave(e.wave('$1'),wave_id='$2',n_shards=$3,markers=m).provenance(),sort_keys=True))" 2>/dev/null
}

run_unit() { # group shard n split wave arms cap
  local group=$1 shard=$2 n=$3 split=$4 wave=$5 arms=$6 cap=$7
  local wid="snap-$LABEL-$group-$TS"
  local name="$VM_NAME.$group-$shard"
  local P; P=$(prov $wave $wid $n)
  local out=$T/out/$wid/shard-${shard}-of-${n}; mkdir -p $out; chmod 777 $out
  date -u +%FT%TZ > $T/out/$name.start
  sudo docker run --rm --name snap-$group-$shard -v $T/code:/w:ro -v $T/splits:/eval/splits:ro \
    -v $T/inputs:/eval/inputs:ro -v $out:/eval/out -v $T/ledger:/eval/ledger -w /w -e PYTHONPATH=/w/src \
    --env-file $T/secret.env $IMG \
    python devtools/validation/v4_eval/run_arm.py \
    --mode arms --arms "$arms" --h-mm auto ${OPS:+--ops $OPS} --workers 1 --cache-uri $CACHE --commit $COMMIT \
    --image $IMAGE_ID --machine-type $MACHINE --wave $wave --split $split \
    --split-file /eval/splits/$split.json --shard-plan /eval/splits/plan_$group.json --shard ${shard}/${n} \
    --inputs /eval/inputs --out /eval/out --plan-provenance "$P" --route $ROUTE --wave-id $wid \
    --wave-type normal --n-shards $n --ledger-path /eval/ledger/$wid-s$shard.jsonl --marker-bucket $BUCKET \
    --model-usd-cap $cap --file-usd-cap $FILE_USD_CAP ${FILE_TIMEOUT_S:+--file-timeout-s $FILE_TIMEOUT_S} \
    ${SEARCH_WALL_S:+--search-wall-s $SEARCH_WALL_S} > $T/out/$name.log 2>&1
  echo $? > $T/out/$name.rc; date -u +%FT%TZ > $T/out/$name.end
}

unit_with_retry() { # same args: one retry when the shard refused BEFORE any file (exit 6: no key, route probe)
  local group=$1 shard=$2
  run_unit "$@"
  if [ "$(cat $T/out/$VM_NAME.$group-$shard.rc)" = 6 ]; then
    mkdir -p $T/out/attempt1
    for x in log rc start end; do cp $T/out/$VM_NAME.$group-$shard.$x $T/out/attempt1/ 2>/dev/null; done
    rm -f $T/out/snap-$LABEL-$group-$TS/shard-$shard-of-$3/PROVENANCE.json
    echo "$VM_NAME.$group-$shard" >> $T/out/RETRIED
    run_unit "$@"
  fi
}

date -u +%FT%TZ > $T/out/$VM_NAME.LAUNCHED
# PACKING PILOT evidence (owner directive): every 15 s, load per core, memory and running containers; at the end the
# kernel's OOM kills and every unit that exited 137 (SIGKILL: the OOM killer's signature for a docker run)
( printf 'utc\tnproc\tload1\tload_per_core\tmem_used_mb\tmem_avail_mb\tcontainers\n'
  while [ ! -f $T/out/$VM_NAME.RUN_DONE ]; do
    read l1 _ < /proc/loadavg; np=$(nproc); set -- $(free -m | awk '/^Mem:/{print $3, $7}')
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%H:%M:%S)" "$np" "$l1" "$(awk -v a=$l1 -v b=$np 'BEGIN{printf "%.2f", a/b}')" "$1" "$2" "$(sudo docker ps -q | wc -l)"
    sleep 15
  done ) > $T/out/$VM_NAME.sysmon.tsv 2>/dev/null &
# the units for THIS VM, one line each: group shard n split wave arms cap
python3 -I -c "
import json,sys
plan=json.load(open('$T/vm_plan.json'))
vm=next((v for v in plan['vms'] if v['name']=='$VM_NAME'), None)
if vm is None: sys.exit('no units for $VM_NAME in vm_plan.json')
for u in vm['units']:
    print(u['group'], u['shard'], u['n'], u['split'], u['wave'], ','.join(u['arms']), f\"{u['cap_usd']:.2f}\")
" > $T/units.txt || { echo "no units" > $T/out/$VM_NAME.ABORTED; date -u +%FT%TZ > $T/out/$VM_NAME.VMDONE; exit 1; }
while read -r group shard n split wave arms cap; do
  unit_with_retry "$group" "$shard" "$n" "$split" "$wave" "$arms" "$cap" &
done < $T/units.txt
wait
date -u +%FT%TZ > $T/out/$VM_NAME.RUN_DONE
sudo dmesg -T 2>/dev/null | grep -i -E 'out of memory|oom-kill|killed process' > $T/out/$VM_NAME.oom.txt
grep -l '^137$' $T/out/$VM_NAME.*.rc 2>/dev/null > $T/out/$VM_NAME.rc137.txt
echo "oom_kernel_lines=$(wc -l < $T/out/$VM_NAME.oom.txt) units_exit_137=$(wc -l < $T/out/$VM_NAME.rc137.txt)" > $T/out/$VM_NAME.pack_summary
shred -u $T/secret.env 2>/dev/null; ls $T/secret.env >/dev/null 2>&1 && echo present > $T/out/$VM_NAME.SECRET_STILL_THERE
date -u +%FT%TZ > $T/out/$VM_NAME.VMDONE
