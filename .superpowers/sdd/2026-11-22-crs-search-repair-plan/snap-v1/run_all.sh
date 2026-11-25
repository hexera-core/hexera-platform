#!/bin/bash
# Dev snapshot v1 driver (runs ON the VM dev-crsv4-snap-v1). Adapted from task-49/run_all.sh.
# Each container is one shard; its exit code lands in out/<name>.rc. Sentinel: out/ALLDONE.
# All four arms in every shard; every shard is ledgered (search_ai), per-shard caps sum to $14.95 (<= $15).
set -u
T=$HOME/snap; IMG=meshpipeline-mesh:native
COMMIT=$(cat $T/COMMIT); IMAGE_ID=$(sudo docker inspect --format '{{.Id}}' $IMG)
B=dev-crsv3-transfer-224734058693; CACHE=gs://$B/eval/snap-v1/verdict-cache
SPL=devtools/validation/corpora/v4/provisional_sample_task43
PLAN=.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1/shard_plan_dev6.json
ROUTE=openai:gpt-6-astra
mkdir -p $T/out $T/ledger; chmod 777 $T/out $T/ledger
prov() { # wave wave_id n
  sudo docker run --rm -v $T/code:/w -w /w -e PYTHONPATH=/w/src $IMG python -c "
import sys,tempfile; sys.path.insert(0,'/w/devtools/validation/v4_eval')
import evalspec as e, json
from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore
m=FileMarkerStore(tempfile.mkdtemp())
print(json.dumps(e.plan_wave(e.wave('$1'),wave_id='$2',n_shards=$3,markers=m).provenance(),sort_keys=True))" 2>/dev/null
}
launch() { # name wave split splitfile wave_id n shard cap [run_arm extra...]
  local name=$1 wave=$2 split=$3 sfile=$4 wid=$5 n=$6 shard=$7 cap=$8; shift 8
  local P; P=$(prov $wave $wid $n)
  local out=$T/out/$wid/shard-${shard}-of-${n}; mkdir -p $out; chmod 777 $out
  ( date -u +%FT%TZ > $T/out/$name.start
    sudo docker run --rm --name snap-$name -v $T/code:/w:ro -v $T/inputs:/eval/inputs:ro -v $out:/eval/out \
      -v $T/ledger:/eval/ledger -w /w -e PYTHONPATH=/w/src --env-file $T/secret.env $IMG \
      python devtools/validation/v4_eval/run_arm.py \
      --mode arms --h-mm auto --ops 24 --workers 1 --cache-uri $CACHE --commit $COMMIT --image $IMAGE_ID \
      --machine-type c2d-standard-16 --wave $wave --split $split --split-file /w/$sfile \
      --shard ${shard}/${n} --inputs /eval/inputs --out /eval/out --plan-provenance "$P" \
      --route $ROUTE --wave-id $wid --wave-type normal --n-shards $n \
      --ledger-path /eval/ledger/$wid-s$shard.jsonl --marker-bucket $B \
      --model-usd-cap $cap --file-usd-cap 0.5 "$@" \
      > $T/out/$name.log 2>&1; echo $? > $T/out/$name.rc; date -u +%FT%TZ > $T/out/$name.end ) &
}
# dev: 6 shards cut by the cost-balanced plan (1/3/3/7/8/8 files); caps ~$0.483/file -> $14.45
CAPS=(x 0.50 1.45 1.45 3.35 3.85 3.85)
for i in 1 2 3 4 5 6; do
  launch dev$i snap_v1_dev dev $SPL/PROVISIONAL_dev.json snap-v1-dev 6 $i ${CAPS[$i]} --shard-plan /w/$PLAN
done
# clean control: 1 shard, 8 files, all clean at input -> $0.50
launch clean snap_v1_clean clean $SPL/PROVISIONAL_clean.json snap-v1-clean 1 1 0.50
wait
date -u +%FT%TZ > $T/out/ALLDONE
