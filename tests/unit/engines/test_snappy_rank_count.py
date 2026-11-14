# Responsibility: Verify snappyHexMesh takes every core its machine was configured with: the Cloud
# Run job's quota, and on the big-VM route every core of the VM (the container states it as a quota).
from __future__ import annotations

import pytest

from meshpipeline.adapters.mesh_execution import gce_vm
from meshpipeline.engines import determinism as det
from meshpipeline.engines.snappy import native as N


@pytest.fixture
def unpinned(monkeypatch):
    monkeypatch.setattr(det, "_SNAPPY_RANKS", None)
    return monkeypatch


def test_the_cloud_run_job_runs_one_rank_per_cpu_of_its_quota(unpinned):
    unpinned.setattr(N, "_container_cpus", lambda fallback: 8)
    unpinned.setattr(N, "_container_mem_bytes", lambda: 32 * (1 << 30))
    with det.recording() as rec:
        assert N._rank_count() == 8
    assert rec.settings["snappy_ranks"] == {"value": 8,
                                            "basis": "the job's CPU quota (8), at most 64"}
    assert rec.settings["snappy_decomposition"]["value"] == "scotch"


@pytest.mark.parametrize("cpus,gib", [(16, 128), (32, 256), (64, 512)])
def test_a_big_mesh_vm_runs_one_rank_per_core(unpinned, cpus, gib):
    unpinned.setattr(N, "_container_cpus", lambda fallback: cpus)
    unpinned.setattr(N, "_container_mem_bytes", lambda: 0)   # the VM's container has no limit
    assert N._rank_count() == cpus


def test_memory_still_lowers_the_count(unpinned):
    unpinned.setattr(N, "_container_cpus", lambda fallback: 64)
    unpinned.setattr(N, "_container_mem_bytes", lambda: 64 * (1 << 30))
    assert N._rank_count() == 21                            # 64 GiB at 3 GiB a rank


def test_a_pin_still_wins(monkeypatch):
    monkeypatch.setattr(det, "_SNAPPY_RANKS", 1)
    monkeypatch.setattr(N, "_container_cpus", lambda fallback: 8)
    monkeypatch.setattr(N, "_container_mem_bytes", lambda: 32 * (1 << 30))
    assert N._rank_count() == 1


def test_the_vm_container_states_every_core_as_its_quota():
    run = next(line for line in gce_vm.STARTUP_SCRIPT.splitlines()
               if line.startswith("docker run") and "--name mesh" in line)
    assert '--cpus "$(nproc)"' in run, run
