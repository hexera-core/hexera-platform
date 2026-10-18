# Responsibility: Verify fill_nonplanar_hole's decline order, its area declaration before sewing, and its flag gate, with every kernel call replaced by a double.
# Boundaries: no kernel - the private seams, the free-wire lookup and the measuring helpers are monkeypatched.
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.conservative as conservative
import meshpipeline.cad.repair.localize as localize
import meshpipeline.cad.repair.operations as operations
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityMapping, EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import AREA_DELTA, FILL_OPS, FillNonplanarHole


class _Untouchable:
    """The input shape. Any attribute read on it - the first step of any in-place mutation - is
    recorded and fails, so the operation itself can only pass it on to a seam."""

    touched: list[str] = []

    def __getattr__(self, name):
        _Untouchable.touched.append(name)
        raise AssertionError(f"the operation touched the input shape: .{name}")


SHAPE = _Untouchable()
#: The seams that may be handed the input shape: each only reads it. The kernel builders that
#: write (`_build_patch` on its own wire copy, `_sew_patch` into a NEW shape) are pinned natively.
_READ_ONLY = {"free_wire_containing", "_diagonal", "_build_patch", "_sew_patch",
              "mapping_from_sewing"}
REF = EntityRef(entity_type="edge", fingerprint="edge:rim", entity_index=3)
REF2 = EntityRef(entity_type="edge", fingerprint="edge:other", entity_index=9)
OPEN_LOOP_DEFECT = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                                entity_type="edge", entity_index=3, message="free loop",
                                fingerprint="edge:rim", measurements={"boundary_edges": 4})
_MAPPING = EntityMapping(moved={}, gone=frozenset({"edge:rim"}), basis="kernel")


@pytest.fixture
def doubles(monkeypatch):
    """A factory: configure every seam, get back an operation whose `.calls` logs seam order."""
    calls: list[str] = []
    received: list[str] = []

    def configure(*, flag=True, targets=None, wire="WIRE", span_ratio=0.25, planar=False,
                  g0=0.001, g1=True, builder="BRepFill_Filling", bulges=False,
                  sew=("SEWN", "SEWING")):
        del targets                       # the caller passes these to apply, not to the doubles
        calls.clear()
        received.clear()
        _Untouchable.touched.clear()

        def logged(name, value):
            def double(*args, **kwargs):
                calls.append(name)
                if any(a is SHAPE for a in (*args, *kwargs.values())):
                    received.append(name)
                return value
            return double

        monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", flag)
        monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)
        monkeypatch.setattr(operations, "free_wire_containing", logged("free_wire_containing", wire))
        monkeypatch.setattr(localize, "_diagonal", logged("_diagonal", 100.0))
        monkeypatch.setattr(conservative, "_span", logged("_span", span_ratio * 100.0))
        monkeypatch.setattr(operations, "_is_planar_loop", logged("_is_planar_loop", planar))
        monkeypatch.setattr(operations, "_build_patch",
                            logged("_build_patch", ("PATCH", g0, g1, builder)))
        monkeypatch.setattr(localize, "_face_area", logged("_face_area", 418.9))
        monkeypatch.setattr(operations, "_bulges", logged("_bulges", bulges))
        monkeypatch.setattr(operations, "_sew_patch", logged("_sew_patch", sew))
        monkeypatch.setattr(operations, "box_of", logged("box_of", {
            "bbox_min": [10.0, 0.0, 40.0], "bbox_max": [20.0, 17.3, 60.0]}))
        monkeypatch.setattr(operations, "mapping_from_sewing",
                            logged("mapping_from_sewing", _MAPPING))
        op = FillNonplanarHole()
        op.calls = calls
        op.received = received
        return op

    return configure


@pytest.mark.parametrize("setup,reason", [
    ({"flag": False}, "filling_disabled"), ({"targets": ()}, "no loop was named"),
    ({"targets": (REF, REF2)}, "one_loop_only"), ({"wire": None}, "bounds no closed free loop"),
    ({"span_ratio": 0.8}, "too_large"), ({"planar": True}, "planar"),
    ({"g0": 0.02}, "boundary_error"), ({"bulges": True}, "patch_bulges"),
    ({"sew": None}, "sewing_failed")])
def test_each_decline_returns_the_input_and_names_its_reason(setup, reason, doubles):
    op = doubles(**setup)
    out = op.apply(SHAPE, setup.get("targets", (REF,)), tolerance_mm=0.01)
    assert out.shape is SHAPE and reason in out.evidence["declined"]
    _assert_untouched(op)
    assert "_sew_patch" not in op.calls or reason == "sewing_failed"


def _assert_untouched(op):
    assert _Untouchable.touched == []
    assert set(op.received) <= _READ_ONLY, f"handed the input to {set(op.received) - _READ_ONLY}"


#: Every decline's failing setup, in the order apply must check them.
_ORDER = [("filling_disabled", {"flag": False}), ("no loop was named", {"targets": ()}),
          ("one_loop_only", {"targets": (REF, REF2)}),
          ("bounds no closed free loop", {"wire": None}), ("too_large", {"span_ratio": 0.8}),
          ("planar", {"planar": True}), ("boundary_error", {"g0": 0.02}),
          ("patch_bulges", {"bulges": True}), ("sewing_failed", {"sew": None})]


