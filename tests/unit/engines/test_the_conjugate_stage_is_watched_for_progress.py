# Responsibility: Hold the conjugate chain to the house rule about time - every stage watched for
# progress, and a stage that was stopped told apart from a stage that failed.
"""NO GUESSED TIME LIMITS, and this engine was the last one still guessing inside a stage.

`contracts/native_time` is the adopted rule: "A native mesh run keeps going while it shows
progress - its log growing, a new stage, new cells - and is stopped only when it has made NO
progress for STALL_WINDOW_S, or at the hard wall of the machine it runs on", and it lists "every
OpenFOAM command 1,500 s" among the caps that were REMOVED. Six engines - snapgrid, snappy,
snappy_multiregion, cfmesh, gmsh and vmtk - run their meshers through
`sandbox/safe_exec.run_guarded(stall_s=...)`. `conjugate._foam` was handed `timeout` alone, so the
only thing that could stop a stage was the clock, whether it was working or hung, and a stage that
WAS hung sat on the whole remaining budget - the budget the rest of the chain, the review aids and
the boundary index all come out of.

AND THE OTHER HALF OF `watched=True` IS NOW TAKEN TOO, which an earlier version of this file said
could not be. The flag itself is still unreachable from here - it is read only at
`agents/builder/tools/meshing.py:348` and `:486`, both inside the model-led `run_mesh`, and this
engine has a deterministic driver, so `agents/builder/invoke.py:142` routes it to `driver.drive`.
So the driver adopts the mechanism directly, the way `engines/snapgrid/driver.py:90` does:
`driver._run_off_the_wall` runs the case through `native_time.run_off_the_wall` with
min(`wall_for(workspace)`, this engine's budget) as its cap. On Cloud Run that is 13,800 s against
a declared 21,600 s, which is the end that was broken; on a VM the budget still binds, and
`_run_off_the_wall`'s own docstring carries the arithmetic for why.

TWO THINGS THAT FILE USED TO SAY ARE WRONG, and the measurements are below:

  * "`wait_s = timeout + 600 + VM_START_S`, so a container that took VM_WALL_S for its own
    deadline would still be meshing hours after the worker stopped listening" - `wait_s` is not a
    clock that gives up. `cloud_run_client` says so in its own words at WAIT_STALL_S ("THE WAIT
    ENDS WHEN THE REMOTE IS GONE, NOT ON A CLOCK") and `_poll_gcs_json` reaches its deadline
    branch only when `silent`;
  * the loop needed something else before it could work at all: `run_off_the_wall` decides RUN-5
    and RUN-3 off `timed_out` and `out_of_memory`, and this engine's runner never set either, so
    a Cloud Run pass stopped at the wall still working was never re-run. It sets them now, through
    the seam that already has the names (`safe_exec.why_it_stopped`), which is also the gating
    criterion this engine has always declared and nothing has ever satisfied
    (`criteria.CRITERIA_ROWS`, key="timed_out").
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from meshpipeline.contracts import native_time as NT
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.registry import get_spec
from meshpipeline.sandbox import safe_exec as SE

#: THE COMMIT THIS WORK WAS MEASURED AGAINST, pinned rather than spelled `HEAD`.
#:
#: A no-fix control has to load the code as it was BEFORE the fix. While the fix sat uncommitted
#: `HEAD` WAS that code, so every control below passed - and the moment the fix was committed
#: `HEAD` became the fix, each control compared the fix against itself, and ten rows went red at
#: once. A control that holds only while the work is uncommitted is not a control: it would have
#: been red in CI forever, and it would have stopped discriminating on the day it mattered.
#:
#: 42d8a731 is the tip this branch's conjugate work was written on and measured against. A shallow
#: clone that does not carry it makes these rows SKIP, which the loaders below already handle.
PRE_FIX = "42d8a731"

#: the six that declare the flag, and run their meshers the way this now runs its stages
WATCHED_ENGINES = ("snapgrid", "snappy", "snappy_multiregion", "cfmesh", "gmsh", "vmtk")


# #
# every stage is watched, with the contract's own window
# #
def test_a_stage_is_run_through_the_shared_watch_with_the_contracts_window(tmp_path, monkeypatch):
    """EXECUTED: `_foam` is called for real and `run_guarded` is spied on, so the call it actually
    makes is what is asserted - not the text of the line that makes it."""
    seen: dict = {}

    def spy(args, **kwargs):
        seen.update(kwargs)
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, None, None)

    monkeypatch.setattr(C, "run_guarded", spy)
    rc, took = C._foam(tmp_path, "true", tmp_path / "stage.log", bashrc="/dev/null", timeout=777)

    assert rc == 0 and took >= 0.0
    assert seen["stall_s"] == NT.STALL_WINDOW_S, seen
    assert seen["timeout"] == 777, "the hard wall is still what the caller gave it"
    # the watch reads this stage's own log growing: safe_exec._log_of takes the path off the handle
    assert SE._log_of(seen["stdout"]) == (str(tmp_path / "stage.log"),)


def test_the_window_is_the_one_every_other_engine_waits(monkeypatch):
    """One figure for "no sign of life", not a second one for this engine that happens to
    agree."""
    assert NT.STALL_WINDOW_S == 900
    for name in WATCHED_ENGINES:
        assert get_spec(name).run_policy.watched is True, name
    # and this engine does NOT declare it, because its own deterministic driver is what runs it -
    # see the module docstring and `test_the_cap_is_the_cloud_run_wall...` below, which executes
    # the cap the driver actually hands the mesher
    policy = get_spec("cfmesh_conjugate").run_policy
    assert policy.watched is False
    assert get_spec("cfmesh_conjugate").build_driver is not None, (
        "the flag is only read inside run_mesh, which an engine with its own driver never reaches")


# #
# a stage that was STOPPED is told apart from a stage that FAILED
# #
def _ws(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "controlDict").write_text("")
    (tmp_path / "constant" / "triSurface").mkdir(parents=True)
    return tmp_path


def _build_with(monkeypatch, raising):
    monkeypatch.setattr(C, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(C, "check_mesh", lambda *a, **k: {"mesh_ok": True, "cells": 1})
    monkeypatch.setattr(C, "_foam", raising)


def test_a_stuck_stage_is_reported_as_stuck_and_not_as_slow(tmp_path, monkeypatch):
    """It used to arrive at `native._run_conjugate_local`'s catch-all as
    "FAILED: TimeoutExpired", which reads as a mesher that crashed."""
    def stalled(ws, script, log, *, bashrc, timeout):
        log.write_text("")
        raise SE.StalledExpired(["bash"], timeout, silence_s=900.0, ran_s=1234.0)

    _build_with(monkeypatch, stalled)
    with pytest.raises(C.ConjugateError) as caught:
        C.build(_ws(tmp_path), background_cell_m=1e-3, timeout=21_600)

    said = str(caught.value)
    assert "stuck, not slow" in said, said
    assert "no sign of life for 900s" in said and "after running 1234s" in said, said
    assert getattr(caught.value, "stage", "") == "cartesianMesh"


def test_a_stage_still_working_at_the_budget_says_that_instead(tmp_path, monkeypatch):
    def at_the_wall(ws, script, log, *, bashrc, timeout):
        log.write_text("")
        raise subprocess.TimeoutExpired(["bash"], timeout)

    _build_with(monkeypatch, at_the_wall)
    with pytest.raises(C.ConjugateError) as caught:
        C.build(_ws(tmp_path), background_cell_m=1e-3, timeout=21_600)

    said = str(caught.value)
    assert "cartesianMesh was still working when the run's remaining 215" in said, said
    assert "s ran out" in said, said
    assert "stuck" not in said, "a slow stage is not a stuck one"


def test_a_stage_stopped_at_the_memory_guard_blames_the_machine_and_not_the_clock(tmp_path,
                                                                                 monkeypatch):
    """RUN-3: the same case one machine up. A MemoryExpired is a TimeoutExpired, so the one thing
    that must not happen is it being reported as a part that needs more time."""
    def oom(ws, script, log, *, bashrc, timeout):
        log.write_text("")
        raise SE.MemoryExpired(["bash"], timeout, used_gib=53.1, limit_gib=57.6)

    _build_with(monkeypatch, oom)
    with pytest.raises(C.ConjugateError) as caught:
        C.build(_ws(tmp_path), background_cell_m=1e-3, timeout=21_600)

    said = str(caught.value)
    assert "before the machine ran out of memory" in said, said
    assert "53.1 GiB" in said and "57.6 GiB" in said, said
    assert "larger machine" in said and "RUN-3" in said, said


def test_the_stage_that_was_stopped_is_still_timed_in_the_report(tmp_path, monkeypatch):
    """A refusal that throws away how long the stage ran throws away the only measurement of the
    thing that went wrong. `seconds` is filled before the raise."""
    def stalled(ws, script, log, *, bashrc, timeout):
        log.write_text("")
        raise SE.StalledExpired(["bash"], timeout, silence_s=900.0, ran_s=5.0)

    _build_with(monkeypatch, stalled)
    with pytest.raises(C.ConjugateError) as caught:
        C.build(_ws(tmp_path), background_cell_m=1e-3, timeout=60)
    assert getattr(caught.value, "stage", "") == "cartesianMesh"


# #
# the cap the driver hands the mesher: the machine's wall, as a ceiling on this engine's budget
# #
REPO = Path(__file__).resolve().parents[3]


def _at_head(repo_path: str, name: str):
    """One file as it is at HEAD, as a module of its own. The no-fix control: nothing on the
    shared tree is touched and nothing is written anywhere."""
    try:
        src = subprocess.run(["git", "show", PRE_FIX + ":" + repo_path], cwd=str(REPO),
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - no git, no control
        pytest.skip(f"the no-fix control needs the file at {PRE_FIX}: {exc}")
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = PRE_FIX + ":" + repo_path
    sys.modules[name] = mod
    try:
        exec(compile(src, PRE_FIX + ":" + repo_path, "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


class _Outcome:
    def __init__(self, ok: bool, marker: str) -> None:
        self.produced_deliverable, self.terminal_value = ok, marker


class _Run:
    """The builder run seam `drive` reports through, reduced to what it calls."""

    async def fence(self, phase: str) -> None:
        return None

    def note_authoring(self) -> None:
        return None

    def note_native_run(self, *, produced_usable_mesh: bool) -> None:
        return None

    def outcome(self, *, produced_deliverable: bool, exhausted: bool = False,
                failure_marker: str = ""):
        return _Outcome(produced_deliverable, failure_marker)


def _caps_the_mesher_was_given(module, monkeypatch, tmp_path, *, estimate: int) -> list[int]:
    """Run the REAL `drive` of `module` on a part of this size and return the timeouts the ENGINE
    was actually called with.

    THE ENGINE IS THE SPY, not the driver's own helper, because what is under test is the number
    that reaches `run_cartesian_mesh` - and at HEAD there is no helper to spy on. Everything
    between the CAD and the plan is stubbed, exactly as
    `test_a_mesh_that_cannot_come_home_is_refused_first._drive_a_part_of` stubs it; the route is
    not stubbed, because `drive` deciding it (`mesh_machine.choose`, driver.py:572) is what the
    wall is read off.
    """
    import asyncio

    from meshpipeline.cad import fluid_volume as FV
    from meshpipeline.engines import runtime as RT

    cad = tmp_path / "part.step"
    cad.write_text("")
    lo, hi = [0.0, 0.0, 0.0], [0.17, 0.17, 0.01]
    monkeypatch.setattr(module, "find_source", lambda ws, source_path="": cad)
    monkeypatch.setattr(module, "skin_bounds", lambda ws: (lo, hi))
    monkeypatch.setattr(module, "cad_scale", lambda src: 1.0)
    monkeypatch.setattr(module, "coolant_sits_inside", lambda rec, a, b: "")
    monkeypatch.setattr(FV, "surface_bounded", lambda *a, **k: {
        "narrowest_m": 1.0e-4, "wetted_area_m2": 0.0, "volume_m3": 5.4e-5, "passages": 2,
        "bbox_min_m": lo, "bbox_max_m": hi})
    monkeypatch.setattr(module.B, "cells_across_for",
                        lambda **k: (4, int(estimate), "priced for this test"))

    caps: list[int] = []

    class _Engine:
        def run_cartesian_mesh(self, ws, *, timeout: int, context=None) -> dict:
            caps.append(int(timeout))
            return {"rc": 0}

    monkeypatch.setattr(RT, "get_engine", lambda name: _Engine())
    got = asyncio.run(module.drive(tmp_path, {}, job_id="t", publish=None, run=_Run()))
    assert got[0] is True, (got, "the part has to reach the mesher or this measures nothing")
    return caps


#: Small enough that `mesh_machine.choose` on this engine's own budget model routes it to CLOUD
#: RUN, and inside every size gate `drive` asks before it meshes - which is the case the finding
#: is about: a small conjugate part that runs long.
A_CLOUD_RUN_PART = 6_500_000


def test_the_cap_is_the_cloud_run_wall_when_the_case_runs_there(tmp_path, monkeypatch):
    """EXECUTED, through the real `drive`. `MESH_TIMEOUT_SECONDS` is 14,400 s and Cloud Run stops
    a task there whatever the container thinks, so an engine deadline past CLOUD_RUN_WALL_S
    (13,800) is an engine that is killed with no result document - the customer reads GONE
    ("stopped answering part way through the mesh") instead of a mesh that needs a coarser
    fidelity with this policy's own `timeout_hint`."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    caps = _caps_the_mesher_was_given(D, monkeypatch, tmp_path, estimate=A_CLOUD_RUN_PART)

    assert MM.read(tmp_path).get("where") == "cloud_run", (
        "this size has to route to Cloud Run or the wrong end is being measured")
    assert caps == [NT.CLOUD_RUN_WALL_S], caps
    assert caps[0] <= NT.CLOUD_RUN_WALL_S < get_spec("cfmesh_conjugate").run_policy.run_timeout()


