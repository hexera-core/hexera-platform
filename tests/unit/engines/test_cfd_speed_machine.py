# Responsibility: Pin RUN-13 - a big flow plan (snappyHexMesh's pass budget, cfMesh's run-plan estimate, multi-region
# snappy's thermal plan) of at least SPEED_CELLS planned cells runs on RUN-10's speed machine even when it fits the
# Cloud Run job; memory still wins past the speed machine's memory; the same fallback ladder (c2d -> n2-highmem-16 ->
# the job) with the machine it really ran on written back; the run's machine line says so; and cfMesh and the other
# OpenMP tools take a VM's cores (its CPU quota, at most 32) while the Cloud Run job stays at MESH_THREADS.
# Boundaries: pure apart from the mesh client's exchange, which runs against an in-memory bucket (as
# test_fea_speed_machine); nothing is meshed, no cloud call is made.
from __future__ import annotations

import hashlib

from tests.unit.engines.test_mesh_on_a_vm import remote  # noqa: F401 - the in-memory exchange fixture

from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import gce_vm as G
from meshpipeline.contracts import mesh_machine as MM
from meshpipeline.contracts import narration as N
from meshpipeline.contracts.native_time import CLOUD_RUN_WALL_S
from meshpipeline.engines import determinism as D
from meshpipeline.engines.cfmesh import cfmesh_runner as CF
from meshpipeline.engines.snappy_multiregion import multiregion_runner as MR


def _flow(cells: float) -> dict:
    """A flow plan's machine as route_cells decides it (memory, then speed) - without a workspace."""
    return MM.for_time(MM.for_cells(cells), planned_s=None, wall_s=0.0, cells=float(cells))


def test_the_threshold_is_at_or_above_and_below_it_nothing_changes(tmp_path):
    at = MM.route_cells(tmp_path, MM.SPEED_CELLS)
    assert at["where"] == "gce" and at["machine"] == MM.SPEED_MACHINE_NAME and at["cpus"] == 32
    assert at["why"] == "speed" and at["cloud_fits"] is True
    assert MM.read(tmp_path)["machine"] == MM.SPEED_MACHINE_NAME          # written where the executor reads it
    below = tmp_path / "below"
    below.mkdir()
    assert MM.route_cells(below, MM.SPEED_CELLS - 1) is None and MM.read(below) is None   # the run it always was
    # a 4 M car (the default snappy budget) now goes to the faster machine, told by its cores and cells
    car = _flow(4_000_000)
    assert car["machine"] == MM.SPEED_MACHINE_NAME and "4 M cells" in car["words"] and "16 cores" in car["words"]
    assert "c2d" not in car["words"]
    assert N.machine_words(car) == "Meshing on a faster machine (16 cores)"
    assert N.machine_words(_flow(1_000_000)) == "Meshing on the standard machine"
    # no cell count given: no speed rule (the FEA call is untouched)
    cloud = MM.choose(process_gib=1.0, workspace_gib=1.0)
    assert MM.for_time(cloud, planned_s=60.0, wall_s=CLOUD_RUN_WALL_S) is cloud
    assert MM.for_time(cloud, planned_s=None, wall_s=CLOUD_RUN_WALL_S, cells=MM.SPEED_CELLS - 1) is cloud


def test_memory_still_wins_past_what_the_speed_machine_holds():
    # 10 M cells: past the standard machine (RUN-9 alone would pick n2-highmem-8, 8 vCPUs) - the speed machine holds it
    assert MM.for_cells(10_000_000)["machine"] == "n2-highmem-8"
    mid = _flow(10_000_000)
    assert mid["machine"] == MM.SPEED_MACHINE_NAME and mid["why"] == "speed" and not mid.get("cloud_fits")
    # 50 M cells: 125 GiB of mesher, past the speed machine's usable 115 GiB - the ladder's machine, as today
    big = _flow(50_000_000)
    assert big["machine"] == "n2-highmem-32" and big["why"] != "speed"
    assert MM.cells_carried(mid) > 40_000_000                              # the speed machine carries ~46 M
    # out of memory on the speed machine: the next ladder machine with more memory
    assert MM.step_up(_flow(4_000_000))["machine"] == "n2-highmem-32"


def test_the_fallback_ladder_is_c2d_then_n2_highmem_16_then_the_job():
    fast = _flow(4_000_000)
    second = MM.fallback(fast)
    assert second["machine"] == "n2-highmem-16" and second["why"] == "speed_fallback" and second["cloud_fits"]
    third = MM.fallback(second)
    assert third["where"] == "cloud_run" and third["why"] == "vm_unavailable" and MM.fallback(third) is None
    # a plan that needs a VM's memory: the n2-highmem-16 rung, never the job
    mid = _flow(10_000_000)
    assert MM.fallback(mid)["machine"] == "n2-highmem-16" and MM.fallback(MM.fallback(mid)) is None


