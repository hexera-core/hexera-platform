# Responsibility: Verify the last four CFD engines on fixed clocks - VMTK (3,000 s), multi-region
# snappy (3,000 s a stage), gmsh CFD (1,500 s) and snap-grid (3,600 s) - are watched for progress
# like snappy and cfMesh: each native run is stopped only when STUCK (no sign of life for the stall
# window) or at its machine's wall, a run stopped at the Cloud Run wall still working runs again on a
# VM (RUN-5, run_off_the_wall), and a captured run's output is watched too.
# Boundaries: real subprocesses for the watch (seconds long), stand-ins for the meshers.
from __future__ import annotations

import subprocess
import sys

import pytest

from meshpipeline.contracts import native_time as NT
from meshpipeline.contracts import progress_clock
from meshpipeline.sandbox.safe_exec import StalledExpired, run_guarded

ENGINES = ("vmtk", "snappy_multiregion", "gmsh", "snapgrid")


@pytest.fixture(autouse=True)
def _fresh():
    progress_clock.reset()
    yield
    progress_clock.reset()


def _py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def _stuck(where: str):
    def _raise(args, *a, **k):
        assert k.get("stall_s") == NT.STALL_WINDOW_S, f"{where} is not watched"
        raise StalledExpired(args, k.get("timeout"), silence_s=900.0, ran_s=1200.0)
    return _raise


# A CAPTURED RUN IS WATCHED TOO

def test_a_captured_run_that_goes_quiet_is_stopped_as_stalled_with_its_output():
    with pytest.raises(StalledExpired) as err:
        run_guarded(_py("import sys,time\nprint('start')\nprint('oops', file=sys.stderr)\ntime.sleep(30)"),
                    capture_output=True, text=True, timeout=30, stall_s=0.8, poll_s=0.1)
    assert err.value.stalled
    assert "start" in err.value.stdout and "oops" in err.value.stderr


def test_a_captured_run_still_printing_finishes_and_reads_as_before():
    code = "import sys,time\nfor i in range(6):\n print(i)\n time.sleep(0.3)\nprint('e', file=sys.stderr)"
    proc = run_guarded(_py(code), capture_output=True, text=True, timeout=30, stall_s=1.0, poll_s=0.1)
    assert proc.returncode == 0 and proc.stdout == "0\n1\n2\n3\n4\n5\n" and proc.stderr == "e\n"
    raw = run_guarded(_py("print('b')"), capture_output=True, timeout=30, stall_s=1.0, poll_s=0.1)
    assert raw.stdout == b"b\n"                      # bytes, as a captured run without text gives


def test_a_captured_run_still_moving_at_the_wall_is_a_plain_timeout():
    with pytest.raises(subprocess.TimeoutExpired) as err:
        run_guarded(_py("import time\nwhile True:\n print('x', flush=True)\n time.sleep(0.1)"),
                    capture_output=True, text=True, timeout=1.0, stall_s=5.0, poll_s=0.1)
    assert not getattr(err.value, "stalled", False) and "x" in err.value.stdout


# EVERY ENGINE IS WATCHED, CAPPED ONLY BY ITS MACHINE

@pytest.mark.parametrize("engine", ENGINES)
def test_every_cfd_engine_is_watched(engine):
    from meshpipeline.engines.registry import get_spec
    assert get_spec(engine).run_policy.watched


@pytest.mark.parametrize("engine", ("vmtk", "snappy_multiregion", "gmsh"))
def test_a_builder_engines_run_gets_the_machines_wall_not_its_old_clock(monkeypatch, tmp_path, engine):
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing as M
    from meshpipeline.engines.registry import get_spec

    monkeypatch.setattr(M, "input_contract_rejection", lambda *a, **k: "")
    ws = tmp_path / "ws"
    ws.mkdir()
    for rel in get_spec(engine).run_policy.required_files:
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("x")
    ctx = BuilderToolContext(workspace=ws, geometry=object(), job_id="j", execution_id="e",
                             execution_generation=1, engine=engine, mesh_fidelity="",
                             loop_deadline=progress_clock.model_time() + 120)
    prepared = M.prepare_mesh_run(ctx)
    assert prepared.refusal is None and prepared.cap == NT.CLOUD_RUN_WALL_S


