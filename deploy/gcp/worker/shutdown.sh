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
# WHY THE BUDGET IS 75 SECONDS. A VM being deleted gets about ninety seconds before Compute Engine
# stops it regardless (graceful shutdown with a longer window cannot be enabled on instances in a
# managed instance group), and this script has to finish inside that. The hand-back needs a few
# seconds; the rest is headroom for a graph step that is slow to unwind. `docker stop` gives up with
# SIGKILL at the end of its timeout, and a job still unreleased then is re-run once by the reaper.
#
# BOTH CONTAINERS, AT ONCE. The geometry-check worker only ever holds seconds of work, so it gets a
# shorter budget, and stopping it in parallel costs the simulation worker nothing.
#
# The same stop timeouts are set on the containers themselves (startup.sh), because docker's own
# shutdown on a stopping VM may reach the containers before this script does - either way the job
# gets its SIGTERM and its time.
set -uo pipefail

docker stop --time 75 hexera-worker &
docker stop --time 30 hexera-geometry-worker &
wait
