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
# v4 only: the model ledger's inputs (wave id and type, the wave's N, and the secret holding the shards'
# marker bucket), written by launch_fleet.py. Read only on a v4 VM; a v3 VM never asks for them.
WAVE_ID=""; WAVE_TYPE=""; NSHARDS=""; MARKER_BUCKET=""; PLAN_PROV=""; SHARD_PLAN_URI=""
if [ "${P}" = "v4" ]; then
  WAVE_ID="$(attr v4-wave-id)"; WAVE_TYPE="$(attr v4-wave-type)"; NSHARDS="$(attr v4-n-shards)"
  MARKER_BUCKET="$(attr v4-marker-bucket)"
  # a cost-balanced shard plan (wave.py): every shard is cut from it instead of the hash. Empty = the hash partition.
  SHARD_PLAN_URI="$(attr v4-shard-plan)"
  # an ARM wave (launch_fleet --wave): the plan_wave provenance the launcher claimed, copied by every shard into its
  # PROVENANCE.json. Empty for the re-probe wave, which runs --mode reprobe exactly as before.
  PLAN_PROV="$(attr v4-plan-provenance)"
fi
# v4 ARMS wave only (empty for the re-probe): the WAVES entry, the arms, the split and where its json is staged,
# and the element size the search arms need.
WAVE=""; ARMS=""; SPLIT=""; SPLIT_URI=""; HMM=""
if [ "${P}" = "v4" ]; then
  WAVE="$(attr v4-wave)"; ARMS="$(attr v4-arms)"; SPLIT="$(attr v4-split)"; SPLIT_URI="$(attr v4-split-uri)"
  HMM="$(attr v4-h-mm)"