# RUN-5 FOR EVERY WATCHED ENGINE

def _off_the_wall(tmp_path, results):
    from meshpipeline.contracts import mesh_machine as MM
    seen = []

    def _once(cap):
        seen.append((cap, (MM.read(tmp_path) or {}).get("where", "cloud_run")))
        return results[min(len(seen), len(results)) - 1]

    return NT.run_off_the_wall(tmp_path, _once, engine="vmtk", job_id="t"), seen


def test_a_run_still_working_at_the_cloud_run_wall_runs_again_on_a_vm(tmp_path):
    wall, done = {"rc": -1, "timed_out": True}, {"rc": 0, "timed_out": False}
    out, seen = _off_the_wall(tmp_path, [wall, done])
    assert out == done and seen == [(NT.CLOUD_RUN_WALL_S, "cloud_run"), (NT.VM_WALL_S, "gce")]


def test_a_stuck_run_comes_back_at_once(tmp_path):
    stuck = {"rc": -1, "timed_out": True, "stalled": True}
    out, seen = _off_the_wall(tmp_path, [stuck])
    assert out is stuck and len(seen) == 1


def test_a_run_at_a_vms_wall_comes_back(tmp_path):
    wall = {"rc": -1, "timed_out": True}
    out, seen = _off_the_wall(tmp_path, [wall, wall])
    assert out["timed_out"] and [w for _c, w in seen] == ["cloud_run", "gce"]


def test_the_builders_run_mesh_moves_a_run_off_the_wall_and_tells_a_stuck_one_as_stuck(monkeypatch, tmp_path):
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing as M
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import runtime

    caps: list[tuple[int, str]] = []
    results = iter([{"rc": -1, "timed_out": True, "log_tail": "wall"},
                    {"rc": -1, "timed_out": True, "stalled": True, "log_tail": "stuck"}])

    class _Engine:
        name = "vmtk"

        def run_cartesian_mesh(self, workspace, *, timeout, context=None):
            caps.append((timeout, (MM.read(workspace) or {}).get("where", "cloud_run")))
            return next(results)

    monkeypatch.setattr(runtime, "get_engine", lambda name: _Engine())
    ctx = BuilderToolContext(workspace=tmp_path, geometry=object(), job_id="j", engine="vmtk")
    out = M._run_and_judge(ctx, M.PreparedMeshRun(engine="vmtk", cap=NT.CLOUD_RUN_WALL_S))
    assert caps == [(NT.CLOUD_RUN_WALL_S, "cloud_run"), (NT.VM_WALL_S, "gce")]
    assert out["timed_out"] and out["stalled"] and "no progress for 15 min" in out["guidance"]
    assert "too fine" not in out["guidance"]


# EACH ENGINE'S NATIVE RUN SAYS WHEN IT WAS STUCK

def test_a_stuck_vmtk_pype_says_it_stalled(monkeypatch, tmp_path):
    from meshpipeline.engines.vmtk import vmtk_runner as V
    monkeypatch.setattr(V, "run_guarded", _stuck("the vmtk pype"))
    out = V._run_pype(tmp_path, ["vmtk", "vmtkmeshgenerator"], timeout=NT.CLOUD_RUN_WALL_S)
    assert out["timed_out"] and out["stalled"]


def test_a_stuck_multiregion_stage_says_it_stalled(monkeypatch, tmp_path):
    from meshpipeline.engines.snappy_multiregion import native as MR
    monkeypatch.setattr(MR, "run_guarded", _stuck("a multi-region stage"))
    logs: list[str] = []
    st = MR.run_stage(tmp_path, "snappyHexMesh -overwrite", "snappyHexMesh", env={}, bashrc="/x",
                      timeout=NT.CLOUD_RUN_WALL_S, logs=logs)
    assert st.timed_out and st.stalled and "STALLED" in logs[-1]
    out = MR._timed_out(st, "snappyHexMesh -overwrite", logs)
    assert out["timed_out"] and out["stalled"]


