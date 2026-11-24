#!/bin/bash
# Snapshot v4 collect/post, ON the first VM. Pulls every VM's out/ from the bucket, regroups and reports in the
# image, and pushes the result back. Each step reports its OWN status to stdout and nothing uses `set -e`: pilot 3
# failed here in about 5 s with no diagnosis at all, because one chained command's exit code ended the whole
# remote shell and the log only said "the collect/post step failed".
#   bash collect.sh            (reads ~/snap/run.env)
set -u
T=$HOME/snap; source $T/run.env
cd "$T" || { echo "COLLECT no ~/snap"; exit 1; }
say() { echo "COLLECT $*"; }

mkdir -p out
gcloud storage rsync -r -q "$SYNC/out" out; say "pull_out rc=$?"
mkdir -p ledger
gcloud storage rsync -r -q "$SYNC/ledger" ledger; say "pull_ledger rc=$?"    # a wave with no model arm has none
ls out | wc -l | xargs -I{} echo "COLLECT out_entries={}"

sudo docker run --rm -v "$T/code:/w:ro" -v "$T/tools:/tools:ro" -v "$T/splits:/eval/splits:ro" \
  -v "$T/out:/eval/out" -w /w -e PYTHONPATH=/w/src meshpipeline-mesh:native \
  python /tools/post.py --out /eval/out --splits /eval/splits --manifest /eval/splits/launch_manifest.json \
  > out/post.log 2>&1
rc=$?
echo "$rc" > out/post.rc
say "post rc=$rc"
[ "$rc" = 0 ] || tail -25 out/post.log | sed 's/^/COLLECT post| /'

date -u +%FT%TZ > out/ALLDONE
gcloud storage rsync -r -q out "$SYNC/out"; say "push_out rc=$?"
say "done post_rc=$rc"
exit "$rc"
