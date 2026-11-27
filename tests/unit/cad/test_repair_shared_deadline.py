# Responsibility: Pin gap 3 - ONE shared deadline for the run's late stages. The delivered-file
#                 read-back, the per-kept-op delivered probe and the hints sidecar each get the
#                 lesser of their own cap and what is left of the deadline `repair_step_file` took
#                 at entry (less a margin), never a fresh budget; hints run only after the repaired
#                 STEP is written, and with no time left they are skipped and recorded, so the run
#                 returns before the attempt node's `wait_for(CAD_REPAIR_TIMEOUT_S)` with the repair.
# Boundaries: no kernel. The clock is injected; the STEP writer, the hints provider and the
#             read-back child are doubles that spend the fake clock as the real ones may.
from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.cad.test_repair_package_session import _EDGE, _package, _session

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import (
    conservative,
    delivery_check,
    mesh_hints,
    oracle,
    region_loop,
)
from meshpipeline.cad.repair.contracts import RepairReport


@pytest.fixture(autouse=True)
def _seam_opted_in(monkeypatch):
    # `repair_step_file` refuses BEFORE choosing an engine unless the deployment opted in (the seam's switch,
    # tests/native/test_native_cad_repair.py proves it for every engine). These tests are about what happens after
    # that, with the engines stubbed, so they run as an opted-in deployment.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)


LIMIT_S = 420.0
#: What a hints child may overrun its wall by: killed at it, then reaped (`bounded_child._REAP_S`).
REAP_S = 5.0


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(conservative, "_clock", fake)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", LIMIT_S)
    return fake


