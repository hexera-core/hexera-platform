#!/bin/bash
# Responsibility: Let a worker VM be deleted without taking its job with it - stop the containers so the running job hands itself back.
# Owns: the order and the time budget of the worker's shutdown.
# Boundaries: it only stops containers; handing the job back is the worker's own code (src/meshpipeline/application/worker_handoff.py).
#
# This is the MIG instance SHUTDOWN script. Compute Engine runs it when the VM is stopped or
# deleted - autoscaler scale-in, a rolling update onto a new template, an operator deleting the
# instance from the admin console. Before it existed nothing ran at all: the VM went down in under
# a minute, the job it was running stopped heartbeating, and the stalled-job reaper failed the job
# about thirty minutes later as "worker lost" (shared dev, 2026-09-29: three of six runs).
#
# WHAT STOPPING THE CONTAINER DOES. `docker stop` sends SIGTERM to celery's main process (PID 1 in
# the container - the entrypoint `exec`s it). Celery stops taking new work at once and marks the
# worker as draining; the job running in its pool process sees the mark within a second, stops its
# graph, releases its lease, puts the job back on the queue and returns - a few seconds in all. The
# next free worker runs the job; the user is told it moved. Only then does the container exit.
#
# WHY THE BUDGET IS WHAT IT IS, AND WHY IT IS NOT A CONSTANT. A VM being DELETED gets about ninety
# seconds before Compute Engine stops it regardless (graceful shutdown with a longer window cannot
# be enabled on instances in a managed instance group), and this script has to finish inside that -
# so 75 seconds, the hand-back plus headroom for a graph step that is slow to unwind.
#
# A RECLAIMED SPOT VM gets about THIRTY, not ninety. A 75-second budget on a Spot fleet is worse
# than a short one: `docker stop` would still be waiting when the machine went away, so the job
# would never be handed back and would instead wait out its 900-second lease to be re-run as
# worker_lost - flakiness that looks like nothing in particular. So the number is decided per
# provisioning model by create-worker-fleet.sh and read from instance metadata here, where it is
# the same value the containers carry as their own --stop-timeout (startup.sh).
#
# The measured drain is about eighteen seconds - the run notices the mark within 1 s, is cancelled,
# and gets 15 s to unwind (application/worker_handoff.py) - which is what makes 25 fit in 30.
#
# `docker stop` gives up with SIGKILL at the end of its timeout, and a job still unreleased then is
# re-run once by the reaper.
#
# BOTH CONTAINERS, AT ONCE. The geometry-check worker only ever holds seconds of work, so it gets a
# shorter budget, and stopping it in parallel costs the simulation worker nothing.
#
# The same stop timeouts are set on the containers themselves (startup.sh), because docker's own
# shutdown on a stopping VM may reach the containers before this script does - either way the job
# gets its SIGTERM and its time.
set -uo pipefail

# THE WATCHDOG FIRST (watchdog.sh): it restarts a container docker calls unhealthy, and a container
# being stopped on purpose must not be started again behind this script's back.
touch /run/hexera-shutting-down
systemctl stop hexera-worker-watchdog >/dev/null 2>&1 || true

# The budget the template was built with. Defaults are the on-demand values, so an instance whose
# template predates this metadata behaves exactly as it did before.
md() { curl -sf -H 'Metadata-Flavor: Google' \
  "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
WORKER_STOP_TIMEOUT="$(md worker-stop-timeout || true)"
GEOMETRY_STOP_TIMEOUT="$(md geometry-stop-timeout || true)"
[[ "${WORKER_STOP_TIMEOUT}" =~ ^[0-9]+$ ]] || WORKER_STOP_TIMEOUT=75
[[ "${GEOMETRY_STOP_TIMEOUT}" =~ ^[0-9]+$ ]] || GEOMETRY_STOP_TIMEOUT=30

docker stop --time "${WORKER_STOP_TIMEOUT}" hexera-worker &
docker stop --time "${GEOMETRY_STOP_TIMEOUT}" hexera-geometry-worker &
wait
