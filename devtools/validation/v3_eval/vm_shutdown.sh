#!/bin/bash
# Responsibility: When a CRS v3 eval VM is stopped before its shards finished (max-run-duration, a
#                 manual delete), keep each unfinished shard's partial rows and log, so its failure
#                 is evidence, not a gap.
# Boundaries: best effort inside the shutdown grace period; it never writes DONE.
W=/var/lib/v3eval
for uri_file in "${W}"/out/*.uri; do
  [ -f "${uri_file}" ] || continue
  dir="${uri_file%.uri}"; uri="$(cat "${uri_file}")"
  gcloud storage ls "${uri}/DONE" >/dev/null 2>&1 && continue
  rm -rf "${dir}/clean-inputs"
  cp "${dir}.log" "${dir}/run.partial.log" 2>/dev/null
  gcloud storage rsync -r "${dir}" "${uri}" >/dev/null 2>&1
  echo "stopped before the shard finished" | gcloud storage cp - "${uri}/INCOMPLETE"
done