def test_the_multiregion_stages_share_one_wall(monkeypatch, tmp_path):
    """Every stage used to get the whole limit again; now they share the machine's wall."""
    from meshpipeline.engines.snappy_multiregion import native as MR
    given: list[float] = []

    def _run(args, *a, timeout=None, **k):
        given.append(float(timeout))
        return subprocess.CompletedProcess(args, 1, "", "")       # the first stage fails: enough

    monkeypatch.setattr(MR, "run_guarded", _run)
    monkeypatch.setattr(MR, "scan_case_dicts", lambda ws: None)
    MR.run_native_build(tmp_path, preflight=lambda ws: None, render_region_properties=lambda r: "",
                        parse_layer_coverage=lambda s: {}, bashrc="/x", timeout=1000)
    assert len(given) == 1 and 990 <= given[0] <= 1000


def test_a_stuck_gmsh_cfd_fill_says_it_stalled(monkeypatch, tmp_path):
    from meshpipeline.engines.gmsh import gmsh_runner as G
    monkeypatch.setattr(G, "run_guarded", _stuck("the gmsh CFD fill"))
    out = G._run_gmsh_local(tmp_path, timeout=NT.CLOUD_RUN_WALL_S)
    assert out["timed_out"] and out["stalled"]


def test_a_stuck_snapgrid_build_says_it_stalled(monkeypatch, tmp_path):
    from meshpipeline.engines.snapgrid import runner as SG
    from meshpipeline.sandbox import safe_exec
    (tmp_path / SG.SOURCE_NAME).write_text("<ecxml/>")
    SG.write_plan(tmp_path, SG.plan_for("draft"))
    monkeypatch.setattr(safe_exec, "run_guarded", _stuck("the snap-grid build"))
    out = SG.run_native_build(tmp_path, bashrc="/x", timeout=NT.CLOUD_RUN_WALL_S)
    assert out["timed_out"] and out["stalled"]
    assert "STALLED" in (tmp_path / SG.LOG_NAME).read_text()


def test_snapgrids_own_openfoam_tools_carry_no_clock_of_their_own():
    import inspect

    from meshpipeline.engines.snapgrid import native as SN
    for fn in (SN.check, SN.split_and_check, SN.export_volume):
        assert inspect.signature(fn).parameters["timeout"].default is None, fn.__name__


# MULTI-REGION: THE PLAN'S TIME SENDS IT TO A VM, NEVER REFUSES IT FOR A GUESSED CLOCK

def test_a_multiregion_plan_past_the_cloud_run_wall_runs_on_a_vm(tmp_path):
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
    assert R.machine_for_plan(tmp_path, None)["where"] == "cloud_run"
    assert R.machine_for_plan(tmp_path, 3000.0)["where"] == "cloud_run"
    assert MM.read(tmp_path)["where"] == "cloud_run"
    assert R.machine_for_plan(tmp_path, NT.CLOUD_RUN_WALL_S)["where"] == "gce"
    assert NT.wall_for(tmp_path) == NT.VM_WALL_S


def test_a_thermal_plan_the_budget_holds_is_no_longer_refused_for_a_guessed_clock():
    """The plan used to be refused past 80% of a fixed 3,000 s; the time it may take is now the
    biggest machine's wall (configure_mesh passes VM_WALL_S), and only the cell budget refuses it."""
    import math

    from tests.unit.cad.ecxml_models import wirebond_package

    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_build import build
    from meshpipeline.engines.snappy_multiregion import thermal_fidelity as TF

    side = build(read_ecxml(wirebond_package().xml())).sidecar
    mapping = {r["name"]: [r["name"]] for r in side["regions"]}
    kinds = {r["name"]: r["type"] for r in side["regions"]}
    b = side["domain"]["box_m"]
    base = math.dist(b["min"], b["max"]) / TF.BACKGROUND_DIVISIONS

    def _plan(timeout_s):
        return TF.plan(side, mapping, kinds, base, (2, 2), {}, budget_cells=1e12, timeout_s=timeout_s)

    need = _plan(float(NT.VM_WALL_S))
    old_limit_s = need.seconds / TF.TIME_SHARE / 2         # a clock half what this plan needs
    assert _plan(old_limit_s).refusal                       # refused for time, as it was
    assert not need.refusal                                 # held only by the budget now


# THE LOOP'S CLOCKS GIVE A WATCHED MESH ITS TIME

