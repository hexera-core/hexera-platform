# Responsibility: Pin RUN-10 - a big FEA plan runs on the compute-optimised speed machine (c2d-standard-32, kept apart
# from the memory ladder) even when it fits the Cloud Run job's memory (contracts/mesh_machine.for_time), the memory
# rule still wins when the plan needs more memory than the speed machine has, a VM that cannot be started falls back
# c2d -> n2-highmem-16 -> the job (told) for a plan that fits it, the HXT fill and surface workers never run more than
# 32 threads (MESH-25 / MESH-23), and the plan step's estimate row names the same machine the run uses, with its time
# at the measured speed-up of that machine's threads.
# Boundaries: pure apart from the mesh client's exchange, which runs against an in-memory bucket (as
# test_mesh_on_a_vm); nothing is meshed, no cloud call is made.
from __future__ import annotations

import hashlib

import pytest
from tests.unit.engines.test_mesh_on_a_vm import remote  # noqa: F401 - the in-memory exchange fixture

from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import gce_vm as G
from meshpipeline.contracts import mesh_machine as MM
from meshpipeline.contracts import mesh_progress
from meshpipeline.contracts import narration as N
from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate
from meshpipeline.engines import determinism as D
from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan_options as PO
from meshpipeline.engines import fea_tetwild as TW
from meshpipeline.engines.gmsh import fea_adapter as FA

WALL = FD.ceiling_for(None)


def _run_machine(elements: int, *, rate: float = FD.FILL_RATES["moderate"]) -> dict:
    """The machine as the run decides it (fea_driver.drive): memory, then time and speed."""
    process, files = FD.memory_need(float(elements))
    memory = MM.choose(process_gib=process, workspace_gib=files, why="memory model")
    plan = {"estimate": {"elements": elements}}
    return MM.for_time(memory, planned_s=FD.planned_seconds(plan, rate), wall_s=WALL, elements=float(elements))


def test_the_speed_machine_is_compute_optimised_and_not_on_the_memory_ladder():
    fast = MM.speed_machine()
    assert (fast.name, fast.cpus, fast.cores, fast.memory_gib) == ("c2d-standard-32", 32, 16, 128.0)
    assert fast.name == MM.SPEED_MACHINE_NAME and fast not in MM.MACHINES
    assert all(m.name.startswith(("n2-highmem", "m1-")) for m in MM.MACHINES)
    # memory alone never picks it, whatever the need
    for gib in (1.0, 40.0, 100.0, 200.0, 2000.0):
        assert MM.choose(process_gib=gib, workspace_gib=0.0)["machine"] != fast.name


def test_the_owners_857k_plan_fits_the_job_but_runs_on_the_faster_machine():
    process, files = FD.memory_need(857_000.0)
    memory = MM.choose(process_gib=process, workspace_gib=files)
    assert memory["where"] == "cloud_run"                         # it fits 32 GiB...
    got = _run_machine(857_000)
    assert got["where"] == "gce" and got["machine"] == "c2d-standard-32" and got["cpus"] == 32
    assert got["why"] == "speed" and got["cloud_fits"] is True
    assert "c2d" not in got["words"] and "16 cores" in got["words"]
    assert N.machine_words(got) == "Meshing on a faster machine (16 cores)"


def test_a_100k_plan_stays_on_the_cloud_run_job():
    got = _run_machine(100_000)
    assert got["where"] == "cloud_run" and got["machine"] is None
    assert N.machine_words(got) == "Meshing on the standard machine"


def test_the_threshold_is_at_or_above():
    assert _run_machine(MM.SPEED_ELEMENTS)["machine"] == MM.SPEED_MACHINE_NAME
    assert _run_machine(MM.SPEED_ELEMENTS - 1)["where"] == "cloud_run"
    # the flow engines pass no element count: no speed rule for them
    cloud = MM.choose(process_gib=1.0, workspace_gib=1.0)
    assert MM.for_time(cloud, planned_s=60.0, wall_s=WALL) is cloud


