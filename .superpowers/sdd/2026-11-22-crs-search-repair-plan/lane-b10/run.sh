#!/bin/bash
# lane B10 driver, ON the VM. Sentinel: ~/b10/out/ALLDONE. Results also copied to gs://.../eval/lane-b10/.
set -u
T=$HOME/b10; IMG=us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:633e1c3bfd8e2c0fb97b166fa28ff834d0433b84ca8e77c24eb4b00c83ece361
cd $T && mkdir -p code inputs out && tar xzf code.tgz -C code && chmod -R a+rwX $T
docker-credential-gcr configure-docker --registries us-central1-docker.pkg.dev >/dev/null 2>&1
docker pull -q $IMG > $T/out/pull.log 2>&1
docker run --rm -v $T:/b10 -w /b10 $IMG python /b10/fetch.py /b10/dev30.txt /b10/inputs > $T/out/fetch.log 2>&1
chmod -R a+rwX $T
run_one() {
  sha=$1; f=$(ls $T/inputs/$sha/ | head -1); mkdir -p $T/out/$sha; chmod a+rwX $T/out/$sha
  timeout 3000 docker run --rm --name b10-${sha:0:12} --label owner=lane-b10 -v $T/code:/w:ro -v $T/inputs:/in:ro \
    -v $T/out:/out -v $T/staged_tiers.py:/s.py:ro -w /w -e PYTHONPATH=/w/src $IMG \
    python /s.py "/in/$sha/$f" /out/$sha > $T/out/$sha.log 2>&1; echo $? > $T/out/$sha.rc
}
export -f run_one; export T IMG
cat $T/dev30.txt | xargs -P 3 -I{} bash -c 'run_one {}'
docker run --rm -v $T/out:/out $IMG python -c "
from pathlib import Path; from google.cloud import storage
b=storage.Client().bucket('dev-crsv3-transfer-224734058693')
for p in Path('/out').rglob('*'):
  if p.is_file(): b.blob('eval/lane-b10/'+str(p.relative_to('/out'))).upload_from_filename(str(p))
" > $T/out/upload.log 2>&1
date -u +%FT%TZ > $T/out/ALLDONE
