#!/bin/bash
# Responsibility: The startup script of one CRS eval VM (v3 verdict wave or v4 re-probe wave): run its
#                 shards (one container per vCPU, each one shard) in the image, stream rows and
#                 PROVENANCE to GCS, then delete this VM.
# Owns: the VM's working directory, the docker runs, the GCS write-back and the self-delete.
# Boundaries: everything it runs arrives as instance metadata (launch_fleet.py writes it); the model
#             key is read from Secret Manager into a 0600 env-file, never echoed, and removed once
#             every container has started. --max-run-duration with DELETE is the backstop.
#             THE PROFILE: a VM whose metadata carries v3-image is a v3 VM (every v3 command below is
#             unchanged); one that carries v4-image is a v4 VM, and reads v4-* keys, runs the
#             v4-run-arm script and needs no model key. The v3 branch of every fork is the original text.
set -uo pipefail

md()   { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1"; }
log()  { echo "$(date -u +%FT%TZ) ${P:-v3}-eval: $*"; }

# retry <cmd...>: up to 4 attempts, backing off V3EVAL_RETRY_SLEEP x 1, 2, 4 seconds.
retry() {
  local attempt delay="${V3EVAL_RETRY_SLEEP:-5}"
  for attempt in 1 2 3 4; do
    "$@" && return 0
    [ "${attempt}" -lt 4 ] && sleep $((delay * (1 << (attempt - 1))))
  done
  return 1
}

# publish_unit <dir> <uri> <exit code>: write one finished shard back, THEN its marker. DONE is
# written only when the container exited 0 AND rows.json and PROVENANCE.json are confirmed in GCS;
# anything else gets FAILED, which the resume (launch_fleet done_units) and collect.py treat as
# not done, so the shard is relaunched rather than collected with its rows missing.
publish_unit() {
  local dir="$1" uri="$2" rc="$3" f why=""
  retry gcloud storage rsync -r "${dir}" "${uri}" || why="write-back rsync failed"
  for f in rows.json PROVENANCE.json; do
    if [ ! -f "${dir}/${f}" ]; then
      [ "${rc}" = "0" ] && why="${why:+${why}; }no ${f} written"
    elif ! retry gcloud storage cp "${dir}/${f}" "${uri}/${f}"; then
      why="${why:+${why}; }upload of ${f} failed"
    fi
  done
  # markers go up from a local file, so every retry re-reads the same bytes
  if [ "${rc}" = "0" ] && [ -z "${why}" ]; then
    echo "${rc}" > "${dir}.DONE"
    retry gcloud storage cp "${dir}.DONE" "${uri}/DONE" && return 0
    why="DONE upload failed"
  fi
  [ "${rc}" = "0" ] && log "${uri}: exit 0 but ${why}; marking FAILED (retry-eligible)"
  echo "exit ${rc}${why:+; ${why}}" > "${dir}.FAILED"
  retry gcloud storage cp "${dir}.FAILED" "${uri}/FAILED" \
    || log "${uri}: FAILED marker upload failed too"
  return 1
}

# Tests source this file for the functions above and stop here.
[ "${V3EVAL_SOURCE_ONLY:-}" = "1" ] && return 0

# P: the metadata prefix of this VM's profile (v3 unless it carries v4-image). Set after the
# source-only return above, so sourcing this file for its functions never reads metadata.
P=v3; [ -z "$(attr v3-image)" ] && [ -n "$(attr v4-image)" ] && P=v4

NAME="$(md instance/name)"
ZONE="$(md instance/zone | awk -F/ '{print $NF}')"
PROJECT="$(md project/project-id)"
IMAGE="$(attr ${P}-image)"; COMMIT="$(attr ${P}-commit)"; IMAGE_COMMIT="$(attr ${P}-image-commit)"
UNITS="$(attr ${P}-units)"; BUCKET="$(attr ${P}-bucket)"; PREFIX="$(attr ${P}-prefix)"
CODE="$(attr ${P}-code)"; MACHINE="$(attr ${P}-machine-type)"; SECRET="$(attr ${P}-model-secret)"
# v4 only: the run_arm.py to invoke (repo-relative), the staged pool manifest, the verdict cache and
# the pool sources the wave may probe. Empty on a v3 VM.
RUN_ARM="$(attr ${P}-run-arm)"; POOL="$(attr ${P}-pool)"; CACHE="$(attr ${P}-cache-uri)"
SOURCES="$(attr ${P}-sources)"
W="${V3EVAL_WORKDIR:-/var/lib/v3eval}"   # vm_shutdown.sh reads the same directory; tests override it

self_delete() {
  log "deleting ${NAME}"
  gcloud compute instances delete "${NAME}" --zone "${ZONE}" --project "${PROJECT}" --quiet \
    || log "self-delete failed; --max-run-duration (DELETE) will remove the VM"
}

# unit "arm:kind:I/N" -> its GCS output directory
unit_uri() {
  local arm="${1%%:*}" rest="${1#*:}"; local kind="${rest%%:*}" shard="${rest#*:}"
  echo "${BUCKET}/eval/${arm}-${COMMIT:0:12}/${kind}/shard-${shard%/*}-of-${shard#*/}"
}
unit_dir() { echo "${W}/out/$(echo "$1" | tr ':/' '__')"; }

IFS=',' read -r -a ALL <<< "${UNITS}"
TODO=()
for u in "${ALL[@]}"; do
  # IDEMPOTENT: a shard already written back is never re-run (a relaunch, or this VM rebooting).
  if gcloud storage ls "$(unit_uri "$u")/DONE" >/dev/null 2>&1; then
    log "$u: DONE exists, skipping"
  else
    TODO+=("$u")
  fi
done
[ "${#TODO[@]}" -eq 0 ] && { self_delete; exit 0; }

mkdir -p "${W}/code" "${W}/inputs" "${W}/out"
printf '%s\n' "${TODO[@]}" > "${W}/units"

if ! command -v docker >/dev/null 2>&1; then
  log "installing docker"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -y -q ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q && apt-get install -y -q docker-ce docker-ce-cli containerd.io
  systemctl enable --now docker
fi

fail_all() {
  log "FAILED: $*"
  for u in "${TODO[@]}"; do
    echo "$*" | gcloud storage cp - "$(unit_uri "$u")/FAILED" || true
  done
  self_delete; exit 1
}

log "staging code ${COMMIT:0:12}, sample and inputs for ${#TODO[@]} shard(s)"
gcloud storage cp "${CODE}" "${W}/code.tgz" || fail_all "no code.tgz at ${CODE}"
tar -xzf "${W}/code.tgz" -C "${W}/code" || fail_all "code.tgz does not extract"
if [ "${P}" = "v3" ]; then
  gcloud storage cp "${PREFIX}/sample.json" "${W}/sample.json" || fail_all "no staged sample.json"
  for kind in failing clean; do
    if grep -q ":${kind}:" "${W}/units"; then
      mkdir -p "${W}/inputs/${kind}"
      gcloud storage cp -r "${PREFIX}/inputs/${kind}/*" "${W}/inputs/${kind}/" \
        || fail_all "no staged ${kind} inputs"
    fi
  done
else
  # v4: one input set for the whole pool, laid out <sha256>/<basename> (run_arm.input_path)
  gcloud storage cp "${POOL}" "${W}/pool.json" || fail_all "no staged pool.json at ${POOL}"
  mkdir -p "${W}/inputs/pool"
  gcloud storage cp -r "${PREFIX}/inputs/*" "${W}/inputs/pool/" || fail_all "no staged pool inputs"
fi
SPEC="${W}/code/devtools/validation/v3_eval/evalspec.py"

# THE KEY: written once, 0600, read by docker at container start, removed once all have started.
# A v3 VM always needs it; a v4 VM only when the launcher named a secret (the v4 checker needs none).
if [ "${P}" = "v3" ] || [ -n "${SECRET}" ]; then
( umask 077
  { printf 'OPENAI_API_KEY='; gcloud secrets versions access latest --secret="${SECRET}" --project="${PROJECT}"; echo; } \
    > "${W}/secret.env" ) || fail_all "cannot read secret ${SECRET}"
fi

gcloud auth configure-docker "$(echo "${IMAGE}" | cut -d/ -f1)" --quiet
docker pull "${IMAGE}" || fail_all "cannot pull ${IMAGE}"

run_unit() {
  local u="$1" k="$2"
  local arm="${u%%:*}" rest="${u#*:}"; local kind="${rest%%:*}" shard="${rest#*:}"
  local dir uri rc
  dir="$(unit_dir "$u")"; uri="$(unit_uri "$u")"
  mkdir -p "${dir}"; chown 1000:1000 "${dir}"   # the image runs as uid 1000
  gcloud storage rm "${uri}/FAILED" "${uri}/INCOMPLETE" >/dev/null 2>&1 || true
  echo "${uri}" > "${dir}.uri"
  if [ "${P}" = "v3" ]; then python3 "${SPEC}" env "${arm}" > "${dir}.env"; else : > "${dir}.env"; fi
  # ROWS LEAVE THE VM AS THEY ARE WRITTEN: repair_corpus streams one row per file into
  # bundle/rows.jsonl; this loop syncs the shard's directory every 4 minutes.
  ( while sleep 240; do
      gcloud storage rsync -r -x 'clean-inputs/.*' "${dir}" "${uri}" >/dev/null 2>&1
    done ) &
  local sync=$!
  log "start ${u}"
  # PYTHONPATH=/eval/code/src: the archived commit's src/ shadows the image's installed wheel;
  # the image supplies the toolchain only. run_arm.py refuses if meshpipeline resolves elsewhere.
  if [ "${P}" = "v3" ]; then
  docker run --rm --name "v3eval-${k}" --cpus=1 -e PYTHONPATH=/eval/code/src \
    --env-file "${dir}.env" --env-file "${W}/secret.env" \
    -v "${W}/code:/eval/code:ro" -v "${W}/inputs/${kind}:/eval/inputs:ro" -v "${dir}:/eval/out" \
    -v "${W}/sample.json:/eval/sample.json:ro" \
    --entrypoint python "${IMAGE}" /eval/code/devtools/validation/v3_eval/run_arm.py \
    --arm "${arm}" --kind "${kind}" --shard "${shard}" --inputs /eval/inputs --out /eval/out \
    --code-root /eval/code --sample-json /eval/sample.json --commit "${COMMIT}" \
    --image-commit "${IMAGE_COMMIT}" --image "${IMAGE}" --machine-type "${MACHINE}" \
    > "${dir}.log" 2>&1
  rc=$?
  else
  # v4: the key file is mounted only when the launcher named a secret
  local keyenv=(); [ -f "${W}/secret.env" ] && keyenv=(--env-file "${W}/secret.env")
  docker run --rm --name "v4eval-${k}" --cpus=1 -e PYTHONPATH=/eval/code/src \
    --env-file "${dir}.env" ${keyenv[@]+"${keyenv[@]}"} \
    -v "${W}/code:/eval/code:ro" -v "${W}/inputs/${kind}:/eval/inputs:ro" -v "${dir}:/eval/out" \
    -v "${W}/pool.json:/eval/pool.json:ro" \
    --entrypoint python "${IMAGE}" "/eval/code/${RUN_ARM}" \
    --mode reprobe --shard "${shard}" --inputs /eval/inputs --out /eval/out \
    --pool /eval/pool.json --cache-uri "${CACHE}" --sources "${SOURCES}" --commit "${COMMIT}" \
    --image "${IMAGE}" --machine-type "${MACHINE}" \
    > "${dir}.log" 2>&1
  rc=$?
  fi
  kill "${sync}" 2>/dev/null || true
  cp "${dir}.log" "${dir}/run.log" 2>/dev/null || true
  rm -rf "${dir}/clean-inputs"   # symlinks into the inputs: CAD, never written back
  publish_unit "${dir}" "${uri}" "${rc}" || log "${u}: not DONE (exit ${rc}); relaunch to retry"
  log "end ${u}: exit ${rc}"
}

PIDS=()
k=0
for u in "${TODO[@]}"; do
  k=$((k + 1))
  run_unit "$u" "$k" &
  PIDS+=($!)
done
# Every container reads the env-files when it starts; once all have started (or 5 minutes have
# passed) the key file is dropped.
for _ in $(seq 1 60); do
  [ "$(docker ps -a -q --filter name=${P}eval- | wc -l)" -ge "${#TODO[@]}" ] && break
  sleep 5
done
rm -f "${W}/secret.env"
for pid in "${PIDS[@]}"; do wait "${pid}"; done
log "all ${#TODO[@]} shard(s) finished"
self_delete