def test_a_tool_call_that_is_a_mesh_still_running_is_not_cut_by_the_loops_clock():
    """agents/loop/tool_execution waited on each call with a WALL clock of the loop's model time
    left: a watched mesh past it was dropped mid-run and the attempt ended (Greptile on #320)."""
    import asyncio

    from meshpipeline.agents.loop.budget import wait_on_model_time

    async def _mesh():
        with progress_clock.native_run():
            await asyncio.sleep(0.5)
        return "meshed"

    assert asyncio.run(wait_on_model_time(_mesh(), timeout=0.1, poll_s=0.05)) == "meshed"
    assert asyncio.run(wait_on_model_time(_mesh(), timeout=None)) == "meshed"
    import inspect

    from meshpipeline.agents.loop import tool_execution
    assert "wait_on_model_time(self.driver.execute(inv)" in inspect.getsource(tool_execution.ToolExecution.run)


def test_the_fallback_ladders_time_left_gives_native_time_back():
    import time

    from meshpipeline.pipeline.engine_fallback import remaining_seconds
    now = time.time()
    state = {"builder_deadline_epoch": now + 100.0}
    assert remaining_seconds(state, now=now) == pytest.approx(100.0, abs=1)
    with progress_clock.native_run():
        time.sleep(0.3)
    assert remaining_seconds(state, now=now) == pytest.approx(100.3, abs=0.2)


def test_a_stuck_cfmesh_passage_says_it_stalled(monkeypatch, tmp_path):
    import json

    from meshpipeline.engines.cfmesh import native as N
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text('surfaceFile "geom.stl";')
    sub = tmp_path / "p2"
    sub.mkdir()
    (sub / "geom.stl").write_text("solid x\nendsolid x\n")
    (tmp_path / N.PASSAGES_FILE).write_text(json.dumps({"runs": ["p2"], "ports": [[], ["b"]]}))
    monkeypatch.setattr(N, "run_guarded", _stuck("a cfMesh passage"))
    rc, timed_out, why = N.mesh_passages(tmp_path)
    assert rc == -1 and timed_out and N.PASSAGE_STALLED in why


def test_a_stopped_run_takes_a_tool_it_started_in_its_own_session_with_it(tmp_path):
    """A nested watched tool (snap-grid's checkMesh) runs in a session of its own; stopping the run
    that started it must stop it too."""
    import os
    import time

    pidf = tmp_path / "child.pid"
    code = ("import subprocess,sys,time\n"
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True)\n"
            f"open({str(pidf)!r},'w').write(str(c.pid))\n"
            "print('start', flush=True)\ntime.sleep(60)")
    with pytest.raises(StalledExpired), (tmp_path / "m.log").open("w") as fh:
        run_guarded(_py(code), stdout=fh, stderr=subprocess.STDOUT, timeout=30, stall_s=1.0, poll_s=0.1)
    child = int(pidf.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        try:                                   # a zombie left for init to reap counts as gone
            if open(f"/proc/{child}/stat").read().split(")")[-1].split()[0] == "Z":
                break
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        pytest.fail("the tool the run started outlived it")


# VMTK: A LAYER THAT IS NOT UNTANGLING IS STOPPED BY ITS OWN COUNT, NEVER BY A CLOCK

def test_the_untangle_brake_stops_a_layer_whose_final_warp_keeps_tangling_it():
    """vmtk's layer generator prints "try last : " before each final warp; a layer that converges
    prints it once. Here it keeps printing it (the Fluent aorta, 4 in 2.5 h at 100% CPU)."""
    from meshpipeline.engines.vmtk.vmtk_runner import UntangleBrake
    from meshpipeline.sandbox.safe_exec import StoppedByWatch

    code = ("import sys,time\nprint('Generating boundary layer')\nprint('untangle procedure.. ')\n"
            "for i in range(50):\n sys.stdout.write('try last : '); sys.stdout.flush(); time.sleep(0.2)")
    brake = UntangleBrake()
    with pytest.raises(StoppedByWatch) as err:
        run_guarded(_py(code), capture_output=True, text=True, timeout=30, stall_s=5.0, poll_s=0.1,
                    watch=brake)
    assert "not untangling" in err.value.reason and "try last" in err.value.output
    assert brake.tripped["retangled"] > 1


def test_a_layer_that_untangles_once_is_left_alone():
    from meshpipeline.engines.vmtk.vmtk_runner import UntangleBrake
    code = "import time\nprint('untangle procedure.. ')\nprint('try last : ')\ntime.sleep(0.5)\nprint('done')"
    proc = run_guarded(_py(code), capture_output=True, text=True, timeout=30, stall_s=5.0, poll_s=0.1,
                       watch=UntangleBrake())
    assert proc.returncode == 0 and "done" in proc.stdout


def test_a_layered_fill_the_brake_stopped_goes_on_to_the_layer_free_fill(tmp_path, monkeypatch):
    import json

    from meshpipeline.engines.vmtk import vmtk_runner as V
    from meshpipeline.sandbox.safe_exec import StoppedByWatch

    calls = []

    def fake_run(argv, timeout=None, **kw):
        calls.append(list(argv))
        joined = " ".join(argv)
        if "vmtksurfaceremeshing" in joined:
            (tmp_path / "lumen.vtp").write_text("remeshed")
            return subprocess.CompletedProcess(argv, 0, stdout="Done.", stderr="")
        if "-boundarylayer 1" in joined:
            exc = StoppedByWatch(argv, "the boundary layer is not untangling: its final warp tangled it again 2 times")
            exc.output = "Generating boundary layer\nuntangle procedure.. \ntry last : try last : try last : "
            raise exc
        (tmp_path / "mesh.vtu").write_text("filled")
        return subprocess.CompletedProcess(argv, 0, stdout="Done executing vmtkmeshgenerator.", stderr="")

    monkeypatch.setattr(V, "run_guarded", fake_run)
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(V.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 4})))
    (tmp_path / "lumen_open.vtp").write_text("staged")
    res = V._run_vmtk_local(tmp_path, timeout=NT.CLOUD_RUN_WALL_S)
    gens = [c for c in calls if len(c) > 1 and c[1] == "vmtkmeshgenerator"]
    assert len(gens) == 2 and "-boundarylayer 0" in " ".join(gens[1])
    assert "not untangling" in res["repair_note"] and not res.get("timed_out")


