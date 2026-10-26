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
