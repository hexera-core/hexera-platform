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


#: THE RULE (docs/RULES.md CFDREQ-28, rulebook 112): a run that already holds a kept mesh hands it back inside its
#: own time - the next engine's native run ends by the run's anchored deadline less HANDBACK_RESERVE_S, never at a
#: machine's wall past it (abc 00025560: cfMesh ran into the wall clock and the kept mesh was lost).
HANDBACK_RULE = "CFDREQ-28"
#: What handing the kept mesh back takes after the stop: the executor, the review, the exports and the upload.
HANDBACK_RESERVE_S = 1800
#: The least a held native run is given (it still starts, and is stopped as a guard trip if it cannot finish).
HANDBACK_MIN_S = 60
#: The workspace file that holds the hand-back time (written by the builder when the run holds a kept mesh).
HANDBACK_FILE = "handback_by.json"


def hold_for_handback(workspace, by_epoch: float | None) -> None:
    """Write (or, with None, remove) the time by which this attempt's native runs must end so a kept mesh can be
    handed back (CFDREQ-28). Never raises."""
    import json
    from pathlib import Path
    try:
        p = Path(workspace) / HANDBACK_FILE
        if by_epoch is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps({"by_epoch": float(by_epoch), "rule": HANDBACK_RULE}), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def handback_cap(workspace, *, now: float | None = None) -> int | None:
    """The seconds a native run started now may take so the kept mesh is still handed back (CFDREQ-28): None when
    the run holds no kept mesh (no HANDBACK_FILE)."""
    import json
    import time
    from pathlib import Path
    try:
        by = float(json.loads((Path(workspace) / HANDBACK_FILE).read_text(encoding="utf-8"))["by_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return max(HANDBACK_MIN_S, int(by - (time.time() if now is None else now)))


def wall_for(workspace) -> int:
    """The wall for the run written in `workspace` (its mesh_machine.json, if any) - no later than the kept mesh's
    hand-back time when the run holds one (CFDREQ-28, handback_cap)."""
    from meshpipeline.contracts import mesh_machine
    wall = wall_s(mesh_machine.read(workspace))
    held = handback_cap(workspace)
    return wall if held is None else min(wall, held)


def held_short_of_the_wall(workspace) -> bool:
    """The run's cap is the kept mesh's hand-back time, not the machine's wall: a run stopped there is out of the
    run's time, and moving it to a bigger machine would only spend more (CFDREQ-28)."""
    from meshpipeline.contracts import mesh_machine
    held = handback_cap(workspace)
    return held is not None and held < wall_s(mesh_machine.read(workspace))


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
        held = held_short_of_the_wall(workspace)
        result = run_once(wall_for(workspace))
        if held and stopped_at_the_wall(result):
            # CFDREQ-28: stopped at the kept mesh's hand-back time - the run's time, not the machine's wall
            return result
        if isinstance(result, dict) and result.get("out_of_memory"):
            # RUN-3: stopped before the machine ran out of memory - the SAME case one machine up that
            # also holds what it used (nothing coarsened); None on the largest, and it is handed back
            peak = result.get("used_gib")
            moved = MM.step_up(MM.read(workspace), measured_gib=float(peak) / 0.9 if peak else None)
            rule, decided = "RUN-3", "step up"
        elif stopped_at_the_wall(result):
            moved, rule, decided = MM.off_the_wall(MM.read(workspace)), "RUN-5", "off the wall"
        else:
            return result
        if moved is None:
            return result
        MM.write(workspace, moved)
        log.warning("%s: %s - the same case runs again on a %s VM (%s)", engine,
                    "out of memory" if rule == "RUN-3" else "still working at the Cloud Run wall",
                    moved.get("machine"), rule)
        try:
            from meshpipeline.contracts import decision_record as _dr
            _dr.cite(str(job_id or ""), stage=f"{engine}_machine", decided=decided,
                     value=moved.get("machine"), rule=rule, inputs={"engine": engine},
                     words=str(moved.get("words") or ""))
        except Exception:  # noqa: BLE001 - the why record never stops a mesh
            log.debug("%s not cited", rule, exc_info=True)


__all__ = ["CLOUD_RUN_MARGIN_S", "CLOUD_RUN_TASK_TIMEOUT_S", "CLOUD_RUN_WALL_S", "HANDBACK_FILE", "HANDBACK_MIN_S",
           "HANDBACK_RESERVE_S", "HANDBACK_RULE", "STALL_WINDOW_S", "VM_WALL_S", "handback_cap",
           "held_short_of_the_wall", "hold_for_handback", "run_off_the_wall", "stopped_at_the_wall", "wall_for",
           "wall_s"]
