#!/bin/bash
# Responsibility: The startup script of one CRS v3 eval VM: run its shards (one container per vCPU,
#                 each one shard of one arm) in the image, stream rows and PROVENANCE to GCS, then
#                 delete this VM.
# Owns: the VM's working directory, the docker runs, the GCS write-back and the self-delete.
# Boundaries: everything it runs arrives as instance metadata (launch_fleet.py writes it); the model
#             key is read from Secret Manager into a 0600 env-file, never echoed, and removed once
#             every container has started. --max-run-duration with DELETE is the backstop.
set -uo pipefail

md()   { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1"; }
log()  { echo "$(date -u +%FT%TZ) v3-eval: $*"; }

NAME="$(md instance/name)"
ZONE="$(md instance/zone | awk -F/ '{print $NF}')"
PROJECT="$(md project/project-id)"
IMAGE="$(attr v3-image)"; COMMIT="$(attr v3-commit)"; IMAGE_COMMIT="$(attr v3-image-commit)"
UNITS="$(attr v3-units)"; BUCKET="$(attr v3-bucket)"; PREFIX="$(attr v3-prefix)"
CODE="$(attr v3-code)"; MACHINE="$(attr v3-machine-type)"; SECRET="$(attr v3-model-secret)"
W=/var/lib/v3eval

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
gcloud storage cp "${PREFIX}/sample.json" "${W}/sample.json" || fail_all "no staged sample.json"
for kind in failing clean; do
  if grep -q ":${kind}:" "${W}/units"; then
    mkdir -p "${W}/inputs/${kind}"
    gcloud storage cp -r "${PREFIX}/inputs/${kind}/*" "${W}/inputs/${kind}/" \
      || fail_all "no staged ${kind} inputs"
  fi
done
SPEC="${W}/code/devtools/validation/v3_eval/evalspec.py"

# THE KEY: written once, 0600, read by docker at container start, removed once all have started.
( umask 077
  { printf 'OPENAI_API_KEY='; gcloud secrets versions access latest --secret="${SECRET}" --project="${PROJECT}"; echo; } \
    > "${W}/secret.env" ) || fail_all "cannot read secret ${SECRET}"

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
  python3 "${SPEC}" env "${arm}" > "${dir}.env"
  # ROWS LEAVE THE VM AS THEY ARE WRITTEN: repair_corpus streams one row per file into
  # bundle/rows.jsonl; this loop syncs the shard's directory every 4 minutes.
  ( while sleep 240; do
      gcloud storage rsync -r -x 'clean-inputs/.*' "${dir}" "${uri}" >/dev/null 2>&1
    done ) &
  local sync=$!
  log "start ${u}"
  # PYTHONPATH=/eval/code/src: the archived commit's src/ shadows the image's installed wheel;
  # the image supplies the toolchain only. run_arm.py refuses if meshpipeline resolves elsewhere.
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
  kill "${sync}" 2>/dev/null || true
  cp "${dir}.log" "${dir}/run.log" 2>/dev/null || true
  rm -rf "${dir}/clean-inputs"   # symlinks into the inputs: CAD, never written back
  gcloud storage rsync -r "${dir}" "${uri}" || log "${u}: write-back failed"
  if [ "${rc}" = "0" ]; then
    echo "${rc}" | gcloud storage cp - "${uri}/DONE"
  else
    echo "${rc}" | gcloud storage cp - "${uri}/FAILED"
  fi
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
  [ "$(docker ps -a -q --filter name=v3eval- | wc -l)" -ge "${#TODO[@]}" ] && break
  sleep 5
done
rm -f "${W}/secret.env"
for pid in "${PIDS[@]}"; do wait "${pid}"; done
log "all ${#TODO[@]} shard(s) finished"
self_delete