# NEVER AN OOM KILL: THE MEMORY GUARD, AND RUN-3 FOR EVERY WATCHED ENGINE

def test_a_run_its_machine_cannot_hold_is_stopped_as_out_of_memory(monkeypatch, tmp_path):
    from meshpipeline.sandbox import safe_exec
    from meshpipeline.sandbox.safe_exec import MemoryExpired, why_it_stopped
    monkeypatch.setattr(safe_exec, "_over_memory", lambda: (30.1, 32.0))
    with pytest.raises(MemoryExpired) as err:
        run_guarded(_py("import time\nprint('x', flush=True)\ntime.sleep(30)"), capture_output=True,
                    text=True, timeout=30, stall_s=5.0, poll_s=0.1)
    assert err.value.out_of_memory and not err.value.stalled and err.value.used_gib == 30.1
    out = why_it_stopped(err.value, {"rc": -1, "timed_out": True, "log_tail": ""})
    assert out["out_of_memory"] and not out["timed_out"] and "out of memory" in out["log_tail"]


def test_the_fea_watch_stops_for_memory_as_the_kill_run_3_answers(monkeypatch, tmp_path):
    from meshpipeline.engines.gmsh import gmsh_runner as G
    from meshpipeline.sandbox import safe_exec
    monkeypatch.setattr(safe_exec, "_over_memory", lambda: (30.1, 32.0))
    w = safe_exec.run_watched(_py("import time\nprint('x', flush=True)\ntime.sleep(30)"),
                              log_path=tmp_path / "l.log", hard_timeout=30, stall_s=5.0, poll_s=0.1)
    assert w.stopped == "memory"
    monkeypatch.setattr(safe_exec, "run_watched", lambda *a, **k: w)
    out = G._run_fea_watched(tmp_path, timeout=100)
    from meshpipeline.engines.fea_driver import _out_of_memory
    assert out["out_of_memory"] and _out_of_memory(out)