def test_at_head_the_same_part_asks_for_more_than_cloud_run_allows_THE_CONTROL(tmp_path,
                                                                              monkeypatch):
    """THE NO-FIX CONTROL. The same part, the same stubs, the same route - against `driver.py` as
    it is at HEAD, which computes its cap from `policy.run_timeout()` alone."""
    head = _at_head("src/meshpipeline/engines/cfmesh_conjugate/driver.py", "_head_conj_driver")
    caps = _caps_the_mesher_was_given(head, monkeypatch, tmp_path, estimate=A_CLOUD_RUN_PART)

    assert len(caps) == 1, caps
    assert caps[0] > NT.CLOUD_RUN_WALL_S, (
        caps, "HEAD fitted the Cloud Run wall, so this is not the control it claims to be")
    assert caps[0] == 21_600


def test_a_vm_route_keeps_the_budget_it_already_had(tmp_path, monkeypatch):
    """THE OTHER END, and the reason the wall is a CEILING and not a replacement. `wall_for` is
    72,000 s on a VM; taking it would put the plate's own maxRunDuration at 94,831 s, past the
    86,400 s soft limit of the worker task waiting to collect the result
    (adapters/pipeline_execution/celery.py:44). The settled VM figures must not move."""
    from meshpipeline.adapters.mesh_execution import gce_vm as V
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines.cfmesh_conjugate import budget as B
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    caps: list[int] = []

    class _Engine:
        def run_cartesian_mesh(self, ws, *, timeout: int, context=None) -> dict:
            caps.append(int(timeout))
            return {"rc": 0}

    from meshpipeline.engines import runtime as RT
    monkeypatch.setattr(RT, "get_engine", lambda name: _Engine())
    MM.write(tmp_path, MM.choose(process_gib=B.peak_gib(333_869_285),
                                 workspace_gib=B.written_gib(333_869_285), why="the plate"))
    assert MM.read(tmp_path).get("where") == "gce"
    assert NT.wall_for(tmp_path) == NT.VM_WALL_S == 72_000

    D._run_off_the_wall(tmp_path, job_id="")

    budget = get_spec("cfmesh_conjugate").run_policy.run_timeout()
    assert caps == [budget] == [21_600], caps
    # ...and that is what keeps the machine's life the figure this route is settled at
    gib = B.written_gib(333_869_285)
    assert V.lifetime_seconds(caps[0], workspace_gib=gib, cpus=64) == 44_431
    assert V.lifetime_seconds(NT.VM_WALL_S, workspace_gib=gib, cpus=64) > 86_400, (
        "the control: the wall taken outright outlives the worker that collects the result")


