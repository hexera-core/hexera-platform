#!/bin/bash
# lane D15: det-only search_det on the 30 dev_v1r files, instrumented sweep. No model, no secrets. Self-deletes.
exec > /var/log/laned-d15.log 2>&1
set -x
S=gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe-staging/d15-9766d1b90d90
IN=gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/inputs
IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:633e1c3bfd8e2c0fb97b166fa28ff834d0433b84ca8e77c24eb4b00c83ece361
NAME=$(curl -s -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/name)
ZONE=$(curl -s -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/zone | cut -d/ -f4)
finish() { rm -rf /w/out/work; gcloud storage cp -r /w/out $S/ ; gcloud storage cp /var/log/laned-d15.log $S/vm.log; gcloud compute instances delete "$NAME" --zone "$ZONE" --quiet; }
trap finish EXIT
apt-get update -qq && apt-get install -y -qq docker.io >/dev/null
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker pull $IMG
W=/w; mkdir -p $W/code $W/in $W/out && cd $W
gcloud storage cp $S/code.tgz $S/split.json $S/shas.txt . && tar -xzf code.tgz -C code
for h in $(cat shas.txt); do gcloud storage cp -r "$IN/$h" in/ ; done
chmod -R a+rwX $W/code $W/in $W/out; chmod a+r $W/split.json
( while sleep 300; do gcloud storage cp $W/out/*.json $W/out/*.log $S/out/ >/dev/null 2>&1; done ) &
seq 0 29 | xargs -P 14 -I{} sh -c "docker run --rm --cpus=1 --name d15-{} -v $W/code:/w:ro -v $W/in:/eval/in:ro -v $W/split.json:/eval/split.json:ro -v $W/out:/eval/out -w /w -e PYTHONPATH=/w/src $IMG timeout 2400 python /w/d15_probe.py --split /eval/split.json --inputs /eval/in --out /eval/out --index {} > $W/out/f{}.log 2>&1"
date -u +%FT%TZ > out/END
