# Responsibility: Pin RUN-10 - a big FEA plan runs on more cores even when it fits the Cloud Run job's memory
# (contracts/mesh_machine.for_time), the memory rule still wins when it wants a bigger machine, a VM that cannot be
# started falls back to the job (told) for a plan that fits it, and the plan step's estimate row names the same
# machine the run uses, with its time at that machine's cores.
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
from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan_options as PO
from meshpipeline.engines import fea_tetwild as TW

WALL = FD.ceiling_for(None)


def _run_machine(elements: int, *, rate: float = FD.FILL_RATES["moderate"]) -> dict:
    """The machine as the run decides it (fea_driver.drive): memory, then time and speed."""
    process, files = FD.memory_need(float(elements))
    memory = MM.choose(process_gib=process, workspace_gib=files, why="memory model")
    plan = {"estimate": {"elements": elements}}
    return MM.for_time(memory, planned_s=FD.planned_seconds(plan, rate), wall_s=WALL, elements=float(elements))


def test_the_owners_857k_plan_fits_the_job_but_runs_on_32_cores():
    process, files = FD.memory_need(857_000.0)
    memory = MM.choose(process_gib=process, workspace_gib=files)
    assert memory["where"] == "cloud_run"                         # it fits 32 GiB...
    got = _run_machine(857_000)
    assert got["where"] == "gce" and got["machine"] == "n2-highmem-32" and got["cpus"] == 32   # ...and goes to 32 cores
    assert got["why"] == "speed" and got["cloud_fits"] is True
    assert "32-core" in got["words"] and N.machine_words(got) == "Meshing on a 32-core machine"


def test_a_100k_plan_stays_on_the_cloud_run_job():
    got = _run_machine(100_000)
    assert got["where"] == "cloud_run" and got["machine"] is None
    assert N.machine_words(got) == "Meshing on an 8-core machine"


def test_the_threshold_is_at_or_above():
    assert _run_machine(MM.SPEED_ELEMENTS)["machine"] == MM.SPEED_MACHINE_NAME
    assert _run_machine(MM.SPEED_ELEMENTS - 1)["where"] == "cloud_run"
    # the flow engines pass no element count: no speed rule for them
    cloud = MM.choose(process_gib=1.0, workspace_gib=1.0)
    assert MM.for_time(cloud, planned_s=60.0, wall_s=WALL) is cloud


def test_the_memory_rule_wins_when_it_wants_a_bigger_machine():
    # 40 M elements: about 240 GiB of mesher - past the 32-core machine's memory, so the memory rule's machine
    got = _run_machine(40_000_000)
    assert got["machine"] == "n2-highmem-64" and got["cpus"] == 64
    # a plan that needs a VM by memory but a smaller one than the speed machine goes up to it
    mid = _run_machine(10_000_000)
    assert MM.choose(process_gib=FD.memory_need(1e7)[0], workspace_gib=0.0)["machine"] == "n2-highmem-16"
    assert mid["machine"] == "n2-highmem-32" and not mid.get("cloud_fits")
    # out of memory on the speed machine: one machine up, as before
    assert MM.step_up(_run_machine(857_000))["machine"] == "n2-highmem-64"


def test_the_fallback_is_only_for_a_plan_that_fits_the_job():
    fell = MM.fallback(_run_machine(857_000))
    assert fell["where"] == "cloud_run" and fell["why"] == "vm_unavailable"
    assert "could not be started" in fell["words"] and "8-core" in fell["words"]
    assert MM.fallback(_run_machine(10_000_000)) is None          # needs the VM's memory: no fallback
    assert MM.fallback(_run_machine(100_000)) is None             # already on the job


def _boom(**_kw):
    raise RuntimeError("no n2-highmem-32 VM could be started (HTTP 403 in us-central1-a)")


def test_a_vm_that_cannot_be_started_falls_back_to_the_job_and_says_so(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    MM.write(ws, _run_machine(857_000))
    monkeypatch.setattr(G, "trigger_vm", _boom)
    said: list = []
    monkeypatch.setattr(mesh_progress, "report", lambda stage: said.append(stage))
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=72_000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert out["rc"] == 0 and len(remote["job"]) == 1 and not remote["deleted"]
    assert out["ran_on"]["where"] == "cloud_run" and out["ran_on"]["why"] == "vm_unavailable"
    # started with the job's own wall, waited for as a job run
    from meshpipeline.contracts.native_time import CLOUD_RUN_WALL_S
    assert remote["job"][0]["timeout"] == CLOUD_RUN_WALL_S and remote["waited"] == [CLOUD_RUN_WALL_S + 600]
    assert any(s.get("key") == "machine_fallback" for s in said)
    assert N.stage_words("machine_fallback")[0] == "machine"


def test_a_vm_the_plan_needs_for_memory_is_never_fallen_back_from(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    MM.write(ws, _run_machine(10_000_000))
    monkeypatch.setattr(G, "trigger_vm", _boom)
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=1000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert out["rc"] != 0 and not remote["job"]


def test_an_indeterminate_vm_request_is_never_fallen_back_from(tmp_path, remote, monkeypatch):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    MM.write(ws, _run_machine(857_000))

    def lost(**_kw):
        raise SubmissionIndeterminate("the VM request failed in transport; it may exist")
    monkeypatch.setattr(G, "trigger_vm", lost)
    with pytest.raises(SubmissionIndeterminate):
        C.run_mesh_remote(ws, engine="gmsh", timeout=1000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert not remote["job"]


def test_the_driver_takes_the_machine_the_pass_really_ran_on(tmp_path):
    vm = _run_machine(857_000)
    fell = MM.fallback(vm)
    got = FD._fell_back(tmp_path, "", 1, vm, {"rc": 0, "ran_on": fell})
    assert got["where"] == "cloud_run" and MM.read(tmp_path)["why"] == "vm_unavailable"
    assert FD._fell_back(tmp_path, "", 1, vm, {"rc": 0, "ran_on": vm}) is vm


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
    assert r["minutes"] == int(-(-eight / 2.0 // 60))             # twice the 8-core speed on 32 cores
    assert r["fits_standard"] is True and r["standard"] is False
    assert PO._machine_words(r) == "on a 32-core machine"


def test_the_last_rung_runs_on_the_speed_machine_too():
    process, files = FD.memory_need(857_000.0)
    got = TW.rung_machine(857_000.0, process, files, margin=FD.MEMORY_MARGIN)
    assert got["machine"] == MM.SPEED_MACHINE_NAME and got["ftetwild"]["workers"] == 32
    small = TW.rung_machine(50_000.0, *FD.memory_need(50_000.0), margin=FD.MEMORY_MARGIN)
    assert small["where"] == "cloud_run"