def test_a_flow_speed_machine_that_cannot_start_runs_on_the_next_fast_vm(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    MM.route_cells(ws, 4_000_000)
    tried: list = []
    real = G.trigger_vm                                                   # the fixture's in-memory VM

    def vm(**kw):
        tried.append(kw["decision"]["machine"])
        if kw["decision"]["machine"] == MM.SPEED_MACHINE_NAME:
            raise RuntimeError("QUOTA_EXCEEDED: C2D_CPUS")
        return real(**kw)
    monkeypatch.setattr(G, "trigger_vm", vm)
    out = C.run_mesh_remote(ws, engine="snappy", timeout=72_000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert out["rc"] == 0 and tried == ["c2d-standard-32", "n2-highmem-16"] and not remote["job"]
    assert out["ran_on"]["machine"] == "n2-highmem-16" and out["ran_on"]["why"] == "speed_fallback"


def test_the_machine_it_really_ran_on_is_written_back_and_kept(tmp_path):
    fast = MM.route_cells(tmp_path, 4_000_000)
    job = MM.fallback(MM.fallback(fast))
    assert MM.adopt_ran_on(tmp_path, {"rc": 0, "ran_on": job})["why"] == "vm_unavailable"
    assert MM.read(tmp_path)["where"] == "cloud_run"
    # so RUN-5 can move a run still working at the job's wall to a VM (it ran on the job, not the VM)
    assert MM.off_the_wall(MM.read(tmp_path)) is not None
    # and the next pass is not sent to the VM that could not start again
    assert MM.route_cells(tmp_path, 4_000_000)["why"] == "vm_unavailable"
    # a run that ran where it was sent changes nothing
    other = tmp_path / "o"
    other.mkdir()
    sent = MM.route_cells(other, 4_000_000)
    assert MM.adopt_ran_on(other, {"rc": 0, "ran_on": sent}) is None and MM.adopt_ran_on(other, {"rc": 0}) is None
    # a stepped-up machine already there is kept
    up = tmp_path / "up"
    up.mkdir()
    MM.write(up, MM.step_up(sent))
    assert MM.route_cells(up, 4_000_000)["machine"] == "n2-highmem-32"


def test_cfmesh_and_multi_region_plans_route_by_their_cells(tmp_path):
    big = CF.machine_for_plan(tmp_path, {"estimated_cells": 2_000_000, "estimated_seconds": 300.0})
    assert big["machine"] == MM.SPEED_MACHINE_NAME and big["cloud_fits"] and MM.read(tmp_path)["why"] == "speed"
    small = CF.machine_for_plan(tmp_path, {"estimated_cells": 600_000, "estimated_seconds": 100.0})
    assert small["where"] == "cloud_run"
    # the user's own count counts when it is the larger
    asked = CF.machine_for_plan(tmp_path, {"estimated_cells": 600_000, "estimated_seconds": 100.0},
                                user_cells=3_000_000)
    assert asked["machine"] == MM.SPEED_MACHINE_NAME
    assert MR.machine_for_plan(tmp_path, 300.0, cells=2_500_000)["machine"] == MM.SPEED_MACHINE_NAME
    assert MR.machine_for_plan(tmp_path, 300.0, cells=200_000)["where"] == "cloud_run"
    assert MR.machine_for_plan(tmp_path, None)["where"] == "cloud_run"


def test_the_snappy_route_cites_run_13(tmp_path, monkeypatch):
    from meshpipeline.contracts import decision_record as DR
    from meshpipeline.engines.snappy import drivers as SD
    cited: list = []
    monkeypatch.setattr(DR, "cite", lambda job, **kw: cited.append(kw))
    moved = SD._route_big(tmp_path, 4_000_000, job_id="j", attempt=1)
    assert moved["machine"] == MM.SPEED_MACHINE_NAME
    assert cited and cited[0]["rule"] == "RUN-13" and cited[0]["decided"] == "faster machine"
    assert cited[0]["inputs"]["cpus"] == 32
    cited.clear()
    big = tmp_path / "big"
    big.mkdir()
    SD._route_big(big, 60_000_000, job_id="j", attempt=1)                # memory: RUN-9 as before
    assert cited[0]["rule"] == "RUN-9"


def test_cfmesh_threads_follow_a_vms_cores_and_stay_on_the_cloud_run_job(monkeypatch):
    monkeypatch.setattr(D, "MESH_DETERMINISTIC", True)
    monkeypatch.setattr(D, "MESH_THREADS", 8)
    monkeypatch.setattr(D, "_CFMESH_THREADS", None)
    monkeypatch.setattr(D, "cpu_quota", lambda: 32)                       # the speed machine's container
    assert D.threads_for("cfmesh") == 32 and D.native_environment("cfmesh")["OMP_NUM_THREADS"] == "32"
    assert D.threads_for("vmtk") == 32                                    # every OpenMP tool, not only cfMesh
    monkeypatch.setattr(D, "cpu_quota", lambda: 16)                       # the n2-highmem-16 rung
    assert D.threads_for("cfmesh") == 16
    monkeypatch.setattr(D, "cpu_quota", lambda: 64)                       # a memory-picked 64-vCPU VM
    assert D.threads_for("cfmesh") == D.MAX_NATIVE_THREADS == 32
    monkeypatch.setattr(D, "cpu_quota", lambda: 8)                        # the Cloud Run job: as today
    assert D.threads_for("cfmesh") == 8
    monkeypatch.setattr(D, "cpu_quota", lambda: 4)                        # a smaller quota never lowers it
    assert D.threads_for("cfmesh") == 8
    monkeypatch.setattr(D, "cpu_quota", lambda: None)                     # no quota: never the host's cores
    assert D.threads_for("cfmesh") == 8
    # a pin still wins on a VM: 1 = the bit-repeatable serial cfMesh
    monkeypatch.setattr(D, "cpu_quota", lambda: 32)
    monkeypatch.setattr(D, "_CFMESH_THREADS", 1)
    assert D.threads_for("cfmesh") == 1 and D.native_environment("cfmesh")["OMP_NUM_THREADS"] == "1"
    # the stamp says the count it ran with
    monkeypatch.setattr(D, "_CFMESH_THREADS", None)
    assert D.stamp("cfmesh")["threads"]["engine"] == 32


def test_the_settings_help_says_what_the_code_does():
    from pathlib import Path

    from meshpipeline.settings import inventory as I
    text = Path(I.__file__).read_text()
    assert "at most 12" not in text and "CPU quota (at most 64)" in text     # snappy's ranks: RANKS_MAX
