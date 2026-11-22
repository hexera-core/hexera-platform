# Responsibility: Verify the session's rollback and progress rule against the real kernel - a restored shape is the shape that arrived, and a real repair really is reported as progress.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_open_shell_step, write_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.session import KEPT, RepairSession

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def test_a_rollback_leaves_the_shape_measurably_identical(tmp_path):
    # ROLLBACK IS A HANDLE SWAP, which is only true because no operation mutates its input. If one
    # did, this test is where that shows up - the restored shape would differ from the one that
    # arrived.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FixFaceBoundary

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    session = RepairSession(shape)
    before = measure(session.shape)

    # A sound box has no defect to aim at, so this operation cannot make progress and the session
    # must roll it back.
    op = FixFaceBoundary()
    from meshpipeline.cad.repair.identity import EntityRef

    ref = EntityRef(entity_type="face", fingerprint="face:nonexistent", entity_index=1)
    verdict = session.try_op(op, (ref,), DefectCode.curve_inconsistency, tolerance_mm=0.01)

    assert not verdict.kept
    after = measure(session.shape)
    assert after.to_dict() == before.to_dict()


def test_a_real_repair_of_a_real_hole_is_reported_as_progress(tmp_path, monkeypatch):
    # THE END-TO-END CLAIM OF THIS WHOLE SLICE, at the session level: a located defect, an
    # operation aimed at it, and a re-inspection that confirms THAT defect is gone.
    #
    # THE BORE FIXTURE, NOT THE OPEN SHELL. A box missing a whole face spans 0.8165 of its own
    # diagonal, which the span cap correctly calls a missing wall - so filling declines it and the
    # session rolls it back. That case is the rollback test below. The bore's rims span 0.1596 and
    # are the geometry filling exists for.
    #
    # Patching ONE rim is correct here. That patching BOTH would seal the customer's bolt hole is
    # a planning decision, refused in plan.py and tested there.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)

    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FillPlanarHole

    session = RepairSession(_read(write_missing_bore_wall_step(tmp_path / "bore.step")))
    holes = [d for d in session.defects() if d.code is DefectCode.open_shell]
    assert len(holes) == 2, f"expected two rims, found {len(holes)}"

    op = FillPlanarHole()
    verdict = session.try_op(op, op.targets_for(holes[0]), holes[0].code, tolerance_mm=0.02)

    assert verdict.status == KEPT, f"rolled back because {verdict.reason}"
    # GEOMETRIC, NOT KERNEL. The patch leaves the rim edge where it was and the history records it
    # unmoved, so nothing in the history explains the rim's fate; the conclusion that the defect is
    # gone came from diffing the two scans.
    assert verdict.basis == "geometric"
    remaining = [d for d in session.defects()
                 if d.code is DefectCode.open_shell and d.fingerprint == holes[0].fingerprint]
    assert not remaining, "the rim is still reported at the same entity"
    # THE OTHER RIM IS UNTOUCHED, and still reported. One patch, one rim.
    assert [d for d in session.defects() if d.code is DefectCode.open_shell], \
        "both rims were patched by a single operation"


def test_a_missing_wall_is_rolled_back_rather_than_lidded(tmp_path, monkeypatch):
    # The natural rollback case: filling declines a 0.8165-ratio opening as a missing wall, so no
    # progress is made and the session restores the shape.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)

    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FillPlanarHole

    session = RepairSession(_read(write_open_shell_step(tmp_path / "holed.step")))
    before = measure(session.shape)
    holes = [d for d in session.defects() if d.code is DefectCode.open_shell]
    assert holes

    op = FillPlanarHole()
    verdict = session.try_op(op, op.targets_for(holes[0]), holes[0].code, tolerance_mm=0.02)

    # the operation REFUSED (the span cap), which the verdict says in its own words rather than as
    # a generic failure. Not pinned character for character: the span ratio is a measurement.
    assert not verdict.kept
    assert verdict.reason.startswith("declined:") and "too_large" in verdict.reason, verdict.reason
    assert measure(session.shape).to_dict() == before.to_dict(), "the rollback did not restore"