def test_a_run_out_of_memory_runs_again_one_machine_up(tmp_path):
    from meshpipeline.contracts import mesh_machine as MM
    oom = {"rc": -1, "timed_out": False, "out_of_memory": True, "used_gib": 29.5}
    done = {"rc": 0, "timed_out": False}
    out, seen = _off_the_wall(tmp_path, [oom, done])
    assert out == done and [w for _c, w in seen] == ["cloud_run", "gce"]
    assert MM.read(tmp_path)["machine"] == MM.MACHINES[0].name and MM.read(tmp_path)["why"] == "out_of_memory"


def test_out_of_memory_on_the_largest_machine_is_handed_back(tmp_path):
    from meshpipeline.contracts import mesh_machine as MM
    MM.write(tmp_path, MM._decision(MM.CEILING, 900.0, 0.0, need=900.0, words="", why="t"))
    oom = {"rc": -1, "timed_out": False, "out_of_memory": True, "used_gib": 880.0}
    out, seen = _off_the_wall(tmp_path, [oom])
    assert out is oom and len(seen) == 1


# gmsh CFD: A FILL FAR PAST ONE JOB IS NEVER STARTED

def test_the_fill_estimate_reads_the_passage_sizes_off_the_coarse_fill():
    gmsh = pytest.importorskip("gmsh")
    import numpy as np

    from meshpipeline.engines.gmsh.driver import estimate_fill

    def _fill(h: float) -> int:
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        gmsh.option.setNumber("Mesh.MeshSizeMin", h)
        gmsh.model.mesh.clear()
        gmsh.model.mesh.generate(3)
        return sum(len(t) for t in gmsh.model.mesh.getElements(3)[1])

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("box")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        coarse = _fill(0.2)
        _tags, coords, _ = gmsh.model.mesh.getNodes()
        pts = np.asarray(coords).reshape(-1, 3)
        est = estimate_fill(gmsh, pts, np.full(len(pts), 0.1))
        fine = _fill(0.1)
    finally:
        gmsh.finalize()
    assert est is not None and 4 * coarse < est
    assert 0.8 < est / fine < 1.25, (coarse, est, fine)


def test_an_over_size_fill_moves_the_run_to_another_engine(monkeypatch, tmp_path):
    import json

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing as M
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh import driver as D
    from meshpipeline.engines.gmsh import gmsh_runner as G
    from meshpipeline.engines.preflight import read_refusal
    from meshpipeline.pipeline.engine_fallback import ENGINE, classify

    (tmp_path / D.FILL_ESTIMATE_FILE).write_text(json.dumps({"estimated_cells": 40_000_000, "limit": 16_000_000}))
    monkeypatch.setattr(G, "run_guarded", lambda *a, **k: subprocess.CompletedProcess(a[0], D.RC_FILL_OVERSIZE,
                                                                                      "", "[GMSH] OVERSIZE"))
    res = G._run_gmsh_local(tmp_path, timeout=NT.CLOUD_RUN_WALL_S)
    assert res["oversize"]["by_rule"] and res["oversize"]["ratio"] == 5.0

    class _Engine:
        name = "gmsh"

        def run_cartesian_mesh(self, workspace, *, timeout, context=None):
            return res

    monkeypatch.setattr(runtime, "get_engine", lambda name: _Engine())
    ctx = BuilderToolContext(workspace=tmp_path, geometry=object(), job_id="j", engine="gmsh")
    out = M._run_and_judge(ctx, M.PreparedMeshRun(engine="gmsh", cap=NT.CLOUD_RUN_WALL_S))
    assert "end your turn" in out["guidance"] and "40,000,000" in out["guidance"]
    ref = read_refusal(tmp_path)
    assert ref is not None
    state = {"engine": "gmsh", "executor_success": False, "executor_failed_gate": "finalize",
             "executor_failure_cause": ref.cause, "executor_failure_facts": dict(ref.facts)}
    assert classify(state).kind == ENGINE


def test_the_spec_says_a_non_untangling_layer_moves_on_only_when_layers_are_asked_and_an_engine_is_left(tmp_path):
    import json

    from meshpipeline.engines.vmtk import vmtk_runner as V
    spec = tmp_path / "vmtk_spec.json"
    spec.write_text(json.dumps({"boundary_layers": 4}))
    V._note_way_on(tmp_path, "snappy")
    assert json.loads(spec.read_text())[V.LAYERS_MOVE_ON] == "snappy"
    V._note_way_on(tmp_path, "")                         # no later engine: the layer-free fill
    assert V.LAYERS_MOVE_ON not in json.loads(spec.read_text())
    spec.write_text(json.dumps({"boundary_layers": 0}))   # no layers asked: nothing to move on for
    before = spec.read_text()
    V._note_way_on(tmp_path, "snappy")
    assert spec.read_text() == before                    # a run that never meets the brake ships as before


