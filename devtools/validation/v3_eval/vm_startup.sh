#!/bin/bash
# Responsibility: The startup script of one CRS v3 eval VM: run one shard of one arm in the image,
#                 write its rows and PROVENANCE to GCS, then delete this VM.
# Owns: the VM's working directory, the docker run, the GCS write-back and the self-delete.
# Boundaries: everything it runs arrives as instance metadata (launch_fleet.py writes it); the model
#             key is read from Secret Manager into a 0600 env-file and never echoed. The VM's
#             --max-run-duration with DELETE is the backstop if this script never reaches the end.
set -uo pipefail

md()   { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1"; }
log()  { echo "$(date -u +%FT%TZ) v3-eval: $*"; }

NAME="$(md instance/name)"
ZONE="$(md instance/zone | awk -F/ '{print $NF}')"
PROJECT="$(md project/project-id)"
IMAGE="$(attr v3-image)"; COMMIT="$(attr v3-commit)"; ARM="$(attr v3-arm)"; KIND="$(attr v3-kind)"
SHARD="$(attr v3-shard)"; PREFIX="$(attr v3-prefix)"; MACHINE="$(attr v3-machine-type)"
SECRET="$(attr v3-model-secret)"; ACCEPT="$(attr v3-accept-inert || true)"
I="${SHARD%/*}"; N="${SHARD#*/}"
RESULTS="$(attr v3-results)"
OUT_URI="${RESULTS}/${KIND}/shard-${I}-of-${N}"
W=/var/lib/v3eval

self_delete() {
  log "deleting ${NAME}"
  gcloud compute instances delete "${NAME}" --zone "${ZONE}" --project "${PROJECT}" --quiet \
    || log "self-delete failed; --max-run-duration (DELETE) will remove the VM"
}

# IDEMPOTENT: a shard already written back is never re-run (a relaunch, or this VM rebooting).
if gcloud storage ls "${OUT_URI}/DONE" >/dev/null 2>&1; then
  log "${OUT_URI}/DONE exists; nothing to do"
  self_delete; exit 0
fi

mkdir -p "${W}/code" "${W}/inputs" "${W}/out"
echo "${OUT_URI}" > "${W}/out_uri"
# A rerun of a failed shard starts clean: an earlier attempt's markers would contradict its rows.
gcloud storage rm "${OUT_URI}/FAILED" "${OUT_URI}/INCOMPLETE" >/dev/null 2>&1 || true

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

fail() {
  log "FAILED: $*"
  echo "$*" > "${W}/out/FAILED"
  gcloud storage cp -r "${W}/out/"* "${OUT_URI}/" || true
  self_delete; exit 1
}

log "staging code ${COMMIT:0:12}, sample and ${KIND} inputs"
gcloud storage cp "${PREFIX}/${COMMIT:0:12}/code.tgz" "${W}/code.tgz" || fail "no code.tgz at ${COMMIT:0:12}"
tar -xzf "${W}/code.tgz" -C "${W}/code" || fail "code.tgz does not extract"
gcloud storage cp "${PREFIX}/sample.json" "${W}/sample.json" || fail "no staged sample.json"
gcloud storage cp -r "${PREFIX}/inputs/${KIND}/*" "${W}/inputs/" || fail "no staged ${KIND} inputs"
attr v3-arm-env > "${W}/arm.env" || fail "no v3-arm-env metadata"

# THE KEY: written once, 0600, read by docker, removed after. Never printed, never in metadata.
( umask 077
  { printf 'OPENAI_API_KEY='; gcloud secrets versions access latest --secret="${SECRET}" --project="${PROJECT}"; echo; } \
    > "${W}/secret.env" ) || fail "cannot read secret ${SECRET}"

gcloud auth configure-docker "$(echo "${IMAGE}" | cut -d/ -f1)" --quiet
docker pull "${IMAGE}" || fail "cannot pull ${IMAGE}"
# The image runs as uid 1000 (USER_A); the output directory must be its to write.
chown -R 1000:1000 "${W}/out"

ACCEPT_ARGS=()
IFS=',' read -r -a _accept <<< "${ACCEPT}"
for flag in "${_accept[@]}"; do [ -n "${flag}" ] && ACCEPT_ARGS+=(--accept-inert "${flag}"); done

# ROWS LEAVE THE VM AS THEY ARE WRITTEN: repair_corpus streams one row per file into
# bundle/rows.jsonl, and this loop syncs the output directory to GCS every few minutes, so a VM
# that dies mid-shard still leaves every finished file's row behind.
( while sleep 240; do
    gcloud storage rsync -r -x 'clean-inputs/.*' "${W}/out" "${OUT_URI}" >/dev/null 2>&1
  done ) &
SYNC_PID=$!

log "running ${ARM}/${KIND} shard ${SHARD} on ${MACHINE}"
docker run --rm --name v3eval \
  --env-file "${W}/arm.env" --env-file "${W}/secret.env" \
  -v "${W}/code:/eval/code:ro" -v "${W}/inputs:/eval/inputs:ro" -v "${W}/out:/eval/out" \
  -v "${W}/sample.json:/eval/sample.json:ro" \
  --entrypoint python "${IMAGE}" /eval/code/devtools/validation/v3_eval/run_arm.py \
  --arm "${ARM}" --kind "${KIND}" --shard "${SHARD}" --inputs /eval/inputs --out /eval/out \
  --code-root /eval/code --sample-json /eval/sample.json --commit "${COMMIT}" --image "${IMAGE}" \
  --machine-type "${MACHINE}" "${ACCEPT_ARGS[@]}" 2>&1 | tee "${W}/out/run.log"
RC="${PIPESTATUS[0]}"
kill "${SYNC_PID}" 2>/dev/null || true
rm -f "${W}/secret.env"

rm -rf "${W}/out/clean-inputs"   # symlinks into the inputs: CAD, never written back
gcloud storage cp -r "${W}/out/"* "${OUT_URI}/" || log "write-back failed"
if [ "${RC}" = "0" ]; then
  echo "${RC}" | gcloud storage cp - "${OUT_URI}/DONE"
else
  echo "${RC}" | gcloud storage cp - "${OUT_URI}/FAILED"
fi
log "shard ${SHARD} exited ${RC}"
self_delete