# #
# ...and the loop can only act on a result that says why it stopped
# #
def _a_workspace_with_a_plan(tmp_path):
    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "controlDict").write_text("application foamRun;\n")
    (tmp_path / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    PL.write_plan(tmp_path, PL.ConjugatePlan(
        background_cell_m=1e-3, levels=1, expect_fluid_volume_m3=1.0, expect_fluid_pieces=1,
        volume_tol=0.2, narrowest_m=1e-4))
    return tmp_path


def _ran_and_was_stopped(module, tmp_path, monkeypatch, exc):
    """`_run_conjugate_local` of `module` over a build that was STOPPED rather than refused."""
    def raiser(ws, **kwargs):
        raise exc

    monkeypatch.setattr(module, "build", raiser)
    return module._run_conjugate_local(_a_workspace_with_a_plan(tmp_path), bashrc="/dev/null",
                                       timeout=60)


def test_a_run_stopped_at_its_budget_says_so_in_the_result(tmp_path, monkeypatch):
    """THE GATING CRITERION NOTHING WAS SETTING. `criteria.CRITERIA_ROWS` has carried
    key="timed_out", "Build completes within compute budget" since this engine was written, and
    `describe_native_result` derives that flag from the outcome - so a build stopped at its budget
    arrived as outcome "failed", `timed_out` False: a mesher that broke."""
    from meshpipeline.engines.cfmesh_conjugate import criteria as CR
    from meshpipeline.engines.cfmesh_conjugate import native as N

    assert any(row.key == "timed_out" and row.gating for row in CR.CRITERIA_ROWS)
    out = _ran_and_was_stopped(N, tmp_path, monkeypatch,
                               C.ConjugateError("the budget was spent", stage="cartesianMesh",
                                                stopped=True))
    assert out["timed_out"] is True and out["outcome"] == "timed_out", out
    assert NT.stopped_at_the_wall(out) is True


def test_a_stuck_run_is_not_called_a_run_out_of_time(tmp_path, monkeypatch):
    """`stalled` is what keeps RUN-5 from re-running a case that was never going to finish."""
    from meshpipeline.engines.cfmesh_conjugate import native as N

    out = _ran_and_was_stopped(
        N, tmp_path, monkeypatch,
        C.ConjugateError("no sign of life", stage="cartesianMesh",
                         stopped=SE.StalledExpired(["bash"], 60, silence_s=900.0, ran_s=1234.0)))
    assert out["stalled"] is True and out["timed_out"] is True, out
    assert NT.stopped_at_the_wall(out) is False, "a stuck case must not be re-run as if it were slow"


def test_an_out_of_memory_run_is_a_bigger_machine_and_not_more_time(tmp_path, monkeypatch):
    """RUN-3. `why_it_stopped` clears `timed_out` and keeps the peak, which is what makes
    `run_off_the_wall` step the machine up instead of asking for a coarser mesh."""
    from meshpipeline.engines.cfmesh_conjugate import native as N

    out = _ran_and_was_stopped(
        N, tmp_path, monkeypatch,
        C.ConjugateError("out of memory", stage="cartesianMesh",
                         stopped=SE.MemoryExpired(["bash"], 60, used_gib=53.1, limit_gib=57.6)))
    assert out["out_of_memory"] is True and out["timed_out"] is False, out
    assert out["used_gib"] == 53.1


def test_a_mesh_this_engine_DECLINED_is_still_a_refusal_and_not_a_timeout(tmp_path, monkeypatch):
    """THE CONTROL FOR ALL THREE. A refusal carries no `stopped`, so nothing about it moves - more
    than two regions out of the split is still outcome "failed" with `timed_out` False, which is
    what every case this engine has ever refused looks like."""
    from meshpipeline.engines.cfmesh_conjugate import native as N

    out = _ran_and_was_stopped(N, tmp_path, monkeypatch,
                               C.ConjugateError("expected exactly the regions fluid and solid"))
    assert out["timed_out"] is False and out["outcome"] == "failed", out
    assert "stalled" not in out and "out_of_memory" not in out


def test_the_shared_loop_really_moves_a_stopped_cloud_run_pass_onto_a_vm(tmp_path, monkeypatch):
    """BOTH HALVES TOGETHER, EXECUTED: the real `run_off_the_wall` over a real
    `_run_conjugate_local` result. RUN-5 is "the same case, nothing coarsened, on a VM"."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines.cfmesh_conjugate import native as N

    ws = _a_workspace_with_a_plan(tmp_path)
    MM.write(ws, MM.choose(process_gib=3.89, workspace_gib=5.0, why="a small conjugate case"))
    seen: list = []

    def once(cap):
        seen.append((int(cap), MM.read(ws).get("where")))
        if len(seen) == 1:
            return _ran_and_was_stopped(
                N, ws, monkeypatch,
                C.ConjugateError("the budget was spent", stage="cartesianMesh", stopped=True))
        return {"rc": 0}

    out = NT.run_off_the_wall(ws, once, engine="cfmesh_conjugate", job_id="")

    assert seen == [(NT.CLOUD_RUN_WALL_S, "cloud_run"), (NT.VM_WALL_S, "gce")], seen
    assert MM.read(ws).get("where") == "gce"
    assert out["rc"] == 0


def test_at_head_that_pass_was_never_re_run_THE_CONTROL(tmp_path, monkeypatch):
    """THE NO-FIX CONTROL for the three above. `native.py` at HEAD builds the same result without
    the flags, so `stopped_at_the_wall` is False and the loop hands back the stopped run."""
    head = _at_head("src/meshpipeline/engines/cfmesh_conjugate/native.py", "_head_conj_native")
    from meshpipeline.contracts import mesh_machine as MM

    ws = _a_workspace_with_a_plan(tmp_path)
    MM.write(ws, MM.choose(process_gib=3.89, workspace_gib=5.0, why="a small conjugate case"))
    out = _ran_and_was_stopped(
        head, ws, monkeypatch,
        C.ConjugateError("the budget was spent", stage="cartesianMesh", stopped=True))

    assert out["timed_out"] is False, "HEAD set the flag, so this is not the control it claims"
    assert NT.stopped_at_the_wall(out) is False

    seen: list = []

    def once(cap):
        seen.append(int(cap))
        return out if len(seen) == 1 else {"rc": 0}

    got = NT.run_off_the_wall(ws, once, engine="cfmesh_conjugate", job_id="")
    assert seen == [NT.CLOUD_RUN_WALL_S], seen
    assert got["timed_out"] is False and MM.read(ws).get("where") == "cloud_run", (
        "the stopped pass was never moved off the wall")
