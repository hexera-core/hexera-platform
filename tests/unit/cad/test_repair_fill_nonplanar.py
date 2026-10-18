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

SHAPE = object()
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

    def configure(*, flag=True, targets=None, wire="WIRE", span_ratio=0.25, planar=False,
                  g0=0.001, g1=True, builder="BRepFill_Filling", bulges=False,
                  sew=("SEWN", "SEWING")):
        del targets                       # the caller passes these to apply, not to the doubles
        calls.clear()

        def logged(name, value):
            def double(*args, **kwargs):
                calls.append(name)
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
        return op

    return configure


@pytest.mark.parametrize("setup,reason", [
    ({"flag": False}, "filling_disabled"), ({"targets": ()}, "no loop was named"),
    ({"targets": (REF, REF2)}, "one_loop_only"), ({"wire": None}, "bounds no closed free loop"),
    ({"span_ratio": 0.8}, "too_large"), ({"planar": True}, "planar"),
    ({"g0": 0.02}, "boundary_error"), ({"bulges": True}, "patch_bulges"),
    ({"sew": None}, "sewing_failed")])
def test_each_decline_returns_the_input_and_names_its_reason(setup, reason, doubles):
    out = doubles(**setup).apply(SHAPE, setup.get("targets", (REF,)), tolerance_mm=0.01)
    assert out.shape is SHAPE and reason in out.evidence["declined"]


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
    out = doubles(g1=False, builder="GeomPlate").apply(SHAPE, (REF,), tolerance_mm=0.01)
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
