# Responsibility: Verify the small-face sweep against the real kernel: an authored Boolean sliver
#                  is removed and reported gone on the kernel's own history, the part suffers no
#                  geometric damage, the removal cap and the not-actually-small defence decline
#                  with the shape untouched, and the session keeps a real sweep.
# Boundaries: the native tier - the only place OpenCASCADE exists.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_small_edge_step

import meshpipeline.cad.repair.operations as operations
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect, localize_scan
from meshpipeline.cad.repair.operations import FixSmallFaces

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture
def _sweep_on(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", True)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _sliver(tmp_path):
    """The authored Boolean sliver: the small-edge fixture's notch ends in a 0.002 mm wide face,
    which `localize_scan` reports as `small_face` - the same fixture, read for its other defect."""
    shape = _read(write_small_edge_step(tmp_path / "sliver.step"))
    found, complete = localize_scan(shape)
    assert complete
    slivers = [d for d in found if d.code is DefectCode.small_face]
    assert slivers, "the fixture no longer carries a small_face defect"
    return shape, slivers[0]


def test_the_sweep_removes_an_authored_sliver_and_the_kernel_history_says_it_is_gone(
        tmp_path, _sweep_on):
    from meshpipeline.cad.repair.caps import cumulative_refusal, measure, sanity_refusal
    from meshpipeline.cad.repair.identity import GONE

    shape, defect = _sliver(tmp_path)
    before = measure(shape)
    op = FixSmallFaces()
    targets = op.targets_for(defect)
    assert targets, "the operation could not aim at a located small face"

    outcome = op.apply(shape, targets, tolerance_mm=0.1)
    after = measure(outcome.shape)

    # EXACTLY THE SLIVER WENT, and the operation said so - which is what the session's cap reads.
    assert outcome.evidence.get("faces_removed") == before.faces - after.faces == 1
    assert sanity_refusal(before, after,
                          declared_faces_removed=outcome.evidence["faces_removed"]) == ""
    refusal = cumulative_refusal(before, after, declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert refusal == "", refusal

    # The claim rests on the kernel's own record, not on diffing scans.
    fate, _successor = outcome.mapping.fate_of(targets[0].fingerprint)
    assert fate == GONE and outcome.mapping.basis == "kernel"

    # And the re-inspection agrees: no small face is left to find.
    found, _ = localize_scan(outcome.shape)
    assert not [d for d in found if d.code is DefectCode.small_face]


def test_the_session_keeps_a_real_sweep(tmp_path, _sweep_on):
    from meshpipeline.cad.repair.session import RepairSession

    shape, _ = _sliver(tmp_path)
    session = RepairSession(shape)
    defect = next(d for d in session.defects() if d.code is DefectCode.small_face)
    op = FixSmallFaces()

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.1)

    assert verdict.kept, f"{verdict.status}: {verdict.reason}"
    assert not [d for d in session.defects() if d.code is DefectCode.small_face]


def test_the_sweep_aimed_at_a_sliver_edge_removes_the_owning_face_and_is_kept(
        tmp_path, _sweep_on):
    # The fixture's sliver face is also found through its own 0.002 mm edges. Aimed at ONE of
    # those edge defects, the sweep removes the face that owns it, the edge goes with the face,
    # and the session's progress rule sees the edge defect gone - which is what stops a file whose
    # slivers were found edge-first from stalling before the sweep is ever proposed.
    from meshpipeline.cad.repair.session import RepairSession

    shape, _ = _sliver(tmp_path)
    session = RepairSession(shape)
    edge = next(d for d in session.defects() if d.code is DefectCode.small_edge)
    op = FixSmallFaces()
    targets = op.targets_for(edge)
    assert targets and targets[0].entity_type == "edge"

    verdict = session.try_op(op, targets, edge.code, tolerance_mm=0.1)

    assert verdict.kept, f"{verdict.status}: {verdict.reason}"
    assert not [d for d in session.defects()
                if d.code in (DefectCode.small_edge, DefectCode.small_face)]


def test_an_edge_no_sliver_face_owns_is_declined(tmp_path, _sweep_on):
    # A sliver edge on a REAL face (a short edge of a chamfer, say) is fix_small_edges' question,
    # not a licence to remove the face. Edge 1 of the fixture belongs to full walls.
    shape, _ = _sliver(tmp_path)
    ordinary = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                            entity_type="edge", entity_index=1, message="not on a sliver",
                            fingerprint="edge:feedbeef")
    op = FixSmallFaces()

    outcome = op.apply(shape, op.targets_for(ordinary), tolerance_mm=0.1)

    assert "not_small" in str(outcome.evidence.get("declined"))
    assert outcome.shape is shape


def test_a_sweep_over_its_removal_cap_declines_and_the_shape_is_untouched(
        tmp_path, _sweep_on, monkeypatch):
    # The cap is read at apply time, so it can be tightened here without authoring a part with
    # dozens of slivers: one removal over a cap of zero must decline, not remove.
    from meshpipeline.cad.repair.caps import measure

    monkeypatch.setattr(operations, "MAX_SMALL_FACE_REMOVALS", 0)
    shape, defect = _sliver(tmp_path)
    before = measure(shape)
    op = FixSmallFaces()

    outcome = op.apply(shape, op.targets_for(defect), tolerance_mm=0.1)

    assert "swept_too_many" in str(outcome.evidence.get("declined"))
    assert outcome.shape is shape, "a decline must hand back the input object itself"
    assert measure(outcome.shape).faces == before.faces


def test_a_target_that_is_not_actually_small_is_declined_before_the_kernel_runs(
        tmp_path, _sweep_on):
    # The model-planner defence: a directive may name any face it likes, and the operation
    # re-measures rather than trusting the aim. Face 1 of the fixture is a full wall.
    shape, _ = _sliver(tmp_path)
    wall = EntityDefect(code=DefectCode.small_face, severity=DefectSeverity.warning,
                        entity_type="face", entity_index=1, message="not actually small",
                        fingerprint="face:feedface")
    op = FixSmallFaces()

    outcome = op.apply(shape, op.targets_for(wall), tolerance_mm=0.1)

    assert "not_small" in str(outcome.evidence.get("declined"))
    assert outcome.shape is shape
