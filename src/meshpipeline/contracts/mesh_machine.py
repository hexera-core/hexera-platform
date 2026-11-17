# Responsibility: Say WHERE a mesh runs - the Cloud Run mesh job, or a Compute Engine VM sized to fit - from the memory the plan needs, or (RUN-9) from a flow plan's cell count past the standard machine's, before anything is meshed; and one size up when a run still runs out of memory.
# Boundaries: engine-neutral and pure: no provider call. A driver writes the decision into its workspace; the mesh executor (adapters/mesh_execution) reads it and dispatches there.
# Collaborates with: engines/fea_driver.py (writes it from the FEA memory model), engines/snappy/drivers.py and engines/cfmesh/cfmesh_runner.py (a user's cell count, RUN-9), adapters/mesh_execution/cloud_run_client.py and gce_vm.py (read it, run there), docs/RULES.md RUN-1..9.
"""THE MACHINE A MESH RUNS ON (Areen, 24 Oct: a bigger machine replaces shrinking a mesh to fit).

The plan is sized by the accuracy rules alone. Its memory need is then routed:

  * it fits the Cloud Run mesh job (its RAM holds the mesher AND the in-memory workspace the job
    writes its files to) -> Cloud Run, as before;
  * it does not -> a Compute Engine VM, the smallest of MACHINES whose memory holds it (a VM's
    workspace is on disk, so only the mesher counts);
  * not even the largest machine holds it -> the largest, and the plan coarsens to fit it: the last
    resort, never the first.

If a run still runs out of memory, the SAME plan is run again one machine up (step_up), up to the
largest. And TIME (RUN-5): the Cloud Run job stops every run at its wall, so a plan planned to pass it
runs on a VM from the start (for_time), and a run that met the wall still working runs again there
(off_the_wall) - a VM has no wall; the progress watchdog stops a stuck mesher on it. The decision travels in the workspace (MACHINE_FILE) with its numbers and its words.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: The workspace file the decision travels in.
MACHINE_FILE = "mesh_machine.json"


@dataclass(frozen=True)
class Machine:
    name: str           # the Compute Engine machine type
    memory_gib: float   # its memory
    cpus: int


#: THE CLOUD RUN MESH JOB'S MEMORY (deploy/gcp MESH_MEMORY, the same on dev and prod).
CLOUD_RUN_GIB = 32.0
#: What one run may use of a machine's memory: the OS, the container runtime and the runner keep the rest.
USABLE_SHARE = 0.9
#: THE VMs, smallest first: memory-optimised, enough cores for the mesher's threads. The last is the
#: CEILING - about 1 TB; a plan needing more is coarsened to fit it (the last resort).
MACHINES: tuple[Machine, ...] = (
    Machine("n2-highmem-8", 64.0, 8),
    Machine("n2-highmem-16", 128.0, 16),
    Machine("n2-highmem-32", 256.0, 32),
    Machine("n2-highmem-64", 512.0, 64),
    Machine("m1-ultramem-40", 961.0, 40),
)
CEILING = MACHINES[-1]


def usable_gib(machine: Machine | None) -> float:
    """The memory one run may use: on Cloud Run or on `machine`."""
    return USABLE_SHARE * (CLOUD_RUN_GIB if machine is None else machine.memory_gib)


def _by_name(name: str | None) -> Machine | None:
    return next((m for m in MACHINES if m.name == name), None)


#: The files a flow mesh writes per cell, with room: a polyMesh is about 185-205 bytes a cell raw
#: (6.7 M cells, 1,236 MB; 107 M cells, 22 GB on dev-areen, 28 Oct), and its logs, records and the
#: archive it goes home in roughly double that.
FLOW_FILE_BYTES_PER_CELL = 400


def choose(*, process_gib: float, workspace_gib: float, why: str = "") -> dict:
    """WHERE A PLAN RUNS, decided before meshing. `process_gib`: the mesher's peak memory (with its
    margin); `workspace_gib`: the files it writes, which Cloud Run keeps in memory too."""
    process, files = max(0.0, float(process_gib)), max(0.0, float(workspace_gib))
    cloud_need = process + files
    if cloud_need <= usable_gib(None):
        return _decision(None, process, files, need=cloud_need,
                         words=(f"It runs on the Cloud Run mesh job: it needs about {cloud_need:.0f} GiB "
                                f"and the job has {CLOUD_RUN_GIB:.0f} GiB."), why=why)
    machine = next((m for m in MACHINES if process <= usable_gib(m)), CEILING)
    over = process > usable_gib(CEILING)
    words = (f"It needs about {cloud_need:.0f} GiB, more than the Cloud Run mesh job's {CLOUD_RUN_GIB:.0f} GiB, "
             f"so it runs on a {machine.name} VM ({machine.memory_gib:.0f} GiB).")
    if over:
        words += (f" That is the largest machine allowed; the mesher needs about {process:.0f} GiB, more than "
                  "it holds, so the plan is coarsened to fit it.")
    return _decision(machine, process, files, need=process, words=words, why=why, over_ceiling=over)


def step_up(previous: dict | None, *, measured_gib: float | None = None) -> dict | None:
    """THE SAME PLAN ONE MACHINE UP, after a run ran out of memory on `previous` (a choose() decision):
    the smallest VM bigger than the one it ran on that also holds `measured_gib` (the peak seen, when
    one was). None when it already ran on the largest."""
    prev = previous or {}
    ran = _by_name(prev.get("machine"))
    floor = usable_gib(ran)
    want = max(floor * 1.0001, float(measured_gib or 0.0))
    nxt = next((m for m in MACHINES if usable_gib(m) > floor and usable_gib(m) >= want), None)
    if nxt is None:
        return None
    where = f"the {ran.name} VM" if ran else "the Cloud Run mesh job"
    words = (f"The mesher ran out of memory on {where}, so the same plan runs again on a {nxt.name} VM "
             f"({nxt.memory_gib:.0f} GiB) - nothing coarsened.")
    return _decision(nxt, float(prev.get("process_gib") or 0.0), float(prev.get("workspace_gib") or 0.0),
                     need=want, words=words, why="out_of_memory", stepped_from=prev.get("machine") or "cloud_run")


def _decision(machine: Machine | None, process: float, files: float, *, need: float, words: str, why: str,
              over_ceiling: bool = False, stepped_from: str = "") -> dict:
    return {"where": "cloud_run" if machine is None else "gce",
            "machine": machine.name if machine else None,
            "memory_gib": CLOUD_RUN_GIB if machine is None else machine.memory_gib,
            "cpus": machine.cpus if machine else None,
            "need_gib": round(need, 2), "process_gib": round(process, 2), "workspace_gib": round(files, 2),
            "over_ceiling": over_ceiling, "why": why, "words": words,
            **({"stepped_from": stepped_from} if stepped_from else {})}


#: RUN-5: how much longer than the planned time (elements at the part's SLOW fill rate) a run must be
#: able to go before the Cloud Run job's wall would stop it. The slow rate is already the slow end of
#: what the lab measured; this covers a part slower still.
TIME_MARGIN = 1.5


def _on_a_vm(previous: dict, machine: Machine, *, words: str, why: str) -> dict:
    return _decision(machine, float(previous.get("process_gib") or 0.0),
                     float(previous.get("workspace_gib") or 0.0),
                     need=float(previous.get("process_gib") or 0.0), words=words, why=why,
                     stepped_from=str(previous.get("machine") or "cloud_run"))


def _smallest_holding(previous: dict) -> Machine:
    process = float(previous.get("process_gib") or 0.0)
    return next((m for m in MACHINES if process <= usable_gib(m)), CEILING)


def for_time(decision: dict, *, planned_s: float | None, wall_s: float) -> dict:
    """RUN-5, BEFORE MESHING: a plan the Cloud Run mesh job would hold in memory but whose planned
    time (with TIME_MARGIN) passes the job's wall (`wall_s`: Cloud Run stops every run there, a limit
    no setting removes) runs on the smallest VM that holds it instead - a VM has no such wall, and the
    progress watchdog (PLAN-fea-13) is what stops a stuck mesher there. Any other decision is returned
    unchanged."""
    if decision.get("where") != "cloud_run" or not planned_s or float(planned_s) * TIME_MARGIN <= float(wall_s):
        return decision
    machine = _smallest_holding(decision)
    words = (f"At this part's slowest measured speed it takes about {float(planned_s) / 60:.0f} minutes, and "
             f"the Cloud Run mesh job stops every run at {float(wall_s) / 60:.0f} minutes, so it runs on a "
             f"{machine.name} VM ({machine.memory_gib:.0f} GiB), which has no such limit.")
    return _on_a_vm(decision, machine, words=words, why="time")


def off_the_wall(decision: dict | None) -> dict | None:
    """RUN-5, AFTER A RUN: a pass that reached the Cloud Run job's wall STILL WORKING (not stalled)
    runs again, the same plan, on the smallest VM that holds it - never ended for a limit it only met
    because of where it ran. None when it did not run on Cloud Run (a VM has no wall to move off)."""
    prev = decision or {"where": "cloud_run"}
    if prev.get("where") != "cloud_run":
        return None
    machine = _smallest_holding(prev)
    words = (f"The mesher was still working when the Cloud Run mesh job's time limit stopped it, so the "
             f"same plan runs again on a {machine.name} VM ({machine.memory_gib:.0f} GiB), which has no "
             "such limit - nothing coarsened.")
    return _on_a_vm(prev, machine, words=words, why="cloud_run_wall")


def write(workspace, decision: dict) -> None:
    (Path(workspace) / MACHINE_FILE).write_text(json.dumps(decision, indent=1, sort_keys=True))


def read(workspace) -> dict | None:
    """The workspace's decision; None when there is none (Cloud Run, as always)."""
    try:
        d = json.loads((Path(workspace) / MACHINE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def ceiling_gib() -> float:
    """What the largest machine lets one mesher use: the plan's last-resort size."""
    return usable_gib(CEILING)


# #
# RUN-9: a flow mesh's CELL COUNT picks its machine (Areen, 14 Nov: "if the person writes 16 M in the notes, move to
# the big machine; if they say 5 M, we don't")
# #

#: A FLOW MESHER'S PEAK MEMORY PER MILLION CELLS, with room (snappyHexMesh, cfMesh): an estimate, not a measurement.
#: snappyHexMesh is usually quoted at 1-2 GB a million cells, more while it adds layers; the standard machine carries
#: its 8 M (settings/policy.CELL_HARD_LIMIT) in 32 GiB with the workspace in that memory too, which allows up to about
#: 3. A run that still needs more is stopped before the machine runs out and goes one machine up (RUN-3, step_up).
FLOW_GIB_PER_MCELL = 2.5


def normal_cells() -> int:
    """The most cells the STANDARD machine (the Cloud Run mesh job) is sized for: settings/policy.CELL_HARD_LIMIT."""
    import meshpipeline.settings.policy as polcfg
    return int(polcfg.CELL_HARD_LIMIT)


def _mcells(n: float) -> str:
    return f"{float(n) / 1e6:.3g} M"


def for_cells(cells: float, *, why: str = "cell count") -> dict:
    """RUN-9: WHERE A FLOW PLAN OF `cells` CELLS RUNS. Up to normal_cells() the standard machine, as always; past it
    the smallest VM whose memory holds the mesher (cells x FLOW_GIB_PER_MCELL), with no question. Past even the
    largest machine: the largest, said (the plan is held to what it carries, ceiling_cells)."""
    n = max(0.0, float(cells or 0.0))
    process, files = n / 1e6 * FLOW_GIB_PER_MCELL, n * FLOW_FILE_BYTES_PER_CELL / 2**30
    if n <= normal_cells():
        return _decision(None, process, files, need=process + files, why=why,
                         words=f"{_mcells(n)} cells: it runs on the standard machine.")
    machine = next((m for m in MACHINES if process <= usable_gib(m)), CEILING)
    over = process > usable_gib(CEILING)
    words = (f"{_mcells(n)} cells is more than the {_mcells(normal_cells())} our standard machine is sized for, so it "
             f"runs on a {machine.name} VM ({machine.memory_gib:.0f} GiB).")
    if over:
        words += f" That is the largest machine we run; it carries about {_mcells(ceiling_cells())} cells."
    return _decision(machine, process, files, need=process, words=words, why=why, over_ceiling=over)


def cells_carried(decision: dict | None) -> int:
    """The most flow cells the machine of `decision` carries: the standard machine's normal_cells() on Cloud Run (or no
    decision), else what the VM's memory holds at FLOW_GIB_PER_MCELL - never less than the standard machine."""
    m = _by_name((decision or {}).get("machine")) if (decision or {}).get("where") == "gce" else None
    if m is None:
        return normal_cells()
    return max(normal_cells(), int(usable_gib(m) / FLOW_GIB_PER_MCELL * 1e6))


def ceiling_cells() -> int:
    """The most flow cells the largest machine we run carries: the cap on any cell count, the user's included."""
    return max(normal_cells(), int(usable_gib(CEILING) / FLOW_GIB_PER_MCELL * 1e6))


def route_cells(workspace, cells: float, *, why: str = "cell count") -> dict | None:
    """RUN-9 BEFORE MESHING: a plan of more cells than the standard machine is sized for runs on a VM that carries
    them - written into the workspace, where the mesh executor reads it (adapters/mesh_execution). The decision there
    already when it is as big or bigger (a run that stepped up) is kept. None when the plan fits the standard machine
    (nothing is written: the run is the one it always was)."""
    if float(cells or 0.0) <= normal_cells():
        return None
    want = for_cells(cells, why=why)
    have = read(workspace)
    if have and have.get("where") == "gce" and usable_gib(_by_name(have.get("machine"))) >= usable_gib(
            _by_name(want.get("machine"))):
        return have
    write(workspace, want)
    return want


__all__ = ["CEILING", "CLOUD_RUN_GIB", "FLOW_GIB_PER_MCELL", "MACHINES", "MACHINE_FILE", "TIME_MARGIN", "Machine",
           "ceiling_cells", "ceiling_gib", "cells_carried", "choose", "for_cells", "for_time", "normal_cells",
           "off_the_wall", "read", "route_cells", "step_up", "usable_gib", "write"]
