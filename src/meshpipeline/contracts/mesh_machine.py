# Responsibility: Say WHERE a mesh runs - the Cloud Run mesh job, or a Compute Engine VM sized to fit - from the memory the plan needs, before anything is meshed; and one size up when a run still runs out of memory.
# Boundaries: engine-neutral and pure: no provider call. A driver writes the decision into its workspace; the mesh executor (adapters/mesh_execution) reads it and dispatches there.
# Collaborates with: engines/fea_driver.py (writes it from the FEA memory model), adapters/mesh_execution/cloud_run_client.py and gce_vm.py (read it, run there), docs/RULES.md RUN-1..4.
"""THE MACHINE A MESH RUNS ON (Areen, 24 Oct: a bigger machine replaces shrinking a mesh to fit).

The plan is sized by the accuracy rules alone. Its memory need is then routed:

  * it fits the Cloud Run mesh job (its RAM holds the mesher AND the in-memory workspace the job
    writes its files to) -> Cloud Run, as before;
  * it does not -> a Compute Engine VM, the smallest of MACHINES whose memory holds it (a VM's
    workspace is on disk, so only the mesher counts);
  * not even the largest machine holds it -> the largest, and the plan coarsens to fit it: the last
    resort, never the first.

If a run still runs out of memory, the SAME plan is run again one machine up (step_up), up to the
largest. The decision travels in the workspace (MACHINE_FILE) with its numbers and its words.
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


__all__ = ["CEILING", "CLOUD_RUN_GIB", "MACHINES", "MACHINE_FILE", "Machine", "ceiling_gib", "choose", "read",
           "step_up", "usable_gib", "write"]
