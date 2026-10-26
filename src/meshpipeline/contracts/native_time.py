# Responsibility: Say how long a native mesher may run and when it is stuck - for EVERY engine:
# the machine's hard wall, and the stall window after which a run with no progress is stopped.
# Boundaries: numbers, pure helpers and the one RUN-5 re-run loop every watched engine shares; the
# watching is sandbox/safe_exec.run_guarded(stall_s=), the routing is contracts/mesh_machine.
"""NO GUESSED TIME LIMITS (Areen, 24 Oct for FEA; 25 Oct for CFD). A native mesh run keeps going
while it shows progress - its log growing, a new stage, new cells - and is stopped only when it has
made NO progress for STALL_WINDOW_S, or at the hard wall of the machine it runs on. Before this the
CFD engines ran under guessed caps (snappy 2,400 s per mesh, cfMesh 1,200-3,000 s, every OpenFOAM
command 1,500 s; then VMTK 3,000 s, multi-region snappy 3,000 s, gmsh CFD 1,500 s, snap-grid
3,600 s), and a run that was merely slow was cut and re-planned smaller or moved to another engine."""
from __future__ import annotations

from collections.abc import Callable

#: The quiet after which a native mesher is stopped as stuck: no new output, no new stage. The
#: same window as the FEA mesher's (engines/gmsh/gmsh_runner.FEA_STALL_WINDOW_S).
STALL_WINDOW_S = 900

#: THE CLOUD RUN MESH JOB'S WALL: Cloud Run stops every run at its task timeout (deploy/gcp
#: MESH_TIMEOUT_SECONDS 14,400 s) - a platform limit - less this margin to hand the result back.
CLOUD_RUN_TASK_TIMEOUT_S = 14_400
CLOUD_RUN_MARGIN_S = 600
CLOUD_RUN_WALL_S = CLOUD_RUN_TASK_TIMEOUT_S - CLOUD_RUN_MARGIN_S

#: A VM HAS NO WALL; this is only the backstop for a mesher that keeps printing but never ends,
#: under the worker task's own 24-hour limit (adapters/pipeline_execution/celery.py).
VM_WALL_S = 20 * 3600


def wall_s(machine: dict | None = None) -> int:
    """The hard wall of the machine a run is on: a VM's (contracts/mesh_machine decision
    where="gce") or the Cloud Run job's (no decision, or where="cloud_run")."""
    return VM_WALL_S if (machine or {}).get("where") == "gce" else CLOUD_RUN_WALL_S


def wall_for(workspace) -> int:
    """The wall for the run written in `workspace` (its mesh_machine.json, if any)."""
    from meshpipeline.contracts import mesh_machine
    return wall_s(mesh_machine.read(workspace))


def stopped_at_the_wall(result) -> bool:
    """A watched run that came back stopped at its machine's wall STILL WORKING: timed out, and not
    because it stalled (StalledExpired - a stuck run says `stalled`)."""
    return isinstance(result, dict) and bool(result.get("timed_out")) and not result.get("stalled")


def run_off_the_wall(workspace, run_once: Callable[[int], dict], *, engine: str, job_id: str = "") -> dict:
    """RUN-5 AFTER A RUN, for any watched engine: `run_once(cap)` runs the case written in `workspace`
    with the wall of the machine it is on as its cap; a run that comes back stopped at the Cloud Run
    job's wall still working runs again, the same case, on the smallest VM that holds it (a run of its
    own - the machine joins its identity, application/native_submission) - nothing coarsened. What
    comes back is then finished, STUCK (`stalled`: no sign of life for STALL_WINDOW_S), or stopped at
    even a VM's wall - and only those are the caller's to answer (a coarser case, another engine)."""
    import logging

    from meshpipeline.contracts import mesh_machine as MM
    log = logging.getLogger(__name__)
    while True:
        result = run_once(wall_for(workspace))
        if not stopped_at_the_wall(result):
            return result
        moved = MM.off_the_wall(MM.read(workspace))
        if moved is None:
            return result
        MM.write(workspace, moved)
        log.warning("%s: still working at the Cloud Run wall - the same case runs again on a %s VM "
                    "(RUN-5)", engine, moved.get("machine"))
        try:
            from meshpipeline.contracts import decision_record as _dr
            _dr.cite(str(job_id or ""), stage=f"{engine}_machine", decided="off the wall",
                     value=moved.get("machine"), rule="RUN-5", inputs={"engine": engine},
                     words=str(moved.get("words") or ""))
        except Exception:  # noqa: BLE001 - the why record never stops a mesh
            log.debug("RUN-5 not cited", exc_info=True)


__all__ = ["CLOUD_RUN_MARGIN_S", "CLOUD_RUN_TASK_TIMEOUT_S", "CLOUD_RUN_WALL_S", "STALL_WINDOW_S",
           "VM_WALL_S", "run_off_the_wall", "stopped_at_the_wall", "wall_for", "wall_s"]