def test_the_memory_rule_wins_when_the_plan_needs_more_than_the_speed_machine_holds():
    # 40 M elements: about 240 GiB of mesher - past the speed machine's 115 GiB, so the memory ladder's machine
    got = _run_machine(40_000_000)
    assert got["machine"] == "n2-highmem-64" and got["cpus"] == 64 and got["why"] != "speed"
    assert FD.memory_need(4e7)[0] > MM.usable_gib(MM.speed_machine())
    # a plan that needs a VM by memory, but one the speed machine's memory holds, goes to the speed machine
    mid = _run_machine(10_000_000)
    assert MM.choose(process_gib=FD.memory_need(1e7)[0], workspace_gib=0.0)["machine"] == "n2-highmem-16"
    assert mid["machine"] == "c2d-standard-32" and mid["why"] == "speed" and not mid.get("cloud_fits")
    # just past the speed machine's usable memory: the ladder's next machine, never the speed machine
    over = MM.choose(process_gib=MM.usable_gib(MM.speed_machine()) + 0.5, workspace_gib=0.0)
    assert MM.for_time(over, planned_s=None, wall_s=WALL, elements=5e6)["machine"] == "n2-highmem-32"
    # out of memory on the speed machine: the next ladder machine with more memory
    assert MM.step_up(_run_machine(857_000))["machine"] == "n2-highmem-32"


def test_the_fallback_order_is_c2d_then_n2_highmem_16_then_the_job():
    fast = _run_machine(857_000)
    second = MM.fallback(fast)
    assert second["where"] == "gce" and second["machine"] == "n2-highmem-16" and second["why"] == "speed_fallback"
    assert second["cloud_fits"] is True and "could not be started" in second["words"]
    assert N.machine_words(second) == "Meshing on a faster machine (8 cores)"
    third = MM.fallback(second)
    assert third["where"] == "cloud_run" and third["why"] == "vm_unavailable"
    assert "standard machine" in third["words"]
    assert MM.fallback(third) is None
    # a plan that needs a VM's memory: the n2-highmem-16 rung when it holds it, never the job
    mid = _run_machine(10_000_000)
    assert MM.fallback(mid)["machine"] == "n2-highmem-16" and MM.fallback(MM.fallback(mid)) is None
    assert MM.fallback(_run_machine(40_000_000)) is None           # needs the VM's memory: no fallback
    assert MM.fallback(_run_machine(100_000)) is None              # already on the job


def _boom(**_kw):
    raise RuntimeError("no c2d-standard-32 VM could be started (HTTP 403 QUOTA_EXCEEDED in us-central1-a)")


def _ws(tmp_path, decision):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    MM.write(ws, decision)
    return ws


