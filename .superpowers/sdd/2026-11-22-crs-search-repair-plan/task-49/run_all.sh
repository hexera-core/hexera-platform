#!/bin/bash
# Task 49 T2 smoke driver (runs ON the VM). Each container is one shard of a wave; its exit code lands in out/<name>.rc
set -u
T=$HOME/t49; IMG=meshpipeline-mesh:native
COMMIT=$(cat $T/COMMIT); IMAGE_ID=$(sudo docker inspect --format '{{.Id}}' $IMG)
B=dev-crsv3-transfer-224734058693; CACHE=gs://$B/eval/t49-smoke/verdict-cache
SPL=devtools/validation/corpora/v4/provisional_sample_task43
mkdir -p $T/out $T/ledger; chmod 777 $T/out $T/ledger
prov() { # wave wave_id n
  sudo docker run --rm -v $T/t49clone:/w -w /w -e PYTHONPATH=/w/src $IMG python -c "
import sys,tempfile; sys.path.insert(0,'/w/devtools/validation/v4_eval')
import evalspec as e, json
from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore
m=FileMarkerStore(tempfile.mkdtemp())
print(json.dumps(e.plan_wave(e.wave('$1'),wave_id='$2',n_shards=$3,markers=m).provenance(),sort_keys=True))" 2>/dev/null
}
launch() { # name wave split splitfile wave_id n shard [docker-env...] -- [run_arm extra...]
  local name=$1 wave=$2 split=$3 sfile=$4 wid=$5 n=$6 shard=$7; shift 7
  local denv=(); while [ "$1" != "--" ]; do denv+=("$1"); shift; done; shift
  local P; P=$(prov $wave $wid $n)
  local out=$T/out/$wid/shard-${shard}-of-${n}; mkdir -p $out; chmod 777 $out
  ( sudo docker run --rm --name t49-$name -v $T/t49clone:/w:ro -v $T/inputs:/eval/inputs:ro -v $out:/eval/out \
      -v $T/ledger:/eval/ledger -w /w -e PYTHONPATH=/w/src "${denv[@]}" $IMG python devtools/validation/v4_eval/run_arm.py \
      --mode arms --h-mm auto --ops 24 --workers 1 --cache-uri $CACHE --commit $COMMIT --image $IMAGE_ID \
      --machine-type c2d-standard-16 --wave $wave --split $split --split-file /w/$sfile \
      --shard ${shard}/${n} --inputs /eval/inputs --out /eval/out --plan-provenance "$P" "$@" \
      > $T/out/$name.log 2>&1; echo $? > $T/out/$name.rc ) &
}
NOMODEL=(--)
# the model wave first (so spend starts under observation); one shard, ledger + caps
launch ai10 dev_search dev $SPL/PROVISIONAL_dev_ai10.json t49-ai10b 1 1 --env-file $T/secret.env -- \
  --wave-id t49-ai10b --wave-type normal --n-shards 1 --ledger-path /eval/ledger/ai10b.jsonl --marker-bucket $B \
  --model-usd-cap 5 --file-usd-cap 0.5
for i in 1 2 3 4 5; do
  launch dev$i t2_smoke_dev dev $SPL/PROVISIONAL_dev.json t49-dev 5 $i --
done
launch clean t2_smoke_clean clean $SPL/PROVISIONAL_clean.json t49-clean 1 1 --
wait
echo ALLDONE > $T/out/ALLDONE
