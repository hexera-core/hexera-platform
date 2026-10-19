#!/bin/bash
# Responsibility: Keep a worker VM's two containers working - restart one whose consumer stopped beating - and say what the VM runs.
# Owns: the restart rule for an unhealthy container, and the `hexera/*` guest attributes a fleet deploy verifies against.
# Boundaries: it reads docker's own health verdict (the healthcheck is src/meshpipeline/runtime/worker_liveness.py) and restarts containers; it never deletes or recreates the VM.
#
# This is installed and started by startup.sh as the systemd unit hexera-worker-watchdog.
#
# WHY IT EXISTS. Docker's `--restart always` restarts a container whose process EXITS. A worker
# whose broker connection hung did not exit: shared dev, 2026-10-16, the geometry worker sat on a
# dead connection for three days while docker reported it "Up". Each container now carries a
# healthcheck that fails once its consumer's event loop stops beating, and docker marks it
# `unhealthy` - but docker does not act on that by itself. This does: an unhealthy container that is
# running is restarted, with the same stop budget startup.sh and shutdown.sh give it, so a job it
# holds still hands itself back first.
#
# WHAT IT PUBLISHES. Every pass writes guest attributes under `hexera/` - the image reference each
# container was started from, docker's health verdict for each, the time of the pass, and how often
# it has restarted something. create-worker-fleet.sh reads them to prove that a rolled VM is running
# the new digest and is healthy, rather than trusting that a VM made from the new template must be.
# They need `enable-guest-attributes=TRUE` in the template's metadata; without it the writes are
# refused and nothing else changes.
#
# NOT DURING SHUTDOWN. shutdown.sh stops this unit and leaves /run/hexera-shutting-down before it
# stops the containers, so a container being stopped on purpose is never restarted.
set -uo pipefail

WATCHDOG_INTERVAL_SECONDS="${WATCHDOG_INTERVAL_SECONDS:-30}"
SHUTDOWN_MARK="${WATCHDOG_SHUTDOWN_MARK:-/run/hexera-shutting-down}"
GA="http://metadata.google.internal/computeMetadata/v1/instance/guest-attributes/hexera"
RESTARTS=0

# container : guest-attribute prefix : stop budget (the same as startup.sh's --stop-timeout)
CONTAINERS=("hexera-worker:worker:75" "hexera-geometry-worker:geometry:30")

say() { logger -t hexera-watchdog -- "$*" 2>/dev/null || true; echo "hexera-watchdog: $*"; }

ga_put() {  # <key> <value>
  curl -sf -X PUT --max-time 5 -H 'Metadata-Flavor: Google' --data "$2" "${GA}/$1" >/dev/null 2>&1 || true
}

pass() {
  local entry name label budget state running health image
  for entry in "${CONTAINERS[@]}"; do
    name="${entry%%:*}"; label="${entry#*:}"; budget="${label#*:}"; label="${label%%:*}"
    state="$(docker inspect -f '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "${name}" 2>/dev/null || echo "false missing")"
    running="${state%% *}"; health="${state#* }"
    image="$(docker inspect -f '{{.Config.Image}}' "${name}" 2>/dev/null || true)"
    if [ "${running}" = true ] && [ "${health}" = unhealthy ] && [ ! -e "${SHUTDOWN_MARK}" ]; then
      local why
      why="$(docker inspect -f '{{with .State.Health}}{{range .Log}}{{.Output}}{{end}}{{end}}' "${name}" 2>/dev/null \
        | tr -s '\n' ' ' | tail -c 300)"
      say "${name} is unhealthy - restarting it. Last checks: ${why:-<none>}"
      if docker restart -t "${budget}" "${name}" >/dev/null; then
        RESTARTS=$((RESTARTS + 1))
        ga_put restarts "${RESTARTS}"
        ga_put last-restart "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${name}"
        health=restarted
      else
        say "could not restart ${name}"
      fi
    fi
    ga_put "${label}-image" "${image}"
    ga_put "${label}-health" "${health}"
  done
  ga_put heartbeat "$(date +%s)"
}

if [ "${WATCHDOG_ONCE:-}" = 1 ]; then
  pass
  exit 0
fi
say "watching hexera-worker and hexera-geometry-worker every ${WATCHDOG_INTERVAL_SECONDS}s"
while [ ! -e "${SHUTDOWN_MARK}" ]; do
  pass
  sleep "${WATCHDOG_INTERVAL_SECONDS}"
done
