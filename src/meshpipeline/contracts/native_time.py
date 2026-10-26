# Responsibility: Say how long a native mesher may run and when it is stuck - for EVERY engine:
# the machine's hard wall, and the stall window after which a run with no progress is stopped.
# Boundaries: numbers and one pure helper; the watching is sandbox/safe_exec.run_guarded(stall_s=),
# the routing is contracts/mesh_machine.
"""NO GUESSED TIME LIMITS (Areen, 24 Oct for FEA; 25 Oct for CFD). A native mesh run keeps going
while it shows progress - its log growing, a new stage, new cells - and is stopped only when it has
made NO progress for STALL_WINDOW_S, or at the hard wall of the machine it runs on. Before this the
CFD engines ran under guessed caps (snappy 2,400 s per mesh, cfMesh 1,200-3,000 s, every OpenFOAM
command 1,500 s), and a run that was merely slow was cut and re-planned smaller."""
from __future__ import annotations

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


__all__ = ["CLOUD_RUN_MARGIN_S", "CLOUD_RUN_TASK_TIMEOUT_S", "CLOUD_RUN_WALL_S", "STALL_WINDOW_S",
           "VM_WALL_S", "wall_for", "wall_s"]
