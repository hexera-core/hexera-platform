# Responsibility: Run the maintenance schedule once for a hosted deployment - reap stalled jobs, purge expired uploads, reconcile orphaned objects - then exit.
# Boundaries: a process entry point for the scheduled Cloud Run job (deploy/gcp/scripts/
#             create-maintenance-sweep.sh). Binds only the ports a sweep publishes through; what
#             each sweep does, and why running it twice is harmless, is application/maintenance/.
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable

from meshpipeline.application.maintenance import cleanup as _cleanup
from meshpipeline.application.maintenance import reconcile as _reconcile

_CLI_DOC = ("One-shot maintenance sweep: reap stalled jobs, purge expired uploads, reconcile "
            "orphaned objects, then exit.")

#: WHAT THE HOSTED SCHEDULE RUNS, in order, each named by the Celery task it stands in for.
#:
#: celery_app.py declares these as a beat schedule, and the compose stack runs them that way: a
#: `beat` service enqueues them and `worker-utility` consumes the cleanup queue. A hosted
#: deployment runs NO beat and no cleanup consumer - the fleet is one simulation worker per
#: instance (deploy/gcp/worker/startup.sh) - so this job runs the same functions in-process on a
#: Cloud Scheduler tick. The names are the contract: tests/unit/deploy/test_fleet_schedule_contract.py
#: holds this tuple and ELSEWHERE against the beat schedule, so a task added there without a hosted
#: home is refused rather than silently never run - which is how the reaper went unrun on every
#: hosted deployment until 2026-09-28.
#:
#: THE REAPER FIRST. It is the one with a person waiting on it: a stalled job holds back its
#: organisation's credits (application/spend_gate.py), and the two sweeps behind it only free
#: bytes. Each step is its own unit of work - one that raises is logged and counted, and the next
#: still runs. Looked up at call time, not bound here, so a test can substitute the function.
HOSTED_STEPS: tuple[tuple[str, Callable[[], dict]], ...] = (
    ("tasks.cleanup.reap_stalled_jobs", lambda: _cleanup.reap_stalled_jobs()),
    ("tasks.cleanup.purge_expired_geometry_sources", lambda: _cleanup.purge_expired_geometry_sources()),
    ("tasks.cleanup.reconcile_orphan_artifacts", lambda: _reconcile.reconcile_orphan_artifacts()),
)

#: Beat entries this job does NOT run, each with where it runs instead - or why nowhere hosted.
ELSEWHERE: dict[str, str] = {
    "tasks.billing.report_pending_usage":
        "runtime/meter_sweep.py - its own scheduled job, provisioned only where billing is "
        "configured (deploy/gcp/scripts/create-meter-sweep.sh)",
    "tasks.cleanup.purge_expired_workspaces":
        "compose only. A workspace is a directory on the machine that ran the job; on the fleet "
        "that is the instance's own disk, which goes when the autoscaler replaces the instance. "
        "This job has no such disk, and marking a workspace purged that it never saw would be a lie.",
}


def bind_ports() -> None:
    # THE TWO PORTS A SWEEP PUBLISHES THROUGH, and no more. install_adapters() would also bind the
    # model router, the mesh executor, the search provider and every Redis-backed capability -
    # each another setting this job would need, and another way for it to fail before reaping
    # anything. The database is not a port: persistence reads its settings directly.
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.contracts import event_stream, object_storage

    # The reaper's closing line to any client still streaming a dead job. Best effort inside the
    # reaper: a broker it cannot reach costs the line, never the reap.
    event_stream.set_publisher_factory(JobPublisher)
    # The upload purge and the orphan reconcile delete objects; the reaper never touches the store.
    object_storage.set_object_store(build_object_store())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("meshpipeline.runtime.maintenance_sweep")
    bind_ports()

    steps: dict[str, dict] = {}
    failed: list[str] = []
    for name, step in HOSTED_STEPS:
        try:
            steps[name] = step()
        except Exception as exc:  # noqa: BLE001 - one sweep's failure must not cost the others
            log.exception("%s failed: %s", name, exc)
            failed.append(name)
            steps[name] = {"error": type(exc).__name__}
    print(json.dumps({"steps": steps, "failed": failed}, default=str))
    # RED WHEN ANY STEP RAISED. A sweep that cannot reach the database, or a store credential that
    # stopped working, is a deploy problem and shows in the job's execution list as one - while
    # the steps that did run have already done their work.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
