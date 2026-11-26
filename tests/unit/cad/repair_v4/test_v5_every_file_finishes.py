# Responsibility: CRS v5 step 4 ("every file finishes"): the size-scaled budget rule, the wall-aware panel start
#                 under the parallel path, parallel-vs-serial verdict equality, and the per-file budget the eval applies.
# Boundaries: hermetic; fake members, fake workspaces, no OCP.
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4 import limits
from meshpipeline.cad.repair_v4.verdict import wall
from meshpipeline.cad.repair_v4.verdict.assemble import clean_of
from meshpipeline.cad.repair_v4.verdict.panel import MemberResult, run_panel


def _run_arm():
    """The eval modules, loaded the way the eval loads them (`run_arm._load`, stable names)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "devtools/validation/v4_eval"))
    import run_arm

    return run_arm


@pytest.fixture(autouse=True)
def _clean_wall():
    wall.set_file_deadline(None)
    yield
    wall.set_file_deadline(None)


# --- budget_scale --------------------------------------------------------------------------------------------------


def test_budget_scale_is_clamped_and_monotone() -> None:
    assert limits.budget_scale(None) == 1.0 and limits.budget_scale(0) == 1.0
    assert limits.budget_scale(limits.BUDGET_SCALE_FLOOR_FACES) == 1.0
    assert limits.budget_scale(limits.BUDGET_SCALE_CEIL_FACES) == pytest.approx(limits.BUDGET_SCALE_MAX)
    assert limits.budget_scale(10_000_000) == limits.BUDGET_SCALE_MAX
    faces = [1, 500, 1_000, 1_001, 2_000, 5_000, 10_000, 19_999, 20_000, 50_000]
    got = [limits.budget_scale(f) for f in faces]
    assert got == sorted(got) and all(1.0 <= g <= 3.0 for g in got)
    mid = int((limits.BUDGET_SCALE_FLOOR_FACES * limits.BUDGET_SCALE_CEIL_FACES) ** 0.5)
    assert 1.0 < limits.budget_scale(mid) < limits.BUDGET_SCALE_MAX


def test_the_evalspec_restatement_is_the_limits_rule() -> None:
    evalspec = _run_arm().evalspec
    assert evalspec.MAX_BUDGET_SCALE == limits.BUDGET_SCALE_MAX
    assert evalspec.MAX_FILE_WALL_S == evalspec.DEFAULT_FILE_TIMEOUT_S * limits.BUDGET_SCALE_MAX


# --- the wall-aware start ------------------------------------------------------------------------------------------


def test_parallel_worst_case_is_the_slowest_member_plus_the_retry_not_the_sum() -> None:
    assert wall.panel_slots(4, threads=1) == 6       # serial: 4 members + the retry's 2 slots
    assert wall.panel_slots(4, threads=4) == 3       # parallel: one wave + the retry's 2 slots
    assert wall.panel_slots(4, threads=2) == 4
    # 1000 s left: a serial 4 x 300 s panel (1810 s worst case) must clip, a parallel one (910 s) runs as asked
    wall.set_file_deadline(time.monotonic() + 1000.0)
    assert wall.clip_member_timeout(4, 300.0, threads=1)[1] is True
    assert wall.clip_member_timeout(4, 300.0, threads=4) == (300.0, False)


def test_a_panel_that_does_not_fit_gets_a_timeout_derived_from_the_wall_and_still_fits() -> None:
    wall.set_file_deadline(time.monotonic() + 500.0)
    timeout, clipped = wall.clip_member_timeout(4, 300.0, threads=4)
    assert clipped is True and wall.MIN_MEMBER_S <= timeout < 300.0
    assert wall.panel_worst_case_s(4, timeout, threads=4) <= 500.0 + 0.1   # stops AT the wall, never past it


def test_under_the_floor_the_panel_is_not_started() -> None:
    wall.set_file_deadline(time.monotonic() + 60.0)    # (60 - 10) / 3 slots < 20 s floor
    with pytest.raises(wall.TooLateToStart):
        wall.clip_member_timeout(4, 300.0, threads=4)


def _assemble(monkeypatch, *, left: float | None, scale: float = 1.0, statuses=("pass",) * 4):
    from meshpipeline.cad.repair_v4.verdict import assemble

    seen: dict = {}

    def runner(pool, step, members, *, timeout_s, panel_threads=1, **kw):  # noqa: ANN001, ARG001
        seen.update(timeout_s=timeout_s, panel_threads=panel_threads)
        return [MemberResult(m.name, s, min_sicn=0.3) for m, s in zip(members, statuses, strict=False)]

    monkeypatch.delenv("CRS_V4_PANEL_THREADS", raising=False)
    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: SimpleNamespace(
        ok=True, hard=[], soft=[], unbounded=False))
    monkeypatch.setattr(assemble, "check_faithful_steps", lambda *a, **k: None)
    wall.set_file_deadline(None if left is None else time.monotonic() + left)
    wall.set_member_scale(scale)
    members = [SimpleNamespace(name=n) for n in ("gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ")]
    v = assemble.verdict(object(), "s.step", "o.step", members=members, run_panel=runner)
    return v, seen


def test_a_full_panel_is_parallel_by_default_and_takes_the_part_s_scale(monkeypatch) -> None:
    v, seen = _assemble(monkeypatch, left=None, scale=2.0)
    assert seen["panel_threads"] == 4                   # parallel members, with no env var set
    assert seen["timeout_s"] == pytest.approx(600.0)    # 300 s x the part's budget scale
    assert v.clean is True


def test_a_panel_refused_on_the_wall_says_so_and_is_not_a_verdict(monkeypatch) -> None:
    from meshpipeline.cad.repair_v4.verdict.assemble import PanelWallExhausted
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    with pytest.raises(PanelWallExhausted) as why:
        _assemble(monkeypatch, left=30.0)
    assert isinstance(why.value, CheckerUnavailable)    # every caller already reads it as "not measured"


# --- parallel vs serial ------------------------------------------------------------------------------------------


class _Fake:
    def __init__(self, name: str, seconds: float, status: str = "pass", kind: str | None = None) -> None:
        self.name, self.seconds, self.status, self.kind = name, seconds, status, kind

    def run(self, pool, step_path: str, *, timeout_s: float) -> MemberResult:  # noqa: ANN001, ARG002
        time.sleep(self.seconds)
        return MemberResult(self.name, self.status, self.kind, min_sicn=0.3 if self.status == "pass" else None)


@pytest.mark.parametrize("statuses", [("pass", "pass", "pass", "pass"), ("pass", "fail", "pass", "fail")])
def test_parallel_and_serial_panels_give_the_same_results_in_member_order_and_the_same_verdict(statuses) -> None:
    # finishing order deliberately the reverse of member order, so a reduce in completion order would show
    names = ("gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ")
    members = [_Fake(n, 0.08 - 0.02 * i, s, None if s == "pass" else "low_quality")
               for i, (n, s) in enumerate(zip(names, statuses, strict=True))]
    serial = run_panel(None, "x.step", members, timeout_s=10)                     # type: ignore[arg-type]
    parallel = run_panel(None, "x.step", members, timeout_s=10, panel_threads=4)  # type: ignore[arg-type]
    assert parallel == serial and [r.name for r in parallel] == list(names)
    valid = SimpleNamespace(ok=True)
    assert clean_of(tier="full", inconclusive=False, valid=valid, panel=parallel, faithful=None) == \
        clean_of(tier="full", inconclusive=False, valid=valid, panel=serial, faithful=None)


# --- the eval's per-file budget ------------------------------------------------------------------------------------


def _eval_arms():
    return _run_arm().arms_mod


def test_the_file_budget_scales_from_the_indexed_part_and_clears_after_the_file() -> None:
    arms = _eval_arms()

    class Alarm:
        seconds = None

        def rescale(self, s: float) -> None:
            self.seconds = s

    ws = SimpleNamespace(ids={**{f"F{i}": "" for i in range(1, 5001)}, "B1": "", "B2": "", "E1": ""})
    alarm = Alarm()
    fb = arms.FileBudget(base_wall_s=1800.0, wall_clock=alarm, started=time.monotonic())
    scale = fb.apply(ws)
    assert scale == limits.budget_scale(5000) > 1.0
    assert alarm.seconds == pytest.approx(1800.0 * scale)               # the harness alarm moves with the part
    assert wall.member_scale() == scale
    assert 1800.0 * scale - 61.0 < (wall.remaining_s() or 0.0) <= 1800.0 * scale - 59.0
    assert fb.fields() == {"part_faces": 5000, "part_solids": 2, "budget_scale": scale,
                           "file_wall_s": round(1800.0 * scale, 1)}
    budget = arms.scaled_budget(_run_arm().evalspec.Budget(), scale)
    assert budget.wall_s == pytest.approx(600.0 * scale)
    fb.close()
    assert wall.file_deadline() is None and wall.member_scale() == 1.0


def test_finish_reads_the_arm_s_own_fields() -> None:
    arms = _eval_arms()
    assert arms.finish_of({}) == arms.FINISHED
    assert arms.finish_of({"stopped_on_wall": True, "infrastructure_failed": True}) == arms.STOPPED_ON_WALL
    assert arms.finish_of({"infrastructure_failed": True}) == arms.CHECKER_FAILED