fi
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
  # FAIL CLOSED: without the source list run_arm could not know what it may probe.
  [ -n "${SOURCES}" ] || fail_all "no v4-sources metadata: refusing to probe an unspecified pool"
  # FAIL CLOSED: the ledger cannot start without its inputs, and a shard whose wave type, N or marker-store
  # credentials are unknown would spend without the cap the launcher meant.
  [ -n "${WAVE_ID}" ] && [ -n "${WAVE_TYPE}" ] && [ -n "${NSHARDS}" ] && [ -n "${MARKER_BUCKET}" ] \
    || fail_all "no v4-wave-id/-wave-type/-n-shards/-marker-bucket metadata: refusing to run a shard without its model-spend ledger inputs"
  # Disk headroom: the whole input set is copied to every VM; refuse if it would not fit. FAIL CLOSED: a
  # `du` that errors or prints nothing, or a `df` that does, is NOT a pass - an unmeasured copy can fill the
  # disk and take every shard on this VM with it. Both numbers must be plain integers.
  NEED="$(gcloud storage du -s "${PREFIX}/inputs" 2>/dev/null | awk 'NR==1{print $1}')"
  HAVE="$(df -B1 --output=avail "${W}" 2>/dev/null | tail -1 | tr -d ' ')"
  case "${NEED}" in ''|*[!0-9]*) fail_all "cannot measure the staged inputs (gcloud storage du gave '${NEED}')";; esac
  case "${HAVE}" in ''|*[!0-9]*) fail_all "cannot measure free disk under ${W} (df gave '${HAVE}')";; esac
  NEED=$((10#${NEED})); HAVE=$((10#${HAVE}))
  if [ "${NEED}" -gt $((HAVE * 8 / 10)) ]; then
    fail_all "inputs (${NEED} bytes) exceed 80% of free disk (${HAVE} bytes)"
  fi
  # No marker credentials: the shards write markers with the native GCS conditional create over this VM's
  # service account (ADC through the metadata server), and run_arm.py refuses unless the bucket named in
  # ${MARKER_BUCKET} carries the probe's passing record.
  gcloud storage cp "${POOL}" "${W}/pool.json" || fail_all "no staged pool.json at ${POOL}"
  # an arms wave runs over a split: without its json the shard could not say which files it was meant to run
  if [ -n "${ARMS}" ]; then
    gcloud storage cp "${SPLIT_URI}" "${W}/split.json" || fail_all "no staged split json at ${SPLIT_URI}"
    # FAIL CLOSED: an arms wave without the plan the launcher claimed has no slot the report can check (and a
    # dry-run placeholder is not a plan)
    case "${PLAN_PROV}" in
      "{"*) ;;
      *) fail_all "no v4-plan-provenance metadata (got '${PLAN_PROV}'): refusing an arms wave whose slot was not claimed";;
    esac
  fi
  if [ -n "${SHARD_PLAN_URI}" ]; then
    gcloud storage cp "${SHARD_PLAN_URI}" "${W}/shard-plan.json" || fail_all "no staged shard plan at ${SHARD_PLAN_URI}"
  fi
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
  # THE DURABLE SHARD LOG (v4). The ledger's log lives on this VM's boot disk, under ${W}/ledger/<unit>/ - NOT
  # in the container (docker --rm) and not in ${dir} (rewritten by rsync). The boot disk outlives the container
  # and a VM reboot (this script reruns and mounts the same directory), so a restarted shard finds its log and
  # resumes at its recorded spend. A REPLACEMENT VM has a new disk, so the log is mirrored to the shard's results
  # directory every 4 minutes and at exit, and restored here before the shard starts. A restored copy can lag the
  # spend marker; the ledger then raises LostShardLog and refuses (fail closed), never restarting at $0.
  local ldir="" mirror=""
  if [ "${P}" = "v4" ]; then
    ldir="${W}/ledger/$(basename "${dir}")"
    mkdir -p "${ldir}"; chown 1000:1000 "${ldir}"
    if [ ! -f "${ldir}/log.jsonl" ] && gcloud storage ls "${uri}/ledger/log.jsonl" >/dev/null 2>&1; then
      gcloud storage cp "${uri}/ledger/log.jsonl" "${ldir}/log.jsonl" \
        && chown 1000:1000 "${ldir}/log.jsonl" || log "${u}: could not restore the mirrored shard log"
    fi
    ( while sleep 240; do
        gcloud storage cp "${ldir}/log.jsonl" "${uri}/ledger/log.jsonl" >/dev/null 2>&1
      done ) &
    mirror=$!
  fi
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
  # an ARMS wave: the mode, the arms, the split and the element size are the shard's own; the re-probe passes none of
  # them. The plan the launcher claimed (plan_wave's provenance) rides along on every shard of an arms wave and is
  # checked by run_arm against the flags below before it is stamped into PROVENANCE.json.
  local mode=(--mode reprobe) splitmount=() planarg=() planmount=() planflag=()
  if [ -n "${SHARD_PLAN_URI}" ]; then
    planmount=(-v "${W}/shard-plan.json:/eval/shard-plan.json:ro"); planflag=(--shard-plan /eval/shard-plan.json)
  fi
  if [ -n "${ARMS}" ]; then
    mode=(--mode arms --wave "${WAVE}" --arms "${ARMS}" --split "${SPLIT}" --split-file /eval/split.json --h-mm "${HMM}")
    splitmount=(-v "${W}/split.json:/eval/split.json:ro")
    planarg=(--plan-provenance "${PLAN_PROV}")
  fi
  docker run --rm --name "v4eval-${k}" --cpus=1 -e PYTHONPATH=/eval/code/src \
    --env-file "${dir}.env" ${keyenv[@]+"${keyenv[@]}"} \
    -v "${W}/code:/eval/code:ro" -v "${W}/inputs/${kind}:/eval/inputs:ro" -v "${dir}:/eval/out" \
    -v "${ldir}:/eval/ledger" \
    -v "${W}/pool.json:/eval/pool.json:ro" ${splitmount[@]+"${splitmount[@]}"} ${planmount[@]+"${planmount[@]}"} \
    --entrypoint python "${IMAGE}" "/eval/code/${RUN_ARM}" \
    "${mode[@]}" ${planarg[@]+"${planarg[@]}"} ${planflag[@]+"${planflag[@]}"} --shard "${shard}" --inputs /eval/inputs --out /eval/out \
    --pool /eval/pool.json --cache-uri "${CACHE}" --sources "${SOURCES}" --commit "${COMMIT}" \
    --image "${IMAGE}" --machine-type "${MACHINE}" \
    --wave-type "${WAVE_TYPE}" --wave-id "${WAVE_ID}" --n-shards "${NSHARDS}" \
    --ledger-path /eval/ledger/log.jsonl --marker-bucket "${MARKER_BUCKET}" \
    > "${dir}.log" 2>&1
  rc=$?
  fi
  kill "${sync}" 2>/dev/null || true
  if [ "${P}" = "v4" ]; then
    kill "${mirror}" 2>/dev/null || true
    gcloud storage cp "${ldir}/log.jsonl" "${uri}/ledger/log.jsonl" >/dev/null 2>&1 || true
  fi
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
