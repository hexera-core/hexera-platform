# Responsibility: Prove TIME-21's same answer: a sizing run "as the VM runs it" - the check's file packed, sent as the exchange archive, unpacked in a fresh folder and sized there by the mesh container's own entry (runtime/mesh_runner.run_sizing) with another number of counting processes (fewer, and more than the worker's cap: on the VM they follow the machine) - gives the same size choice, the same element counts and the same plans (their digest apart from recorded seconds) as the worker's sizing of the same file in place.
# Boundaries: the real sizing on real assemblies (tests/fea_fixtures.drilled_plates: 3 bodies, two pairs; 32 hot spots on one size option, and 800 on the two-option path the customer takes - about 6 minutes) in child processes, as on the worker; no VM, no bucket - the archive is made and opened by the exchange's own code.
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from meshpipeline.engines import fea_plan_options as PO

#: what a sizing records of its own time (it differs run to run, never the answer)
CLOCK_KEYS = frozenset({"seconds", "fuse_seconds"})


def _without_clock(v):
    if isinstance(v, dict):
        return {k: _without_clock(x) for k, x in v.items() if k not in CLOCK_KEYS}
    if isinstance(v, list):
        return [_without_clock(x) for x in v]
    return v


def _digest(v) -> str:
    return hashlib.sha256(json.dumps(_without_clock(v), sort_keys=True, default=str).encode()).hexdigest()


def _on(cpus: list[int], fn):
    """`fn()` with this process (and the sizing's children, which inherit it) held to `cpus`."""
    was = os.sched_getaffinity(0)
    os.sched_setaffinity(0, set(cpus))
    try:
        return fn()
    finally:
        os.sched_setaffinity(0, was)


def _avail_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024.0 ** 2
    return 0.0


def _as_worker_and_as_vm(tmp_path, shape, *, here: list[int], there: list[int], cap: int | None):
    """The worker's sizing of `shape` in place on `here`, and the VM's (packed, sent as the exchange archive, opened
    in a fresh folder, sized by the container's entry with `cap` counting processes) on `there`."""
    from tests import fea_fixtures as fx

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _tar_dir
    from meshpipeline.engines.fea_size_field import plan_workers
    from meshpipeline.runtime.mesh_runner import run_sizing
    from meshpipeline.sandbox.safe_extract import safe_extract_tar

    check = tmp_path / "check"
    check.mkdir()
    src = fx.write_step(shape, check / "geometry.step")
    kept = check / PO.PLANS_FILE
    told: dict = {}
    on_worker = _on(here, lambda: PO.bounded(src, to_metres=1e-3, budget_s=1700, plans_out=kept, told=told))
    assert on_worker and not told
    workers_here = _on(here, plan_workers)
    packed = PO.pack(src, tmp_path / "packed", to_metres=1e-3, budget_s=1700)
    archive = tmp_path / "input.tar.gz"
    archive.write_bytes(_tar_dir(packed))
    fresh = tmp_path / "vm" / "work"
    safe_extract_tar(path=archive, dest=str(fresh))
    result = json.loads(json.dumps(_on(there, lambda: run_sizing(str(fresh), workers_cap=cap))))
    got = result["fea_sizing"]
    assert result["rc"] == 0 and got["planning_code"] == PO.planning_code()
    assert not (fresh / src.name).exists()                                    # the input does not travel back
    return (on_worker, json.loads(kept.read_text()), workers_here,
            got, json.loads((fresh / PO.PLANS_FILE).read_text()))


def _the_same(on_worker, plans_worker, on_vm, plans_vm) -> None:
    """THE SAME ANSWER: the size choice, both options' element counts, the plans (digest apart from recorded
    seconds) and the key the run reuses them by."""
    assert on_vm == on_worker
    assert on_vm["recommended"] == on_worker["recommended"]
    assert on_vm["full"]["elements"] == on_worker["full"]["elements"] > 0
    assert (on_vm.get("quicker") or {}).get("elements") == (on_worker.get("quicker") or {}).get("elements")
    assert _digest(plans_vm) == _digest(plans_worker)
    assert plans_vm["key"] == plans_worker["key"]


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="needs CPU affinity (Linux)")
def test_a_sizing_run_as_the_vm_runs_it_gives_the_workers_answer(tmp_path):
    from tests import fea_fixtures as fx

    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 2:
        pytest.skip("needs two CPUs to size with two process counts")
    here = cpus[:min(4, len(cpus) - 1)]  # always fewer than the VM's count, also on a 4-CPU runner
    # the VM's count follows the machine: here every CPU this machine has, past the worker's cap of 8 where it can
    on_worker, plans_worker, workers_here, got, plans_vm = _as_worker_and_as_vm(
        tmp_path, fx.drilled_plates(4), here=here, there=cpus, cap=len(cpus))
    assert got["workers"] == len(cpus) != workers_here                        # another number of processes
    _the_same(on_worker, plans_worker, got["options"], plans_vm)


#: drilled_plates(20)'s sizing on the lab laptop (27 Nov): 5.95 GB at its peak with 4 counting processes, 8.36 GB with
#: 8 (12 did not fit 9 GB) - so about this much for the sizing itself and this much more per process
BASE_GIB_P20, CHILD_GIB_P20 = 3.6, 0.6


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="needs CPU affinity (Linux)")
def test_on_the_two_option_path_more_processes_than_the_workers_cap_give_its_answer(tmp_path):
    """drilled_plates(20) takes the customer's path (two options, 9 counts): the worker's sizing on 4 processes, the
    VM's on as many as this machine holds - more than the worker's cap of 8 where it can."""
    from tests import fea_fixtures as fx

    from meshpipeline.engines.fea_size_field import MAX_WORKERS

    cpus = sorted(os.sched_getaffinity(0))
    many = min(len(cpus), int((_avail_gib() - BASE_GIB_P20) // CHILD_GIB_P20))
    if len(cpus) < 5 or many <= 4:
        pytest.skip("needs more than 4 CPUs and the memory for them")
    on_worker, plans_worker, workers_here, got, plans_vm = _as_worker_and_as_vm(
        tmp_path, fx.drilled_plates(20), here=cpus[:4], there=cpus[:many], cap=many)
    assert workers_here == 4 and got["workers"] == many > workers_here
    if len(cpus) > MAX_WORKERS and many > MAX_WORKERS:
        assert got["workers"] > MAX_WORKERS                                   # past the worker's own cap
    assert on_worker["choice"] is True and on_worker.get("quicker")           # the two-option path
    _the_same(on_worker, plans_worker, got["options"], plans_vm)
