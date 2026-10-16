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

    assert not verdict.kept and verdict.reason == "target_remains"
    assert measure(session.shape).to_dict() == before.to_dict(), "the rollback did not restore"


def test_the_session_writes_a_step_in_millimetres(tmp_path):
    out = tmp_path / "out.step"
    RepairSession(_read(write_step(tmp_path / "box.step", "MM"))).write(out)

    assert out.exists() and out.stat().st_size > 0
    assert "MILLI" in out.read_text(encoding="utf-8", errors="ignore").upper()
