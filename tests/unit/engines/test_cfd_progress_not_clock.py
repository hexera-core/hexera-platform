# Responsibility: Verify the CFD time limits are progress-based, like FEA's: a watched native run is
# stopped only after the stall window with no progress (StalledExpired) or at the machine's wall
# (TimeoutExpired), never while it is still moving; a snappy pass stopped at the Cloud Run wall
# still working runs again on a VM, the same case, and only a stuck run (or one that met even a
# VM's wall) pulls the re-plan-smaller lever; native time never spends the builder's model budgets;
# and a run sent to a VM is a run of its own, never a replay of the one it replaces.
# Boundaries: real subprocesses for the watch (seconds long), stand-ins for the mesher and the claim.
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from meshpipeline.contracts import native_time as NT
from meshpipeline.contracts import progress_clock
from meshpipeline.sandbox.safe_exec import StalledExpired, run_guarded


@pytest.fixture(autouse=True)
def _fresh():
    progress_clock.reset()
    yield
    progress_clock.reset()


def _py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


# THE WATCH

def test_a_run_still_printing_is_never_stopped_however_long_it_takes(tmp_path):
    """Runs well past its stall window in total - 2.4 s against 1 s - but never goes quiet."""
    log = tmp_path / "m.log"
    with log.open("w") as fh:
        proc = run_guarded(_py("import time\nfor i in range(8):\n print(i)\n time.sleep(0.3)"),
                           stdout=fh, stderr=subprocess.STDOUT, timeout=30, stall_s=1.0, poll_s=0.1)
    assert proc.returncode == 0
    assert progress_clock.seconds_since() is not None


def test_a_run_that_goes_quiet_is_stopped_as_stalled(tmp_path):
    log = tmp_path / "m.log"
    t0 = time.monotonic()
    with pytest.raises(StalledExpired) as err, log.open("w") as fh:
        run_guarded(_py("import time\nprint('start')\ntime.sleep(30)"),
                    stdout=fh, stderr=subprocess.STDOUT, timeout=30, stall_s=0.8, poll_s=0.1)
    assert err.value.stalled and time.monotonic() - t0 < 10
    assert isinstance(err.value, subprocess.TimeoutExpired)      # every old handler still catches it


def test_a_run_still_moving_at_the_wall_is_a_plain_timeout_not_a_stall(tmp_path):
    log = tmp_path / "m.log"
    with pytest.raises(subprocess.TimeoutExpired) as err, log.open("w") as fh:
        run_guarded(_py("import time\nwhile True:\n print('x')\n time.sleep(0.1)"),
                    stdout=fh, stderr=subprocess.STDOUT, timeout=1.0, stall_s=5.0, poll_s=0.1)
    assert not getattr(err.value, "stalled", False)


def test_a_captured_run_reads_as_before():
    proc = run_guarded(_py("print('ok')"), capture_output=True, text=True, timeout=10, stall_s=0.1)
    assert proc.returncode == 0 and "ok" in proc.stdout


def test_the_walls_are_the_machines():
    assert NT.wall_s(None) == NT.CLOUD_RUN_WALL_S == 14_400 - 600
    assert NT.wall_s({"where": "cloud_run"}) == NT.CLOUD_RUN_WALL_S
    assert NT.wall_s({"where": "gce", "machine": "n2-highmem-8"}) == NT.VM_WALL_S
    assert NT.STALL_WINDOW_S == 900


# THE cfMesh RUN

