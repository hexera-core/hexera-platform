#!/bin/bash
# Snapshot runner, ON the VM. Reads ~/snap/run.env (written by launch.sh). Stages inputs from the bucket (sha256
# verified), runs every shard of both waves concurrently, retries a shard ONCE if it refused at the route probe
# (exit 6), runs report/triage in the image, then writes out/ALLDONE. Each shard's exit code is out/<name>.rc.
set -u
T=$HOME/snap; source $T/run.env
IMG=meshpipeline-mesh:native
IMAGE_ID=$(sudo docker inspect --format '{{.Id}}' $IMG)
mkdir -p $T/out $T/ledger $T/inputs; chmod 777 $T/out $T/ledger
date -u +%FT%TZ > $T/out/STAGE_START
# --- inputs: <sha256>/<basename>, every byte checked against the split's sha256
cut -d' ' -f1,2 $T/splits/inputs.txt | xargs -P 16 -n 2 sh -c 'mkdir -p "$0/inputs/$1" && gcloud storage cp -q "$2" "$0/inputs/$1/" >/dev/null 2>&1 || echo "$1 copy-failed"' $T > $T/out/stage_errors.txt
# a copy that failed under the parallel pass is retried, one at a time, up to 3 times (transient on a fresh VM)
for attempt in 1 2 3; do
  failed=$(grep copy-failed $T/out/stage_errors.txt 2>/dev/null | cut -d' ' -f1 | sort -u)
  [ -z "$failed" ] && break
  : > $T/out/stage_errors.txt
  for sha in $failed; do url=$(grep "^$sha " $T/splits/inputs.txt | cut -d' ' -f2)
    sleep 2; gcloud storage cp -q "$url" "$T/inputs/$sha/" >/dev/null 2>&1 || echo "$sha copy-failed" >> $T/out/stage_errors.txt
    echo "$sha retried attempt=$attempt" >> $T/out/stage_retries.txt; done
done
bad=0; n=0
while read -r sha url; do
  n=$((n+1)); f="$T/inputs/$sha/$(basename "$url")"
  got=$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1); [ "$got" = "$sha" ] || { echo "$sha mismatch-or-missing" >> $T/out/stage_errors.txt; bad=$((bad+1)); }
done < $T/splits/inputs.txt
chmod -R a+rX $T/inputs
echo "staged=$n bad=$bad" > $T/out/STAGE_DONE
if [ "$bad" != 0 ]; then echo "input staging failed: $bad of $n" > $T/out/ABORTED; date -u +%FT%TZ > $T/out/ALLDONE; exit 1; fi

prov() { # wave wave_id n
  sudo docker run --rm -v $T/code:/w:ro -w /w -e PYTHONPATH=/w/src $IMG python -c "
import sys,tempfile; sys.path.insert(0,'/w/devtools/validation/v4_eval')
import evalspec as e, json
from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore
m=FileMarkerStore(tempfile.mkdtemp())
print(json.dumps(e.plan_wave(e.wave('$1'),wave_id='$2',n_shards=$3,markers=m).provenance(),sort_keys=True))" 2>/dev/null
}
run_shard() { # name wave split wave_id n shard cap
  local name=$1 wave=$2 split=$3 wid=$4 n=$5 shard=$6 cap=$7
  local P; P=$(prov $wave $wid $n)
  local out=$T/out/$wid/shard-${shard}-of-${n}; mkdir -p $out; chmod 777 $out
  date -u +%FT%TZ > $T/out/$name.start
  sudo docker run --rm --name snap-$name -v $T/code:/w:ro -v $T/splits:/eval/splits:ro -v $T/inputs:/eval/inputs:ro \
    -v $out:/eval/out -v $T/ledger:/eval/ledger -w /w -e PYTHONPATH=/w/src --env-file $T/secret.env $IMG \
    python devtools/validation/v4_eval/run_arm.py \
    --mode arms --h-mm auto --ops $OPS --workers 1 --cache-uri $CACHE --commit $COMMIT --image $IMAGE_ID \
    --machine-type $MACHINE --wave $wave --split $split --split-file /eval/splits/$split.json \
    --shard-plan /eval/splits/plan_$split.json --shard ${shard}/${n} --inputs /eval/inputs --out /eval/out \
    --plan-provenance "$P" --route $ROUTE --wave-id $wid --wave-type normal --n-shards $n \
    --ledger-path /eval/ledger/$wid-s$shard.jsonl --marker-bucket $BUCKET \
    --model-usd-cap $cap --file-usd-cap $FILE_USD_CAP > $T/out/$name.log 2>&1
  echo $? > $T/out/$name.rc; date -u +%FT%TZ > $T/out/$name.end
}
shard_with_retry() {
  local name=$1; run_shard "$@"
  if [ "$(cat $T/out/$name.rc)" = 6 ]; then   # refused before any file (no key / route probe): one retry, evidence kept
    mkdir -p $T/out/attempt1; for x in log rc start end; do cp $T/out/$name.$x $T/out/attempt1/; done
    cp $T/out/$4/shard-$6-of-$5/PROVENANCE.json $T/out/attempt1/$name.PROVENANCE.json 2>/dev/null
    rm -f $T/out/$4/shard-$6-of-$5/PROVENANCE.json; echo "$name" >> $T/out/RETRIED
    run_shard "$@"
  fi
}
date -u +%FT%TZ > $T/out/LAUNCHED
read -r -a CD <<< "$CAPS_DEV"; read -r -a CC <<< "$CAPS_CLEAN"
for i in $(seq 1 $DEV_N); do shard_with_retry dev$i $DEV_WAVE dev $WAVE_ID_DEV $DEV_N $i ${CD[$((i-1))]} & done
for i in $(seq 1 $CLEAN_N); do shard_with_retry clean$i $CLEAN_WAVE clean $WAVE_ID_CLEAN $CLEAN_N $i ${CC[$((i-1))]} & done
wait
date -u +%FT%TZ > $T/out/RUN_DONE
shred -u $T/secret.env 2>/dev/null; ls $T/secret.env >/dev/null 2>&1 && echo present > $T/out/SECRET_STILL_THERE
# --- report / triage in the image, with the commit's own code
sudo docker run --rm -v $T/code:/w:ro -v $T/tools:/tools:ro -v $T/splits:/eval/splits:ro -v $T/out:/eval/out \
  -w /w -e PYTHONPATH=/w/src $IMG python /tools/post.py --out /eval/out --splits /eval/splits \
  --dev-wave $DEV_WAVE --clean-wave $CLEAN_WAVE --dev-id $WAVE_ID_DEV --clean-id $WAVE_ID_CLEAN \
  --dev-n $DEV_N --clean-n $CLEAN_N > $T/out/post.log 2>&1
echo $? > $T/out/post.rc
date -u +%FT%TZ > $T/out/ALLDONE
