# Responsibility: Prove TIME-21's same answer: a sizing run "as the VM runs it" - the check's file packed, sent as the exchange archive, unpacked in a fresh folder and sized there by the mesh container's own entry (runtime/mesh_runner.run_sizing) with another number of counting processes - gives the same size choice, the same element counts and the same plans (their digest apart from recorded seconds) as the worker's sizing of the same file in place.
# Boundaries: the real sizing on a real assembly (tests/fea_fixtures.drilled_plates: 3 bodies, 32 hot spots, two pairs) in child processes, as on the worker; no VM, no bucket - the archive is made and opened by the exchange's own code.
from __future__ import annotations

import hashlib
import json
import os

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


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="needs CPU affinity (Linux)")
def test_a_sizing_run_as_the_vm_runs_it_gives_the_workers_answer(tmp_path):
    from tests import fea_fixtures as fx

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _tar_dir
    from meshpipeline.engines.fea_size_field import plan_workers
    from meshpipeline.runtime.mesh_runner import run_sizing
    from meshpipeline.sandbox.safe_extract import safe_extract_tar

    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 2:
        pytest.skip("needs two CPUs to size with two process counts")
    here, there = (cpus[:4], cpus[:2]) if len(cpus) >= 4 else (cpus[:2], cpus[:1])
    check = tmp_path / "check"
    check.mkdir()
    src = fx.write_step(fx.drilled_plates(4), check / "geometry.step")

    # THE WORKER: the check's own sizing of its canonical file, in place
    kept = check / PO.PLANS_FILE
    told: dict = {}
    on_worker = _on(here, lambda: PO.bounded(src, to_metres=1e-3, budget_s=900, plans_out=kept, told=told))
    assert on_worker and not told
    workers_here = _on(here, plan_workers)

    # THE VM: packed, sent as the exchange archive, opened in a fresh folder, sized by the container's entry
    packed = PO.pack(src, tmp_path / "packed", to_metres=1e-3, budget_s=900)
    archive = tmp_path / "input.tar.gz"
    archive.write_bytes(_tar_dir(packed))
    fresh = tmp_path / "vm" / "work"
    safe_extract_tar(path=archive, dest=str(fresh))
    result = json.loads(json.dumps(_on(there, lambda: run_sizing(str(fresh)))))
    got = result["fea_sizing"]
    assert result["rc"] == 0 and got["workers"] != workers_here               # another number of processes
    assert got["planning_code"] == PO.planning_code()
    assert not (fresh / src.name).exists()                                    # the input does not travel back

    # THE SAME ANSWER: the size choice, its element counts, the plans (digest apart from recorded seconds)
    on_vm = got["options"]
    assert on_vm == on_worker
    assert on_vm["recommended"] == on_worker["recommended"]
    assert on_vm["full"]["elements"] == on_worker["full"]["elements"] > 0
    plans_vm = json.loads((fresh / PO.PLANS_FILE).read_text())
    plans_worker = json.loads(kept.read_text())
    assert _digest(plans_vm) == _digest(plans_worker)
    assert plans_vm["key"] == plans_worker["key"]                             # the run reuses them alike