def test_a_stuck_cfmesh_run_says_it_stalled(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import native as N

    def _stuck(*a, **k):
        assert k.get("stall_s") == NT.STALL_WINDOW_S, "cartesianMesh is not watched"
        raise StalledExpired(a[0], k.get("timeout"), silence_s=900.0, ran_s=1200.0)

    monkeypatch.setattr(N, "run_guarded", _stuck)
    out = N._run_cartesian_mesh_local(tmp_path, timeout=NT.CLOUD_RUN_WALL_S)
    assert out["timed_out"] is True and out["stalled"] is True


# THE snappy PASS: off the wall, onto a VM

def _pass(tmp_path, monkeypatch, results):
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines.snappy import drivers as drv

    seen = []

    async def _timed(R, workspace, cap, publish, engine, purpose, *, run, native_attempt=1):
        seen.append((cap, (MM.read(workspace) or {}).get("where", "cloud_run")))
        return results[min(len(seen), len(results)) - 1]

    class _Pub:
        async def anote(self, *a, **k):
            return None

    monkeypatch.setattr(drv, "_run_snappy_timed", _timed)
    out = asyncio.run(drv._run_snappy_on_its_machine(None, tmp_path, _Pub(), "snappy", "external_cfd",
                                                     run=SimpleNamespace(job_id="t"), native_attempt=1))
    return out, seen


def test_a_pass_still_working_at_the_cloud_run_wall_runs_again_on_a_vm(tmp_path, monkeypatch):
    wall = {"rc": -1, "timed_out": True}
    done = {"rc": 0, "timed_out": False}
    out, seen = _pass(tmp_path, monkeypatch, [wall, done])
    assert out == done
    assert seen == [(NT.CLOUD_RUN_WALL_S, "cloud_run"), (NT.VM_WALL_S, "gce")]


def test_a_stuck_pass_comes_back_at_once_for_the_re_plan(tmp_path, monkeypatch):
    stuck = {"rc": -1, "timed_out": True, "stalled": True}
    out, seen = _pass(tmp_path, monkeypatch, [stuck])
    assert out is stuck and len(seen) == 1


def test_a_pass_at_a_vms_wall_comes_back_for_the_re_plan(tmp_path, monkeypatch):
    wall = {"rc": -1, "timed_out": True}
    out, seen = _pass(tmp_path, monkeypatch, [wall, wall])
    assert out["timed_out"] and len(seen) == 2 and seen[1][1] == "gce"


# A RUN ON A VM IS A RUN OF ITS OWN

def test_the_same_pass_on_a_vm_is_a_new_native_submission(tmp_path):
    from meshpipeline.application.native_submission import attempt_scoped_operation
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.contracts.mesh_execution import note_native_pass

    ws = tmp_path / "attempt_1"
    ws.mkdir()
    note_native_pass(ws, 2)
    on_cloud = attempt_scoped_operation(ws)
    MM.write(ws, MM.off_the_wall(None))
    on_vm = attempt_scoped_operation(ws)
    assert on_cloud.endswith(":pass_2") and on_vm == on_cloud + ":on_" + MM.MACHINES[0].name


# NATIVE TIME IS NOT MODEL TIME

def test_the_model_clock_stands_still_while_a_mesher_runs():
    t0 = progress_clock.model_time()
    with progress_clock.native_run():
        assert progress_clock.native_running()
        time.sleep(0.3)
    assert progress_clock.native_seconds() >= 0.29
    assert progress_clock.model_time() - t0 < 0.2


def test_the_loop_budget_is_spent_on_rounds_never_on_a_mesh():
    from meshpipeline.agents.loop.budget import LoopBudget
    from meshpipeline.contracts.agent_loop import LoopLimits

    b = LoopBudget.of(LoopLimits(max_rounds=3, total_timeout_s=0.2), 0.2)
    with progress_clock.native_run():
        time.sleep(0.4)
    assert not b.expired() and b.remaining() > 0.1


def test_the_outer_loop_guard_never_cancels_a_running_mesh():
    from meshpipeline.agents.builder.invoke import _wait_on_model_time

    async def _work():
        with progress_clock.native_run():
            await asyncio.sleep(0.5)          # a mesh far past the 0.1 s loop budget
        return "meshed"

    assert asyncio.run(_wait_on_model_time(_work(), timeout=0.1, poll_s=0.05)) == "meshed"

    async def _rounds():
        await asyncio.sleep(5)                # model rounds past the budget: stopped as before

    with pytest.raises(TimeoutError):
        asyncio.run(_wait_on_model_time(_rounds(), timeout=0.1, poll_s=0.05))


def test_the_aggregate_budget_gives_back_native_time():
    from meshpipeline.agents.builder.budget import settle

    now = time.time()
    spent = settle(carried_epoch=now - 10, pipeline_deadline_epoch=None, now=now)
    assert spent.exhausted
    back = settle(carried_epoch=now - 10, pipeline_deadline_epoch=None, now=now, native_s=3600)
    assert not back.exhausted and back.remaining_s == pytest.approx(3590, abs=2)
    assert back.deadline_epoch == spent.deadline_epoch        # the epoch is kept: added once


def test_a_watched_engines_run_is_capped_only_by_the_machine(monkeypatch, tmp_path):
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing as M
    from meshpipeline.engines.registry import get_spec

    monkeypatch.setattr(M, "input_contract_rejection", lambda *a, **k: "")
    ws = tmp_path / "ws"
    ws.mkdir()
    for rel in get_spec("cfmesh").run_policy.required_files:
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("x")
    # a loop with 2 minutes of model time left: a watched run is NOT cut to fit it
    ctx = BuilderToolContext(workspace=ws, geometry=object(), job_id="j", execution_id="e",
                             execution_generation=1, engine="cfmesh", mesh_fidelity="",
                             loop_deadline=progress_clock.model_time() + 120)
    prepared = M.prepare_mesh_run(ctx)
    assert prepared.refusal is None and prepared.cap == NT.CLOUD_RUN_WALL_S


def test_a_quiet_run_still_burning_cpu_is_not_stalled(tmp_path):
    """The FEA watch rule (sandbox/liveness, PR #318) for CFD too: no output for 2 s against a 1 s
    window, but the process computes the whole time - it finishes, never stopped as stuck."""
    pytest.importorskip("meshpipeline.sandbox.liveness")
    log = tmp_path / "m.log"
    code = "import time\nprint(\"start\", flush=True)\nt=time.time()\nwhile time.time()-t<2.0:\n pass"
    with log.open("w") as fh:
        proc = run_guarded(_py(code), stdout=fh, stderr=subprocess.STDOUT, timeout=30, stall_s=1.0,
                           poll_s=0.1)
    assert proc.returncode == 0