def _brake_run(tmp_path, monkeypatch, move_on):
    import json

    from meshpipeline.engines.vmtk import vmtk_runner as V
    from meshpipeline.sandbox.safe_exec import StoppedByWatch
    calls = []

    def fake_run(argv, timeout=None, **kw):
        calls.append(list(argv))
        joined = " ".join(argv)
        if "vmtksurfaceremeshing" in joined:
            (tmp_path / "lumen.vtp").write_text("remeshed")
            return subprocess.CompletedProcess(argv, 0, stdout="Done.", stderr="")
        if "-boundarylayer 1" in joined:
            brake = kw["watch"]
            brake.tripped = {"final_warps": 3, "retangled": 2}
            exc = StoppedByWatch(argv, "the boundary layer is not untangling: its final warp tangled it again 2 times")
            exc.output = "Generating boundary layer\nuntangle procedure.. \ntry last : try last : try last : "
            raise exc
        (tmp_path / "mesh.vtu").write_text("filled")
        return subprocess.CompletedProcess(argv, 0, stdout="Done executing vmtkmeshgenerator.", stderr="")

    monkeypatch.setattr(V, "run_guarded", fake_run)
    spec = V.resolve_strategy({"sizing_array": "LocalRadius", "boundary_layers": 4})
    if move_on:
        spec[V.LAYERS_MOVE_ON] = move_on
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(spec))
    (tmp_path / "lumen_open.vtp").write_text("staged")
    res = V._run_vmtk_local(tmp_path, timeout=NT.CLOUD_RUN_WALL_S)
    return res, [c for c in calls if len(c) > 1 and c[1] == "vmtkmeshgenerator"]


def test_layers_that_will_not_untangle_move_the_run_on_when_the_plan_asks_for_layers(tmp_path, monkeypatch):
    res, gens = _brake_run(tmp_path, monkeypatch, "snappy")
    assert len(gens) == 1                                    # no layer-free fill
    assert res["layers_not_converging"] == {"retangled": 2, "next_engine": "snappy"}
    assert "VMTK layers kept re-tangling (2 rounds) - next engine (snappy)" in res["repair_note"]
    assert not (tmp_path / "mesh.vtu").exists()


def test_with_no_engine_left_the_layer_free_fill_still_delivers(tmp_path, monkeypatch):
    res, gens = _brake_run(tmp_path, monkeypatch, "")
    assert len(gens) == 2 and "-boundarylayer 0" in " ".join(gens[1]) and not res.get("layers_not_converging")


def test_the_builder_ends_the_attempt_and_the_ladder_moves_on(monkeypatch, tmp_path):
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing as M
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal
    from meshpipeline.pipeline.engine_fallback import ENGINE, classify

    class _Engine:
        name = "vmtk"

        def run_cartesian_mesh(self, workspace, *, timeout, context=None):
            return {"rc": -1, "timed_out": False, "log_tail": "",
                    "layers_not_converging": {"retangled": 2, "next_engine": "snappy"}}

    monkeypatch.setattr(runtime, "get_engine", lambda name: _Engine())
    ctx = BuilderToolContext(workspace=tmp_path, geometry=object(), job_id="j", engine="vmtk",
                             broken_mesh_moves_to="snappy")
    out = M._run_and_judge(ctx, M.PreparedMeshRun(engine="vmtk", cap=NT.CLOUD_RUN_WALL_S))
    assert "re-tangling (2 rounds) - next engine" in out["guidance"] and "end your turn" in out["guidance"]
    ref = read_refusal(tmp_path)
    state = {"engine": "vmtk", "executor_success": False, "executor_failed_gate": "finalize",
             "executor_failure_cause": ref.cause, "executor_failure_facts": dict(ref.facts)}
    assert classify(state).kind == ENGINE