def test_a_speed_machine_that_cannot_be_started_runs_on_the_next_fast_vm(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = _ws(tmp_path, _run_machine(857_000))
    tried: list = []

    def vm(**kw):
        tried.append(kw["decision"]["machine"])
        if kw["decision"]["machine"] == MM.SPEED_MACHINE_NAME:
            _boom()
        return real(**kw)
    real = G.trigger_vm                               # the fixture's in-memory VM
    monkeypatch.setattr(G, "trigger_vm", vm)
    said: list = []
    monkeypatch.setattr(mesh_progress, "report", lambda stage: said.append(stage))
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=72_000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert out["rc"] == 0 and tried == ["c2d-standard-32", "n2-highmem-16"] and not remote["job"]
    assert out["ran_on"]["machine"] == "n2-highmem-16" and out["ran_on"]["why"] == "speed_fallback"
    assert remote["vm"][0]["timeout"] == 72_000                    # a VM has no wall: the timeout stands
    assert any(s.get("key") == "machine_fallback_vm" for s in said)
    assert N.stage_words("machine_fallback_vm")[0] == "machine"


def test_neither_vm_started_falls_back_to_the_job_and_says_so(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = _ws(tmp_path, _run_machine(857_000))
    tried: list = []
    monkeypatch.setattr(G, "trigger_vm", lambda **kw: tried.append(kw["decision"]["machine"]) or _boom())
    said: list = []
    monkeypatch.setattr(mesh_progress, "report", lambda stage: said.append(stage))
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=72_000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert tried == ["c2d-standard-32", "n2-highmem-16"]
    assert out["rc"] == 0 and len(remote["job"]) == 1 and not remote["deleted"]
    assert out["ran_on"]["where"] == "cloud_run" and out["ran_on"]["why"] == "vm_unavailable"
    # started with the job's own wall, waited for as a job run
    from meshpipeline.contracts.native_time import CLOUD_RUN_WALL_S
    assert remote["job"][0]["timeout"] == CLOUD_RUN_WALL_S and remote["waited"] == [CLOUD_RUN_WALL_S + 600]
    assert [s.get("key") for s in said if "fallback" in str(s.get("key"))] == ["machine_fallback_vm",
                                                                                "machine_fallback"]
    assert N.stage_words("machine_fallback")[0] == "machine"


def test_a_vm_the_plan_needs_for_memory_is_never_fallen_back_to_the_job(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = _ws(tmp_path, _run_machine(10_000_000))
    monkeypatch.setattr(G, "trigger_vm", _boom)
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=1000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert out["rc"] != 0 and not remote["job"]


def test_an_indeterminate_vm_request_is_never_fallen_back_from(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = _ws(tmp_path, _run_machine(857_000))

    def lost(**_kw):
        raise SubmissionIndeterminate("the VM request failed in transport; it may exist")
    monkeypatch.setattr(G, "trigger_vm", lost)
    with pytest.raises(SubmissionIndeterminate):
        C.run_mesh_remote(ws, engine="gmsh", timeout=1000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert not remote["job"]


def test_the_driver_takes_the_machine_the_pass_really_ran_on(tmp_path):
    vm = _run_machine(857_000)
    second = MM.fallback(vm)
    got = FD._fell_back(tmp_path, "", 1, vm, {"rc": 0, "ran_on": second})
    assert got["machine"] == "n2-highmem-16" and MM.read(tmp_path)["why"] == "speed_fallback"
    third = MM.fallback(second)
    got = FD._fell_back(tmp_path, "", 1, second, {"rc": 0, "ran_on": third})
    assert got["where"] == "cloud_run" and MM.read(tmp_path)["why"] == "vm_unavailable"
    assert FD._fell_back(tmp_path, "", 1, vm, {"rc": 0, "ran_on": vm}) is vm


def test_the_fill_and_surface_workers_never_run_more_than_32_threads(monkeypatch):
    # MESH-25 / MESH-23: a memory-picked 64-vCPU machine still uses 32 (64 measured slower than 32)
    assert D.MAX_GMSH_THREADS == 32
    monkeypatch.setattr(D, "_GMSH_THREADS", None)
    monkeypatch.setattr(D, "job_cpus", lambda: 64)
    assert FA.mesh_threads({}) == 32 and FA.mesh_threads({"threads": 0}) == 32
    assert FA.mesh_threads({"threads": 64}) == 32                  # the plan's own count is held to it too
    monkeypatch.setattr(D, "job_cpus", lambda: 16)
    assert FA.mesh_threads({}) == 16 and FA.mesh_threads({"threads": 4}) == 4


def test_the_time_estimate_uses_the_measured_whole_run_speed_up():
    assert FD.speedup_on(8) == 1.0 and FD.speedup_on(4) == 1.0
    assert FD.speedup_on(16) == pytest.approx(1.2) and FD.speedup_on(32) == pytest.approx(1.3)
    assert FD.speedup_on(64) == pytest.approx(1.3)                 # held to 32 threads (MESH-25)
    assert 1.0 < FD.speedup_on(12) < 1.2 < FD.speedup_on(24) < 1.3


def test_the_estimate_row_names_the_machine_the_run_uses_and_its_time_on_those_cores():
    record = {"faces": {f"Face_{i}": {} for i in range(60)}, "hot_spots": []}       # a moderate part
    rate, _cls = FD.fill_rate(record)
    for n in (100_000, 857_000, 10_000_000, 40_000_000):
        plan = {"estimate": {"elements": n}, "zones": []}
        r = PO.row(record, plan, PO.FULL)
        run = _run_machine(n, rate=rate)
        assert (r["where"], r["machine"], r["cpus"]) == (run["where"], run["machine"], MM.cpus_of(run))
    r = PO.row(record, {"estimate": {"elements": 857_000}, "zones": []}, PO.FULL)
    eight = FD.planned_seconds({"estimate": {"elements": 857_000}}, rate)
    assert r["minutes"] == int(-(-eight / 1.3 // 60))             # the measured 1.3x of 32 threads against 8
    assert r["fits_standard"] is True and r["standard"] is False
    assert PO._machine_words(r) == "on a faster machine (16 cores)"


def test_the_last_rung_runs_on_the_speed_machine_too():
    process, files = FD.memory_need(857_000.0)
    got = TW.rung_machine(857_000.0, process, files, margin=FD.MEMORY_MARGIN)
    assert got["machine"] == MM.SPEED_MACHINE_NAME and got["ftetwild"]["workers"] == 32
    small = TW.rung_machine(50_000.0, *FD.memory_need(50_000.0), margin=FD.MEMORY_MARGIN)
    assert small["where"] == "cloud_run"