def test_the_session_writes_a_step_in_millimetres(tmp_path):
    out = tmp_path / "out.step"
    RepairSession(_read(write_step(tmp_path / "box.step", "MM"))).write(out)

    assert out.exists() and out.stat().st_size > 0
    assert "MILLI" in out.read_text(encoding="utf-8", errors="ignore").upper()


def _box_with_a_displaced_pcurve(shift_mm: float):
    """A 100 x 100 x 10 box whose first face carries one edge's pcurve shifted `shift_mm` in the
    face's parameter plane, in memory - no file is written.

    WHAT IT IS: an edge whose 2D trace on its face no longer agrees with its 3D curve, the
    "written down wrong, drawn right" damage a translator leaves behind. BRepCheck reports it on
    the face as `UnorientableShape` (the face's wire no longer closes in its own parameter
    plane), which `localize` maps to an `invalid_brep` because that status has no narrower code -
    and `invalid_brep` is a defect ONLY `fix_shape_global` claims. `ShapeFix_Shape` rebuilds the
    pcurve from the 3D edge, which is exactly the repair the whole-shape pass exists for.
    """
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Vec2d
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    shape = BRepPrimAPI_MakeBox(100.0, 100.0, 10.0).Shape()
    face = TopoDS.Face_s(TopExp_Explorer(shape, TopAbs_FACE).Current())
    edge = TopoDS.Edge_s(TopExp_Explorer(face, TopAbs_EDGE).Current())
    pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0).Copy()
    pcurve.Translate(gp_Vec2d(shift_mm, 0.0))
    BRep_Builder().UpdateEdge(edge, pcurve, face, BRep_Tool.Tolerance_s(edge))
    return shape


def test_the_whole_shape_fallback_reaches_the_kernel_through_the_session():
    """The fallback is REACHABLE, proved through the session rather than from the planner alone.

    It was blocked twice over: `targets_for` returned `()`, which the planner skips, and the
    session's `_carries` check over an empty target tuple is False, which is `target_absent`. Now
    it names the defect's own entity, so the operation gets applied, measured and judged like any
    other. What matters is that the verdict comes from running the operation, not from refusing
    to aim it.

    WHY NOT THE SLIVER FIXTURE ANY MORE. This test used to aim the fallback at the
    `write_small_edge_step` sliver's `small_edge`, and expect a `target_remains` rollback. Since
    f0ac2c28 (squashed into #494) `fix_shape_global` deliberately does NOT answer `small_edge` -
    on the real-failure corpus it was 0-for-40 on them, `ShapeFix_Shape` returning an IsSame
    shape with no recorded replacement (see the operation's docstring, and
    test_repair_operations_contract.py, which pins the exclusion) - so `targets_for` correctly
    returns `()` for that defect and the old premise no longer holds. No existing fixture carries
    a defect the fallback still answers, so this one builds the damage it does answer: an
    `invalid_brep` from a displaced pcurve. And here the honest outcome is a KEPT repair, which is
    a stronger proof of reachability than a rollback: the kernel ran, the defect is gone on
    re-inspection, and nothing was added or removed.
    """
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FixShapeGlobal

    session = RepairSession(_box_with_a_displaced_pcurve(0.01))
    op = FixShapeGlobal()
    damaged = next(d for d in session.defects() if d.code is DefectCode.invalid_brep)

    targets = op.targets_for(damaged)
    assert targets, "the fallback could not aim at a located defect"

    verdict = session.try_op(op, targets, damaged.code, tolerance_mm=0.02)

    assert verdict.reason != "target_absent", "the fallback never reached the kernel"
    assert verdict.op == "fix_shape_global"
    assert verdict.status == KEPT, verdict.reason
    assert verdict.delta["after"]["faces"] == verdict.delta["before"]["faces"]
    assert not [d for d in session.defects() if d.fingerprint == damaged.fingerprint], \
        "kept, yet the damaged face is still reported"