@pytest.fixture
def pass_live(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", "live")


@pytest.fixture
def upload(tmp_path) -> Path:
    path = tmp_path / "part.step"
    path.write_bytes(b"ISO-10303-21; the customer's exact bytes\n")
    return path


REPAIRED = b"ISO-10303-21; the repaired part\n"


@pytest.fixture
def events(monkeypatch, clock):
    """The STEP writer and the hints provider, recording their order and the hints' wall. The
    provider spends ALL of its wall and a reap on top - the worst a bounded child may do."""
    seen: list = []

    def write_step(shape, destination, *, unit=None):
        seen.append(("step", None))
        Path(destination).write_bytes(REPAIRED)

    def hints_for(path, defects, *, budget_s=mesh_hints.HINTS_WALL_S, **_):
        seen.append(("hints", budget_s))
        clock.now += budget_s + REAP_S
        sidecar = mesh_hints.sidecar_path(path)
        sidecar.write_text("{}")
        return sidecar

    monkeypatch.setattr(conservative, "_write_step", write_step)
    monkeypatch.setattr(mesh_hints, "hints_for", hints_for)
    return seen


def _repair_that_ends_at(clock: _Clock, elapsed_s: float, measurements: dict):
    """A `_repair_step_file` whose run spends `elapsed_s` of the clock and then delivers through
    the real `_deliver` with the deadline `repair_step_file` handed it."""
    def fake(source, destination, **kwargs):
        clock.now += elapsed_s
        conservative._deliver("shape", Path(destination), unit="mm", defects=(),
                              measurements=measurements, deadline_s=kwargs["deadline_s"])
        return RepairReport(defects=(), measurements=(), operations=(), summary="ok",
                            diagnostics={}, entities=())
    return fake


# (a) LITTLE TIME LEFT: hints get the remainder, the run returns before the limit, the STEP stands

def test_with_little_time_left_hints_get_only_the_remainder(
        pass_live, clock, events, upload, tmp_path, monkeypatch):
    entered = clock.now
    measurements: dict = {}
    monkeypatch.setattr(conservative, "_repair_step_file",
                        _repair_that_ends_at(clock, LIMIT_S - 30.0, measurements))
    destination = tmp_path / "part-repaired.step"

    conservative.repair_step_file(upload, destination)

    # THE STEP FIRST, THEN THE HINTS - and the hints' wall is the remainder, not 180 s.
    assert [name for name, _ in events] == ["step", "hints"]
    left = (entered + LIMIT_S - conservative.DEADLINE_MARGIN_S) - (entered + LIMIT_S - 30.0)
    assert events[1][1] == pytest.approx(left - conservative.HINTS_MARGIN_S)
    assert events[1][1] < mesh_hints.HINTS_WALL_S
    # Even a hints child that spent its whole wall and was reaped leaves the run inside the limit.
    assert clock.now < entered + LIMIT_S
    assert destination.read_bytes() == REPAIRED
    assert measurements["delivery_source"] == conservative.REPAIRED_SOURCE
    assert measurements["hints"] == str(mesh_hints.sidecar_path(destination))


def test_with_plenty_of_time_hints_keep_their_own_wall(pass_live, clock, events, tmp_path):
    measurements: dict = {}
    conservative._deliver("shape", tmp_path / "p.step", unit="mm", defects=(),
                          measurements=measurements, deadline_s=clock.now + 400.0)
    assert events[1][1] == mesh_hints.HINTS_WALL_S


# (c) ZERO REMAINING: hints skipped and recorded, the delivery stands

@pytest.mark.parametrize("left_s", [0.0, -3.0, conservative.HINTS_MARGIN_S + 1.0],
                         ids=["at_deadline", "past_deadline", "under_one_attempt"])
def test_no_time_left_skips_hints_and_the_delivery_stands(
        pass_live, clock, events, tmp_path, left_s):
    destination = tmp_path / "part-repaired.step"
    measurements: dict = {}
    conservative._deliver("shape", destination, unit="mm", defects=(),
                          measurements=measurements, deadline_s=clock.now + left_s)
    assert [name for name, _ in events] == ["step"]          # the provider was never asked
    assert measurements["hints"] == conservative.HINTS_NO_TIME
    assert measurements["delivery_source"] == conservative.REPAIRED_SOURCE
    assert destination.read_bytes() == REPAIRED
    assert not mesh_hints.sidecar_path(destination).exists()


def test_a_passthrough_past_the_deadline_skips_hints_and_still_delivers_the_upload(
        pass_live, clock, events, upload, tmp_path, monkeypatch):
    entered = clock.now

    def refuse(source, destination, **kwargs):
        clock.now = kwargs["deadline_s"]                     # the run spent every second
        raise conservative.RepairRefused("nothing was kept")

    monkeypatch.setattr(conservative, "_repair_step_file", refuse)
    destination = tmp_path / "part-repaired.step"
    with pytest.raises(conservative.RepairRefused) as raised:
        conservative.repair_step_file(upload, destination)
    assert raised.value.measurements["hints"] == conservative.HINTS_NO_TIME
    assert raised.value.measurements["delivery_source"] == conservative.PASSTHROUGH_SOURCE
    assert destination.read_bytes() == upload.read_bytes()
    assert events == [] and clock.now < entered + LIMIT_S


def test_with_the_pass_off_no_hint_is_asked_whatever_the_clock(clock, events, tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", "off")
    measurements: dict = {}
    conservative._deliver("shape", tmp_path / "p.step", unit="mm", defects=(),
                          measurements=measurements, deadline_s=clock.now + 400.0)
    assert [name for name, _ in events] == ["step"] and measurements == {}


# (b) THE READ-BACK GETS THE REMAINDER, NOT A FRESH 120 s

def test_export_check_receives_what_the_run_has_left(monkeypatch):
    session = _session()
    deadline = session.test_clock.now + 40.0
    session.remaining_s = lambda: deadline - session.test_clock()
    budgets: list[float] = []

    def export_check(before, after, *, budget_s=delivery_check.BUDGET_S):
        budgets.append(budget_s)
        return ""

    monkeypatch.setattr(delivery_check, "export_check", export_check)
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.status == "committed"
    assert len(budgets) == 1
    left = deadline - session.test_clock()        # the clock moved while the package ran
    assert budgets[0] < delivery_check.BUDGET_S
    assert budgets[0] <= 40.0 - oracle.STAGE_MARGIN_S
    assert budgets[0] >= left - oracle.STAGE_MARGIN_S


def test_with_no_time_left_the_read_back_cannot_verify_and_the_package_reverts(monkeypatch):
    session = _session()
    session.remaining_s = lambda: 0.0
    assert session._export_budget_s() == 0.0

    def no_child(*args, **kwargs):
        raise AssertionError("no read-back child may start with no time left")

    monkeypatch.setattr(delivery_check, "_read_back", no_child)
    monkeypatch.setattr(delivery_check, "export_check",
                        TestExportCheck.real_export_check)
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    # THE SAFE CHOICE: an unverified delivery is never committed.
    assert verdict.status != "committed"
    assert verdict.reason.startswith("delivered: export_unchecked (no time left")
    assert session.shape == "original"


def test_a_session_with_no_clock_keeps_the_full_read_back_budget():
    session = _session()
    session.remaining_s = None
    assert session._export_budget_s() == delivery_check.BUDGET_S


class TestExportCheck:
    #: The real function, captured before the conftest's autouse stub replaces the attribute.
    real_export_check = staticmethod(delivery_check.export_check)

    @pytest.mark.parametrize("budget_s", [0.0, -1.0, float("nan")])
    def test_no_budget_declines_without_writing_or_reading(self, monkeypatch, budget_s):
        monkeypatch.setattr(conservative, "_write_step",
                            lambda *a, **k: pytest.fail("nothing may be written"))
        monkeypatch.setattr(delivery_check, "_read_back",
                            lambda *a, **k: pytest.fail("no child may start"))
        answer = self.real_export_check(object(), object(), budget_s=budget_s)
        assert answer.startswith("export_unchecked (no time left")


# THE PER-KEPT-OP DELIVERED PROBE

def test_the_delivered_probe_gets_what_is_left_above_the_final_reserve(monkeypatch):
    from meshpipeline.cad.repair import mesh_probe

    assert region_loop._probe_after_budget_s(1000.0, 70.0) == region_loop.PROBE_AFTER_CAP_S
    assert region_loop._probe_after_budget_s(100.0, 70.0) == pytest.approx(
        30.0 - oracle.STAGE_MARGIN_S)
    assert region_loop._probe_after_budget_s(72.0, 70.0) == 0.0
    monkeypatch.setattr(mesh_probe, "probe_file",
                        lambda *a, **k: pytest.fail("no probe may start with no time left"))
    assert region_loop._probe_delivered(object(), 10.0, budget_s=0.0) == {
        "meshes": None, "kind": region_loop.PROBE_AFTER_NO_TIME}


def test_a_kept_application_is_probed_inside_the_runs_remainder(monkeypatch):
    from tests.unit.cad.test_repair_resolve_sub_mesh_gap import _REGION, _gap_clash, _Work

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESHABILITY_PASS", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", True)
    budgets: list[float] = []

    def probe(shape, diagonal, *, budget_s):
        budgets.append(budget_s)
        return {"meshes": True, "kind": ""}

    monkeypatch.setattr(region_loop, "_probe_delivered", probe)
    work = _Work((_gap_clash(),), commit_on=1)
    work.shape = object()
    region_loop._meshability_pass(work, (_REGION,), remaining=lambda: 100.0, reserve=70.0,
                                  tolerance_mm=1e-4, max_ops=6)
    assert budgets == [pytest.approx(30.0 - oracle.STAGE_MARGIN_S)]


def test_stage_budget_is_the_lesser_of_cap_and_remainder_less_margin():
    assert oracle.stage_budget_s(120.0, 1000.0) == 120.0
    assert oracle.stage_budget_s(120.0, 30.0) == 25.0
    assert oracle.stage_budget_s(120.0, 3.0) == 0.0
    assert oracle.stage_budget_s(120.0, float("nan")) == 0.0
    assert oracle.stage_budget_s(180.0, 50.0, margin_s=10.0) == 40.0