@pytest.mark.parametrize("first", range(len(_ORDER)))
def test_with_every_later_check_also_failing_the_earliest_reason_wins(first, doubles):
    # Flipping one seam at a time only proves each decline exists. Failing EVERY check from
    # `first` onward at once pins the whole order: the reason reported must be the earliest.
    setup: dict = {}
    for _, failing in reversed(_ORDER[first:]):
        setup.update(failing)                 # the earliest "targets" setting wins, as in apply
    op = doubles(**setup)
    out = op.apply(SHAPE, setup.get("targets", (REF,)), tolerance_mm=0.01)
    reason = _ORDER[first][0]
    assert out.shape is SHAPE and reason in out.evidence["declined"]
    assert not any(out.evidence["declined"].startswith(later) for later, _ in _ORDER[first + 1:])
    _assert_untouched(op)


def test_no_patch_built_at_all_is_a_boundary_error(doubles, monkeypatch):
    op = doubles()
    monkeypatch.setattr(operations, "_build_patch",
                        lambda *a, **kw: (None, float("inf"), False, ""))
    out = op.apply(SHAPE, (REF,), tolerance_mm=0.01)
    assert out.shape is SHAPE and "boundary_error" in out.evidence["declined"]


def test_the_patch_area_is_measured_before_anything_is_sewn(doubles):
    calls = doubles().calls
    doubles().apply(SHAPE, (REF,), tolerance_mm=0.01)
    assert calls.index("_face_area") < calls.index("_sew_patch")


def test_a_too_large_loop_is_declined_even_when_flat(doubles):
    out = doubles(span_ratio=0.8, planar=True).apply(SHAPE, (REF,), tolerance_mm=0.01)
    assert "too_large" in out.evidence["declined"]


def test_a_kept_patch_declares_its_area_and_says_how_it_was_built(doubles):
    op = doubles(g1=False, builder="GeomPlate")
    out = op.apply(SHAPE, (REF,), tolerance_mm=0.01)
    _assert_untouched(op)
    assert out.shape == "SEWN"
    assert out.mapping is _MAPPING
    assert len(out.added) == 1 and out.added[0]
    ev = out.evidence
    assert "declined" not in ev
    assert ev[AREA_DELTA] == pytest.approx(418.9)
    assert ev["patch_is_planar"] is False
    assert ev["g0_error_mm"] == pytest.approx(0.001)
    assert ev["g1_achieved"] is False
    assert ev["builder"] == "GeomPlate"
    assert ev["span_mm"] == pytest.approx(25.0) and ev["span_ratio"] == pytest.approx(0.25)


def test_with_its_flag_off_the_rules_planner_never_proposes_it(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", False)
    assert FillNonplanarHole().targets_for(OPEN_LOOP_DEFECT) == ()


def test_with_its_flag_on_it_aims_at_the_located_loop_like_the_planar_fill(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", True)
    planar = operations.FillPlanarHole().targets_for(OPEN_LOOP_DEFECT)
    assert FillNonplanarHole().targets_for(OPEN_LOOP_DEFECT) == planar
    assert planar and planar[0].fingerprint == "edge:rim"


def test_it_sits_after_the_planar_fill_and_before_the_whole_shape_fix():
    names = [op.name for op in operations.OPERATIONS]
    assert names.index("fill_planar_hole") + 1 == names.index("fill_nonplanar_hole")
    assert names.index("fill_nonplanar_hole") + 1 == names.index("fix_shape_global")
    assert operations.FillPlanarHole.scope < FillNonplanarHole.scope < operations.FixShapeGlobal.scope


def test_both_fills_are_guarded_by_name():
    assert FILL_OPS == {"fill_planar_hole", "fill_nonplanar_hole"}


def test_a_g1_patch_that_missed_tangency_falls_through_to_the_g0_build(monkeypatch):
    # A G1 build that closes the gap but misses tangency is a patch bent toward the wrong face:
    # never kept, and the kept patch's g1_achieved says what was actually kept.
    pytest.importorskip("OCP.Standard", reason="_build_patch names the kernel's failure type")
    built = []

    def filling(edges, supports):
        built.append("g1" if supports is not None else "g0")
        return ("G1-FACE", 0.0001, False) if supports is not None else ("G0-FACE", 0.0002, False)

    monkeypatch.setattr(operations, "_wire_edges", lambda wire: ["e1", "e2"])
    monkeypatch.setattr(operations, "_support_faces", lambda shape, edges: ["f1", "f2"])
    monkeypatch.setattr(operations, "_filling", filling)
    monkeypatch.setattr(operations, "_plate", lambda wire: pytest.fail("plate not needed"))
    face, g0, g1, builder = operations._build_patch("WIRE", tolerance_mm=0.01, shape="SHAPE")
    assert built == ["g1", "g0"]
    assert (face, g1, builder) == ("G0-FACE", False, "BRepFill_Filling")

    monkeypatch.setattr(operations, "_filling", lambda edges, supports: (
        ("G1-FACE", 0.0001, True) if supports is not None else pytest.fail("G1 held")))
    face, g0, g1, _ = operations._build_patch("WIRE", tolerance_mm=0.01, shape="SHAPE")
    assert (face, g1) == ("G1-FACE", True)
