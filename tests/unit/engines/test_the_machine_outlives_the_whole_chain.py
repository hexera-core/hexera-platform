# Responsibility: Hold the relationship between one conjugate run's budget and the life of the
# machine it runs on, so a chain that is fine stage by stage cannot be deleted mid-run.
"""WHAT WAS MEASURED, by arithmetic on 2026-11-12, before a run of this size was ever allowed to
finish.

`cfmesh_conjugate`'s run budget is one number and three things spent it:

    spec.run_policy.run_timeout   min(CONJUGATE_RUN_TIMEOUT_S 21600,
                                      OPENFOAM_COMMAND_TIMEOUT 1500 x 6)  =  9,000 s
    conjugate._foam               that number PER STAGE, and the parallel chain is eight stages
    foam_exec.EXPORT_TIMEOUT_S    another 7,200 s after it
    gce_vm maxRunDuration         that number ONCE: timeout + LIFETIME_MARGIN_S  =  12,600 s

So one stage's allowance was 71% of the machine's entire life and the export's alone was 57% of
it. For scale, the comparable hand-run chain measured 9,359 s at 413 M cells and 10,796 s at
506 M BEFORE any review aid ran, and the target part is 333,869,285 cells margined. The VM would
have deleted itself holding a finished mesh with nothing written down.

These tests EXECUTE the three halves of the fix rather than reading them: the budget is the
setting's own whole-run figure, the chain spends it as a deadline and cannot exceed it, and the
machine's life is derived from it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.adapters.mesh_execution import gce_vm as V
from meshpipeline.engines import dispatch as D
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.registry import get_spec

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

# #
# the budget is the whole run's, not one command's
# #
def test_the_budget_is_the_setting_and_not_a_command_multiple():
    policy = get_spec("cfmesh_conjugate").run_policy
    assert policy is not None
    assert policy.run_timeout() == int(rtcfg.CONJUGATE_RUN_TIMEOUT_S)


def test_the_budget_no_longer_follows_the_per_command_timeout(monkeypatch):
    """OPENFOAM_COMMAND_TIMEOUT is a per-COMMAND figure and a conjugate build is eleven of them.
    Moving it must not move this budget, which is what the clamp made it do."""
    policy = get_spec("cfmesh_conjugate").run_policy
    before = policy.run_timeout()
    monkeypatch.setattr(rtcfg, "OPENFOAM_COMMAND_TIMEOUT", 60)
    assert policy.run_timeout() == before


def test_the_budget_covers_the_measured_chain():
    """9,359 s at 413 M cells and 10,796 s at 506 M, before checkMesh or any review aid. A budget
    under that is a budget that throws away the expensive part after paying for it."""
    assert get_spec("cfmesh_conjugate").run_policy.run_timeout() > 10_796


# #
# the machine's life is derived from it
# #
#: THE PLATE, AND EVERY PART OF WHAT IT COSTS, each figure recorded somewhere in this repository.
#: Seconds, at the Corintis plate's margined 333,869,285 cells.
PLATE_CELLS = 333_869_285
#: the comparable hand-run chain, measured at 413 M cells (spec.py's own note) - the plate is
#: smaller, so this is the high figure and the honest one to price against
CHAIN_S = 9_359
#: checkMesh with -writeFields, per region, from native._boundary_quality's measured table
#: (652 s at 75,162,351 cells and 878 s at 107,107,563) scaled to this plate's split, where the
#: solid is roughly three quarters of the cells (gce_vm.DISK_PER_WORKSPACE_GIB's note)
CHECKMESH_S = int(0.25 * PLATE_CELLS * 652 / 75_162_351 + 0.75 * PLATE_CELLS * 878 / 107_107_563)
#: the plate's two boundaries
PLATE_FACES = 31_184_373 + 40_667_460
#: WHAT THE RETURN COSTS A GiB, read off engines/return_scope's and parallel_gzip's own headers
#: rather than off gce_vm's constant, so this test PRICES the margin instead of restating it: the
#: coupon's 1,120,883,923-byte (1.0439 GiB) workspace took 4.3 s at FAST_LEVEL, 22.1 s at
#: MEASURE_LEVEL and 7.4 s to archive, all on 32 cores, and a workspace this far over the
#: extraction ceiling pays all three.
_COUPON_GIB = 1_120_883_923 / 2.0 ** 30
RETURN_S_PER_GIB = (4.3 + 22.1 + 7.4) / _COUPON_GIB
#: ...and what is still on the disk when it starts: the mesh, the coolant's internal.vtu, the
#: boundary .vtp tree, the two boundary indexes, the quality sidecars and the review msh. The
#: metal's copy of every cell and checkMesh's raw fields are both gone by then (native.py).
ON_DISK_GIB = 590.0


def _the_whole_container_half() -> float:
    """Every second the container spends between the first cell and handing back, priced.

    THE REVIEW SURFACE IS PRICED AND NOT GUESSED NOW. This used to carry `SURFACE_S = 103`,
    described in its own comment as "extrapolated from its own measured table" - and the phase it
    was about had no budget of any kind, which is the shape of the defect the boundary index and
    the volume export had already been given one for. `review_surface.SURFACE_SECONDS_PER_FACE` is
    a measurement of `build_region_msh` end to end, re-run 2026-11-12 at the budget the module
    actually spends: 3.13 / 2.55 / 1.94 / 1.79 us a face at 1 M / 4 M / 8 M / 16 M delivered
    faces, which is a FIXED 2.3 s plus a marginal 1.65 us a face rather than a flat rate, charged
    at 2.0. It is what the builder itself now refuses against, and this prices the plate off the
    same constant rather than off a number that could drift away from it.
    """
    from meshpipeline.engines.cfmesh_conjugate import native as N
    from meshpipeline.engines.cfmesh_conjugate import review_surface as RS
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import EXPORT_TIMEOUT_S

    return (CHAIN_S + CHECKMESH_S + EXPORT_TIMEOUT_S
            + PLATE_FACES * RS.SURFACE_SECONDS_PER_FACE
            + PLATE_FACES * N.INDEX_SECONDS_PER_FACE)


def test_the_machine_outlives_the_WHOLE_run_including_the_index_and_the_return(tmp_path):
    """THE RELATIONSHIP THAT DECIDES WHETHER THE RUN SURVIVES, and nothing used to execute it.

    The two tests this replaces asserted `x < x + 3600` and `x + 3600 == x + 3600`, which hold for
    every input and for every future value of the margin including one second. What has to hold is
    that the WHOLE run fits inside the life the machine is given - the eight-stage chain, both
    checkMesh runs, foamToVTK, the review surface, the boundary index AND the return, against
    `lifetime_seconds` - and the two halves are bounded by different things: the container's half
    by the engine's own deadline, the return by the margin.

    Both halves are exercised rather than quoted: the boundary index is really run, on a
    two-region workspace, with exactly the budget the rest of the chain would leave it, and has to
    come back BUILT rather than priced out.
    """
    from tests.foam_fixtures import write_row_of_hexes

    from meshpipeline.engines.cfmesh_conjugate import budget as B
    from meshpipeline.engines.cfmesh_conjugate import native as N

    budget = get_spec("cfmesh_conjugate").run_policy.run_timeout()
    container = _the_whole_container_half()
    index_s = PLATE_FACES * N.INDEX_SECONDS_PER_FACE
    returning = RETURN_S_PER_GIB * ON_DISK_GIB

    # 1. the container's half fits the engine's own deadline, the index included
    assert container <= budget, (container, budget, "the chain does not fit its own budget")

    # 2. ...and the index is really reached with what the rest of it leaves
    for region in ("fluid", "solid"):
        write_row_of_hexes(tmp_path / "constant" / region / "polyMesh")
    out = N._boundary_indexes(tmp_path, budget_left_s=budget - (container - index_s))
    assert sorted(out["regions"]) == ["fluid", "solid"]
    assert all(r["ok"] is True for r in out["regions"].values()), out

    # 3. the machine outlives both halves
    life = V.lifetime_seconds(budget, workspace_gib=B.written_gib(PLATE_CELLS))
    assert container + returning < life, (container, returning, life)


def test_the_old_constant_margin_did_not_cover_that_return():
    """THE CONTROL. `LIFETIME_MARGIN_S` alone used to be the whole margin and it is 3,600 s; the
    plate's return prices at about 19,100 s on 32 cores. A machine whose life was budget + 3,600
    is deleted with the mesh finished and the archive part-written, which is the one failure that
    leaves nothing for anybody to collect."""
    from meshpipeline.engines.cfmesh_conjugate import budget as B

    returning = RETURN_S_PER_GIB * ON_DISK_GIB
    assert returning > 5 * V.LIFETIME_MARGIN_S, returning
    budget = get_spec("cfmesh_conjugate").run_policy.run_timeout()
    assert budget + V.LIFETIME_MARGIN_S < _the_whole_container_half() + returning
    # ...and the priced margin does cover it
    assert V.return_margin_s(B.written_gib(PLATE_CELLS)) > returning


def test_the_margin_is_the_floor_it_always_was_for_a_small_workspace():
    """Nothing moves for a run that reached this route before: a workspace figure of zero, or one
    a caller does not know, gets exactly the constant."""
    assert V.return_margin_s(0.0) == V.LIFETIME_MARGIN_S
    assert V.lifetime_seconds(600) == 600 + V.LIFETIME_MARGIN_S
    # monotone, so a bigger mesh never buys a shorter life
    lives = [V.lifetime_seconds(600, workspace_gib=g) for g in (0, 10, 100, 1_000)]
    assert lives == sorted(lives)


def test_the_vm_body_carries_that_life_and_not_one_stages_cap():
    from meshpipeline.engines.cfmesh_conjugate import budget as B

    workspace_gib = B.written_gib(PLATE_CELLS)
    body = V.instance_body(name="n", zone="z", machine="n2-highmem-64", image="i", account="",
                           input_uri="gs://i", output_uri="gs://o", engine="cfmesh_conjugate",
                           timeout=21_600, disk_gb=600, workspace_gib=workspace_gib)
    got = int(body["scheduling"]["maxRunDuration"]["seconds"])
    # the plate's own machine, whose 64 cores are past the 32 the rate was measured on, so the
    # price is the measured one and this is the figure the route has always carried
    assert got == V.lifetime_seconds(21_600, workspace_gib=workspace_gib, cpus=64)
    assert got == V.lifetime_seconds(21_600, workspace_gib=workspace_gib)
    assert got > 21_600 + V.LIFETIME_MARGIN_S, "the plate's return is priced into the life"
    # the run's own budget travels to the container as it always did
    meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert meta["timeout"] == "21600"


# #
# the return is priced on the cores the machine has, not on the cores it was measured on
# #
def test_a_machine_with_fewer_cores_than_the_measurement_is_charged_more():
    """THE DEFECT: RETURN_SECONDS_PER_GIB is a 32-core figure (return_scope and parallel_gzip both
    spread over job_cpus) and it was charged to every VM whatever it has. The header on the same
    line records the spread on the same coupon - 33.8 s on 32 cores, 242.9 s on one - so an 8-core
    box was being priced at 1/1.6 of what its own return costs."""
    assert V.seconds_per_gib(32) == V.RETURN_SECONDS_PER_GIB
    assert V.seconds_per_gib(8) > V.RETURN_SECONDS_PER_GIB
    assert V.seconds_per_gib(1) > V.seconds_per_gib(8) > V.seconds_per_gib(16)
    # and the two recorded points are what the model reproduces
    assert abs(V.seconds_per_gib(1) - V.RETURN_SECONDS_PER_GIB_ONE_CORE) < 0.05
    assert abs(V.seconds_per_gib(32) - 32.4) < 0.05


def test_a_bigger_machine_is_never_charged_less_than_the_measurement():
    """ONE-SIDED, deliberately: a two-point model may raise a price and may not lower one, so
    every figure already settled on this route stays exactly where it was."""
    for cpus in (32, 40, 64, 96):
        assert V.seconds_per_gib(cpus) == V.RETURN_SECONDS_PER_GIB, cpus
    from meshpipeline.engines.cfmesh_conjugate import budget as B

    plate = B.written_gib(PLATE_CELLS)
    assert V.return_margin_s(plate, cpus=64) == V.return_margin_s(plate)
    assert V.lifetime_seconds(21_600, workspace_gib=plate, cpus=64) == V.lifetime_seconds(
        21_600, workspace_gib=plate)


def test_a_caller_that_does_not_know_the_cores_gets_what_it_always_got():
    assert V.seconds_per_gib(None) == V.RETURN_SECONDS_PER_GIB
    assert V.seconds_per_gib(0) == V.RETURN_SECONDS_PER_GIB
    assert V.return_margin_s(0.0, cpus=1) == V.LIFETIME_MARGIN_S, (
        "an empty workspace costs the floor whatever the machine is - which is why CFD cfMesh "
        "and thermal multi-region, both of which pass workspace_gib=0.0, cannot move")


def test_the_body_prices_the_return_on_the_machine_it_is_about_to_start():
    """`cpus_of` reads the machine type from the one table a decision's own `cpus` comes from, so
    a caller that knows only the machine still prices the box it is starting."""
    assert V.cpus_of("n2-highmem-8") == 8
    assert V.cpus_of("n2-highmem-64") == 64
    assert V.cpus_of("not-a-machine") is None

    small = V.instance_body(name="n", zone="z", machine="n2-highmem-8", image="i", account="",
                            input_uri="gs://i", output_uri="gs://o", engine="fea",
                            timeout=600, disk_gb=200, workspace_gib=10.71)
    big = V.instance_body(name="n", zone="z", machine="n2-highmem-64", image="i", account="",
                          input_uri="gs://i", output_uri="gs://o", engine="fea",
                          timeout=600, disk_gb=200, workspace_gib=10.71)
    small_s = int(small["scheduling"]["maxRunDuration"]["seconds"])
    big_s = int(big["scheduling"]["maxRunDuration"]["seconds"])
    assert small_s > big_s, (small_s, big_s)
    assert big_s == V.lifetime_seconds(600, workspace_gib=10.71), "the big box is unchanged"


def test_the_decision_s_own_cores_reach_the_body(monkeypatch):
    """`mesh_machine._decision` already records `cpus`; `trigger_vm` is where it is read."""
    import pytest as _pytest
    import requests as _rq

    seen: dict = {}
    monkeypatch.setattr(V, "instance_body", lambda **k: seen.update(k) or {})
    monkeypatch.setattr(V, "_job_container", lambda: ("img", "acct"))
    monkeypatch.setattr(
        "meshpipeline.adapters.mesh_execution.cloud_run_client._access_token", lambda: "t")
    monkeypatch.setattr(_rq, "post", lambda *a, **k: (_ for _ in ()).throw(
        _rq.exceptions.ConnectionError("no network in a unit test")))
    with _pytest.raises(Exception):
        V.trigger_vm(input_uri="gs://i", output_uri="gs://o", engine="cfmesh_conjugate",
                     timeout=21_600, operation_key="k",
                     decision={"machine": "n2-highmem-8", "workspace_gib": 10.71, "cpus": 8})
    assert seen["cpus"] == 8


def test_the_workspace_figure_that_buys_the_disk_also_buys_the_time(monkeypatch):
    """One number, read once in `trigger_vm`: the return is a walk over every byte of the same
    workspace the disk was sized from, so the two must not be able to disagree."""
    seen: dict = {}
    monkeypatch.setattr(V, "instance_body", lambda **k: seen.update(k) or {})
    monkeypatch.setattr(V, "_job_container", lambda: ("img", "acct"))
    monkeypatch.setattr(
        "meshpipeline.adapters.mesh_execution.cloud_run_client._access_token", lambda: "t")

    import requests as _rq

    def no_http(*_a, **_k):
        raise _rq.exceptions.ConnectionError("no network in a unit test")

    monkeypatch.setattr(_rq, "post", no_http)
    with pytest.raises(Exception):
        V.trigger_vm(input_uri="gs://i", output_uri="gs://o", engine="cfmesh_conjugate",
                     timeout=21_600, operation_key="k",
                     decision={"machine": "n2-highmem-64", "workspace_gib": 565.3})
    assert seen["workspace_gib"] == 565.3
    assert seen["disk_gb"] == V.disk_gb_for(565.3)


# #
# the chain spends it as a deadline
# #
class _Clock:
    """A monotonic clock that only moves when a stage is run, so a chain can be driven through a
    budget in a test without waiting for one."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _drive(monkeypatch, *, budget: int, per_stage: float, ranks: int = 1) -> list[int]:
    """Run `conjugate.build` with every OpenFOAM stage faked out, and return the timeout each
    stage was given. The clock advances `per_stage` seconds per stage."""
    clock = _Clock()
    seen: list[int] = []

    def fake_foam(ws, script, log, *, bashrc, timeout):
        seen.append(int(timeout))
        clock.now += per_stage
        log.write_text("")
        return 0, per_stage

    monkeypatch.setattr(C.time, "monotonic", clock)
    monkeypatch.setattr(C, "_foam", fake_foam)
    monkeypatch.setattr(C, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(C, "check_mesh", lambda *a, **k: {"mesh_ok": True, "cells": 1})
    return seen


def _workspace(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "controlDict").write_text("")
    (tmp_path / "constant" / "triSurface").mkdir(parents=True)
    return tmp_path


def test_the_first_stage_still_gets_the_whole_budget(tmp_path, monkeypatch):
    """Every part in the corpus finishes the whole chain in under two minutes against a budget of
    six hours, so what a stage is allowed must be unchanged in practice."""
    ws = _workspace(tmp_path)
    seen = _drive(monkeypatch, budget=21_600, per_stage=1.0)
    with pytest.raises(C.ConjugateError):
        # it gets as far as the region check, which a faked chain cannot satisfy
        C.build(ws, background_cell_m=1e-3, timeout=21_600)
    assert seen, "no stage ran"
    assert seen[0] == 21_600


def test_no_stage_is_given_more_than_the_budget_has_left(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    seen = _drive(monkeypatch, budget=1_000, per_stage=300.0)
    with pytest.raises(C.ConjugateError):
        C.build(ws, background_cell_m=1e-3, timeout=1_000)
    # three stages at 300 s each out of 1,000: 1000, 700, 400 - and never 1,000 again
    assert seen == [1_000, 700, 400], seen
    assert sum(seen) != len(seen) * 1_000


def test_the_chain_cannot_outlive_the_budget(tmp_path, monkeypatch):
    """THE DEFECT, stated as a test: with a per-stage cap the chain's permitted total was the cap
    times the number of stages. Now a chain that spends the budget is refused rather than allowed
    to run on past the machine's life."""
    ws = _workspace(tmp_path)
    seen = _drive(monkeypatch, budget=500, per_stage=400.0)
    with pytest.raises(C.ConjugateError) as caught:
        C.build(ws, background_cell_m=1e-3, timeout=500)
    assert "budget was spent" in str(caught.value)
    assert sum(seen) <= 500 + max(seen), seen
    assert len(seen) == 2, "the third stage must not have been started"


def test_the_refusal_names_the_stage_that_could_not_start(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _drive(monkeypatch, budget=500, per_stage=400.0)
    with pytest.raises(C.ConjugateError) as caught:
        C.build(ws, background_cell_m=1e-3, timeout=500)
    assert getattr(caught.value, "stage", "") == "splitMeshRegions"


# #
# RULE 4: what an ordinary CFD, thermal or FEA customer gets, against BOTH baselines
# #
#: The refs this branch must not move a reading against: this branch's HEAD, and what Areen and
#: Pranav ship.
BASELINES = (PRE_FIX, "origin/main")
REPO = Path(__file__).resolve().parents[3]


def _module_at(ref: str, repo_path: str, name: str):
    """One file as it is at `ref`, as a module of its own - loaded IN PROCESS so both sides can be
    read side by side. Nothing on the shared tree is touched and nothing is written anywhere."""
    try:
        src = subprocess.run(["git", "show", f"{ref}:{repo_path}"], cwd=str(REPO),
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - no ref, no check
        pytest.skip(f"the baseline needs {ref}:{repo_path}: {exc}")
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = f"{ref}:{repo_path}"
    sys.modules[name] = mod
    try:
        exec(compile(src, f"{ref}:{repo_path}", "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


GCE_VM = "src/meshpipeline/adapters/mesh_execution/gce_vm.py"
DISPATCH = "src/meshpipeline/engines/dispatch.py"


def _body(mod, *, machine: str, timeout: int, workspace_gib: float, cpus=None) -> dict:
    kw = {"name": "n", "zone": "z", "machine": machine, "image": "i", "account": "",
          "input_uri": "gs://i", "output_uri": "gs://o", "engine": "e",
          "timeout": timeout, "disk_gb": 200}
    try:
        return mod.instance_body(workspace_gib=workspace_gib, cpus=cpus, **kw)
    except TypeError:
        return mod.instance_body(**kw)          # the baseline had no such parameters


#: THE DECISIONS THE OTHER ENGINES ACTUALLY MAKE. `engines/cfmesh/cfmesh_runner.py:356` (external
#: and internal CFD) and `engines/snappy_multiregion/multiregion_runner.py:971` (thermal) both
#: call `mesh_machine.choose(process_gib=0.0, workspace_gib=0.0)`, so the workspace figure this
#: whole change is about is ZERO for them - which is the non-regression that matters.
QUIET_ROUTES = (
    ("external and internal CFD, cfMesh", "n2-highmem-8", 7_200),
    ("external and internal CFD, a big one", "n2-highmem-64", 14_400),
    ("thermal, multi-region snappy", "n2-highmem-16", 10_800),
    ("thermal, a big one", "n2-highmem-64", 14_400),
)


@pytest.mark.parametrize("ref", BASELINES)
def test_the_cfd_and_thermal_routes_are_identical_to_the_baseline(ref):
    """EXECUTED AGAINST BOTH BASELINES, not reasoned about. Their drivers pass workspace_gib=0.0,
    so `return_margin_s` is the floor it always was and `maxRunDuration` is the same integer -
    whatever cores the box has, because `seconds_per_gib` is multiplied by zero."""
    base = _module_at(ref, GCE_VM, f"_base_gce_vm_{ref.replace('/', '_')}")
    for label, machine, timeout in QUIET_ROUTES:
        now = _body(V, machine=machine, timeout=timeout, workspace_gib=0.0,
                    cpus=V.cpus_of(machine))
        was = _body(base, machine=machine, timeout=timeout, workspace_gib=0.0)
        assert (int(now["scheduling"]["maxRunDuration"]["seconds"])
                == int(was["scheduling"]["maxRunDuration"]["seconds"])
                == timeout + V.LIFETIME_MARGIN_S), (label, ref)
        # ...and the disk, which is the other thing this route sizes from that figure
        assert V.disk_gb_for(0.0) == V.MIN_DISK_GB == base.MIN_DISK_GB, (label, ref)


#: WHAT A LATER COMMIT ON THIS BRANCH ADDED TO THE SAME STARTUP SCRIPT, named so that the
#: "nothing else moved" controls below stay controls instead of being retired.
#:
#: bc71c00f gave /mnt/stateful_partition/work mode 1777, because the container runs as uid 1000
#: and that directory is its TMPDIR: root-owned 0755 killed the first parallel conjugate mesh this
#: route ever ran, with Open MPI unable to make its session directory. It is a real second change
#: to the body PRE_FIX is the control for, so the two rows that compare the WHOLE body subtract
#: exactly this and nothing else - if the subtraction stops matching, the body moved again and
#: whoever moved it has to say so here.
LATER_STARTUP_LINES = (
    "# THE CONTAINER RUNS AS uid 1000 AND THIS DIRECTORY IS ITS TMPDIR (see the docker run below).",
    "chmod 1777 /mnt/stateful_partition/work",
)

#: WHERE THE SELF-DELETE BEGINS. Everything from this line to the end of the script is the box
#: removing itself, and this branch REWROTE it rather than adding to it: the pinned body issues a
#: bare `curl -sf ... >/dev/null`, and a refused delete therefore said nothing anywhere but
#: `exit status 22` on a serial console - 9.4 hours of an idle n2-highmem-64 on job b6762d5c,
#: 2026-11-13, because the mesh identity had no compute permission at all. The new form reads the
#: HTTP code and prints it with the grant to make.
#:
#: So the tail is cut from BOTH bodies and the comparison is about everything before it, which is
#: what these two rows are for. It is cut, not normalised away unchecked: both bodies are asserted
#: to still carry it, so a self-delete that disappeared entirely would fail here.
SELF_DELETE_FROM = "TOKEN=$(get instance/service-accounts/default/token"


def _without_later_startup_changes(body: dict, *, added: bool = True) -> dict:
    """`body` with the startup-script lines a later commit added taken back out, and the
    self-delete tail cut off.

    Asserts they were there: a control that silently normalises away a line that is no longer
    present stops testing anything. `added=False` for a body from BEFORE those lines existed -
    the pinned one - which has the tail but not the block.
    """
    import copy

    out = copy.deepcopy(body)
    for item in out["metadata"]["items"]:
        if item["key"] != "startup-script":
            continue
        lines = item["value"].split("\n")
        tail = [n for n, ln in enumerate(lines) if ln.startswith(SELF_DELETE_FROM)]
        assert tail, "the startup script no longer removes the machine at all"
        lines = lines[:tail[0]]
        if added:
            for marker in LATER_STARTUP_LINES:
                assert marker in lines, f"startup script no longer carries {marker!r}"
            start = lines.index(LATER_STARTUP_LINES[0])
            end = lines.index(LATER_STARTUP_LINES[-1])
            assert 0 <= start < end, (start, end)
            lines = lines[:start] + lines[end + 1:]
        item["value"] = "\n".join(lines)
    return out


def test_the_whole_instance_body_is_identical_to_this_branchs_head():
    """EVERY field, not only the one that was changed: the metadata, the scheduling, the disk, the
    service account. (Against `origin/main` the body differs in the startup script and two
    metadata keys, which are this branch's own and are already at HEAD - so HEAD is the ref that
    isolates what this change did.)"""
    base = _module_at(PRE_FIX, GCE_VM, "_head_gce_vm_whole")
    for label, machine, timeout in QUIET_ROUTES:
        now = _without_later_startup_changes(
            _body(V, machine=machine, timeout=timeout, workspace_gib=0.0,
                  cpus=V.cpus_of(machine)))
        was = _without_later_startup_changes(
            _body(base, machine=machine, timeout=timeout, workspace_gib=0.0), added=False)
        assert json.dumps(now, sort_keys=True) == json.dumps(was, sort_keys=True), label


#: WHAT AN FEA PLAN CARRIES: `engines/fea_driver.py:1127` and `engines/fea_tetwild.py:684` both
#: pass `files_gib`, so FEA is the one non-semiconductor route whose margin moves. The figures are
#: the brief's own element counts at the 10.71 GiB measured for 5 M elements.
FEA_PLANS = ((5_000_000, 10.71, "n2-highmem-8"), (20_000_000, 42.84, "n2-highmem-16"),
             (50_000_000, 107.1, "n2-highmem-32"), (100_000_000, 214.2, "n2-highmem-64"))


def test_the_fea_margin_is_the_one_non_semiconductor_reading_that_moves():
    """DECIDED ON MEASUREMENT AND RECORDED, which is what the finding asked for. Nothing about an
    FEA mesh changes and a run that FINISHES is unaffected: `gce_vm.STARTUP_SCRIPT` stops the
    container at the run's own timeout + 1,800 s and then deletes the VM itself, so
    maxRunDuration is meant to be only the backstop for a box whose startup script is dead.
    ON 2026-11-13 IT WAS DOING THE WHOLE JOB: the mesh identity had no compute permission in its
    own project, every self-delete was answered 403, and job b6762d5c's n2-highmem-64 sat idle
    from 22:36 UTC to the 07:58 cap. Both halves are fixed - the grant, and a startup script that
    says out loud when the delete is refused - but the sentence above describes the design and
    this one describes what was measured. What it buys is
    that an FEA workspace of 214 GiB is given time to be archived instead of being deleted
    mid-archive; what it costs is that an ALREADY BROKEN FEA box lives up to 2.02 h longer before
    Compute Engine removes it."""
    base = _module_at(PRE_FIX, GCE_VM, "_head_gce_vm_fea")
    grew: list[tuple[int, int]] = []
    for elements, files_gib, machine in FEA_PLANS:
        timeout = {5_000_000: 3_600, 20_000_000: 7_200,
                   50_000_000: 10_800, 100_000_000: 14_400}[elements]
        now = int(_body(V, machine=machine, timeout=timeout, workspace_gib=files_gib,
                        cpus=V.cpus_of(machine))["scheduling"]["maxRunDuration"]["seconds"])
        was = int(_body(base, machine=machine, timeout=timeout,
                        workspace_gib=files_gib)["scheduling"]["maxRunDuration"]["seconds"])
        grew.append((now - was, elements))
        assert now > was, (elements, now, was)
    # the measured growth, in the order the plans are listed
    assert [d for d, _e in grew] == [581, 1_746, 3_643, 7_287], grew
    assert max(d for d, _e in grew) < 2.1 * 3_600, "no plan may buy a broken box another shift"


def test_nothing_about_the_fea_mesh_itself_moves():
    """The run's own budget travels to the container unchanged, and so does the disk - so the
    mesher is handed the same deadline and the same room it was before."""
    base = _module_at(PRE_FIX, GCE_VM, "_head_gce_vm_fea2")
    for _elements, files_gib, machine in FEA_PLANS:
        now = _without_later_startup_changes(
            _body(V, machine=machine, timeout=7_200, workspace_gib=files_gib,
                  cpus=V.cpus_of(machine)))
        was = _without_later_startup_changes(
            _body(base, machine=machine, timeout=7_200, workspace_gib=files_gib), added=False)
        meta_now = {i["key"]: i["value"] for i in now["metadata"]["items"]}
        meta_was = {i["key"]: i["value"] for i in was["metadata"]["items"]}
        assert meta_now == meta_was, machine
        assert meta_now["timeout"] == "7200"
        assert V.disk_gb_for(files_gib) == max(base.MIN_DISK_GB, int(4 * files_gib) + 50)


# #
# ...and the return watch, which runs for EVERY engine
# #
def _a_cfd_workspace(root: Path) -> Path:
    ws = root
    (ws / "constant" / "polyMesh").mkdir(parents=True, exist_ok=True)
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "controlDict").write_text("application foamRun;\n")
    for name in ("owner", "neighbour", "faces", "points"):
        (ws / "constant" / "polyMesh" / name).write_text(
            "\n".join(str(i) for i in range(50_000)))
    (ws / "cartesianMesh.log").write_text("Creating octree\nAdding new cells\nOptimising\n")
    return ws


def _returned_through(module, ws: Path, engine: str) -> dict:
    module.engine_runners = lambda: {engine: lambda w, *, bashrc, timeout: {
        "rc": 0, "timed_out": False, "cells": 123_456, "log_tail": "Mesh OK."}}
    return module.run_engine_local(ws, engine=engine, timeout=600)


@pytest.mark.parametrize("engine", ["cfmesh", "snappy_multiregion", "gmsh"])
def test_the_return_watch_changes_nothing_else_a_non_conjugate_run_returns(engine, tmp_path):
    """EXECUTED, both sides. `dispatch.run_engine_local` is shared by every engine, so the watch
    around the return runs for CFD, thermal and FEA too. What it may do is add its own phase log;
    what it may not do is change the verdict, the cell count, the scope decision or the largest
    file the extractor sizes itself from."""
    base = _module_at(PRE_FIX, DISPATCH, f"_head_dispatch_{engine}")
    now = _returned_through(D, _a_cfd_workspace(tmp_path / "now"), engine)
    was = _returned_through(base, _a_cfd_workspace(tmp_path / "was"), engine)

    for key in ("rc", "timed_out", "cells", "log_tail", "return_scope"):
        assert now.get(key) == was.get(key), (engine, key, now.get(key), was.get(key))
    assert now["returned_bytes"]["largest"] == was["returned_bytes"]["largest"]
    assert now["returned_bytes"]["fea"] == was["returned_bytes"]["fea"]
    # the one difference, and it is the phase log itself
    log = tmp_path / "now" / D.PHASE_LOG.format(phase=D.RETURN_PHASE)
    assert log.is_file() and not (tmp_path / "was" / log.name).exists()
    delta = now["returned_bytes"]["raw"] - was["returned_bytes"]["raw"]
    assert 0 < delta <= 64, (delta, "the watch's own log is all that was added")
    assert log.read_text().splitlines()[0] == f"{D.RETURN_PHASE}: started"
