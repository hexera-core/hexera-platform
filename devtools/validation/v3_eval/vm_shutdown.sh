#!/bin/bash
# Responsibility: When a CRS v3 eval VM is stopped before its shard finished (max-run-duration, a
#                 manual delete), keep the partial log so the shard's failure is evidence, not a gap.
# Boundaries: best effort inside the shutdown grace period; it never writes DONE.
W=/var/lib/v3eval
[ -f "${W}/out_uri" ] || exit 0
OUT_URI="$(cat "${W}/out_uri")"
gcloud storage ls "${OUT_URI}/DONE" >/dev/null 2>&1 && exit 0
rm -rf "${W}/out/clean-inputs"
gcloud storage rsync -r "${W}/out" "${OUT_URI}" >/dev/null 2>&1   # partial rows + log
[ -f "${W}/out/run.log" ] && gcloud storage cp "${W}/out/run.log" "${OUT_URI}/run.partial.log"
echo "stopped before the shard finished" | gcloud storage cp - "${OUT_URI}/INCOMPLETE"
