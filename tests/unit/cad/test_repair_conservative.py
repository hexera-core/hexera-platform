# Responsibility: Verify a conservative repair refuses its own result whenever that result left the caps, and cannot run at all unless a deployment opted in.
from __future__ import annotations

from pathlib import Path

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import (
    _CONSERVATIVE_OPERATIONS,
    RepairRefused,
    RepairUnavailable,
    _refusal,
    repair_step_file,
    status_for,
)
from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairProfile,
    RepairReport,
    RepairStatus,
)

# The caps are measured on the RESULT, never trusted from the request, because an OpenCASCADE
# tolerance is a request: ShapeFix may raise a sub-shape's tolerance well past what it was given.
# These tests drive `_refusal` directly with before/after measurements, which is the whole of that
# judgement - the kernel itself is exercised in the native tier.

_BEFORE = {"valid": True, "diagonal_mm": 200.0, "max_tolerance_mm": 0.01,
           "solids": 1, "shells": 1, "faces": 20, "edges": 60, "vertices": 40}


def _after(**changes) -> dict:
    return {**_BEFORE, **changes}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)


# THE SWITCH


def test_repair_is_refused_outright_when_the_deployment_has_not_opted_in(monkeypatch, tmp_path):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", False)
    # a file that does not exist: proof the refusal lands BEFORE any kernel call, since reading it
    # would raise FileNotFoundError instead
    with pytest.raises(RepairUnavailable, match="CAD_REPAIR_ENABLED"):
        repair_step_file(tmp_path / "absent.step", tmp_path / "out.step")


def test_a_profile_this_module_does_not_implement_is_not_improvised(tmp_path):
    for profile in (RepairProfile.mesh_ready, RepairProfile.manual_review):
        with pytest.raises(RepairUnavailable, match="operator"):
            repair_step_file(tmp_path / "a.step", tmp_path / "b.step", profile=profile)


def test_being_unable_to_repair_is_never_a_verdict_on_the_file():
    # the two outcomes must stay distinguishable: one is about our deployment, the other about
    # the customer's geometry, and conflating them sends a customer to fix a sound file
    assert not issubclass(RepairUnavailable, RepairRefused)
    assert not issubclass(RepairRefused, RepairUnavailable)


# THE AFTER-CHECKS


def test_a_repair_that_left_nothing_to_mesh_is_refused():
    assert "no faces" in _refusal(_BEFORE, _after(faces=0))


def test_a_repair_that_deleted_a_face_is_refused():
    reason = _refusal(_BEFORE, _after(faces=19))
    # DELETING A FACE IS DEFEATURING - a judgement about what the part is for
    assert "removed 1 face" in reason and "operator decision" in reason


def test_face_removal_is_allowed_only_where_a_deployment_said_so(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", True)
    assert _refusal(_BEFORE, _after(faces=19)) == ""


def test_a_repair_that_lost_a_solid_is_refused():
    # FOUND BY THE REAL KERNEL, not by reasoning: a sound box went through sewing and came out
    # with no solid. A closed body reduced to loose surfaces is damage - most meshers either
    # refuse it or quietly mesh something else - so it is refused whatever produced it.
    reason = _refusal(_BEFORE, _after(solids=0))
    assert "lost 1 solid" in reason and "damage rather than repair" in reason


def test_a_shape_that_never_had_a_solid_is_not_refused_for_not_gaining_one():
    # a loose surface model is why sewing exists; it is not required to become a volume
    surfaces = {**_BEFORE, "solids": 0}
    assert _refusal(surfaces, {**surfaces, "shells": 1}) == ""


def test_a_repair_that_inflated_a_tolerance_past_the_cap_is_refused():
    reason = _refusal(_BEFORE, _after(max_tolerance_mm=0.5))
    # "valid" that means "the kernel agreed to stop complaining" is not a repair
    assert "0.5" in reason and "cap" in reason


def test_a_tolerance_exactly_at_the_cap_is_accepted():
    # the cap is a ceiling, not a strict bound - a repair that landed on it stayed inside it
    assert _refusal(_BEFORE, _after(max_tolerance_mm=0.1)) == ""


def test_a_repair_that_moved_the_part_too_far_is_refused():
    # 200 mm diagonal, 0.001 cap => 0.2 mm of movement allowed; 1 mm is five times that
    reason = _refusal(_BEFORE, _after(diagonal_mm=201.0))
    assert "moved the part" in reason


def test_movement_inside_the_cap_is_accepted():
    assert _refusal(_BEFORE, _after(diagonal_mm=200.1)) == ""


def test_a_repair_that_made_a_valid_shape_invalid_is_refused():
    assert "invalid" in _refusal(_BEFORE, _after(valid=False))


def test_a_repair_that_leaves_an_already_invalid_shape_invalid_is_not_refused_for_that():
    # it was invalid on arrival - that is why it is here. Whether the improvement is enough to
    # mesh is a reviewer's call, and the report says so rather than this refusing it.
    assert _refusal({**_BEFORE, "valid": False}, _after(valid=False)) == ""


def test_a_part_with_no_measurable_size_is_not_refused_by_a_division():
    # a degenerate bounding box must not become a ZeroDivisionError in the deviation check
    flat = {**_BEFORE, "diagonal_mm": 0.0}
    assert _refusal(flat, {**flat, "faces": 20}) == ""


def test_a_clean_repair_is_accepted():
    assert _refusal(_BEFORE, _after(max_tolerance_mm=0.02)) == ""


# WHAT THE PROFILE IS ALLOWED TO DO


def test_the_conservative_operation_set_closes_geometry_and_never_deletes_it():
    assert _CONSERVATIVE_OPERATIONS == (
        "fix_small_edges", "fix_wireframe", "fix_face_boundaries", "sew_shells")
    # anything that removes or re-draws geometry is deliberately absent from a conservative pass
    for forbidden in ("remove", "delete", "defeature", "simplify", "rebuild"):
        assert not any(forbidden in op for op in _CONSERVATIVE_OPERATIONS)


# WHAT A COMPLETED REPAIR AMOUNTS TO


def test_a_repair_with_a_warning_is_still_a_repair():
    report = RepairReport(
        defects=(RepairDefect(code=DefectCode.invalid_brep, severity=DefectSeverity.warning,
                              message="still not fully valid"),),
        measurements=(), operations=(), summary="")
    # a warning asks for a person's eyes; it does not retract the repair that was made
    assert status_for(report) is RepairStatus.repaired


def test_a_repair_reporting_an_error_is_not_called_repaired():
    report = RepairReport(
        defects=(RepairDefect(code=DefectCode.invalid_brep, severity=DefectSeverity.error,
                              message="no"),),
        measurements=(), operations=(), summary="")
    assert status_for(report) is RepairStatus.unrepairable


def test_a_clean_report_is_repaired():
    assert status_for(RepairReport(defects=(), measurements=(), operations=(),
                                   summary="")) is RepairStatus.repaired


# THE INPUT IS NEVER TOUCHED


def test_the_source_file_is_only_ever_read(monkeypatch, tmp_path):
    source = tmp_path / "part.step"
    source.write_bytes(b"ISO-10303-21;\n")
    before = source.read_bytes()

    monkeypatch.setattr("meshpipeline.cad.repair.conservative._occ", lambda: None)
    monkeypatch.setattr("meshpipeline.cad.repair.brep._read_shape",
                        lambda p: pytest.fail("the kernel should not be reached in this test"))
    with pytest.raises(BaseException):
        repair_step_file(source, tmp_path / "out.step")

    # whatever happened, the customer's bytes are exactly as they arrived
    assert source.read_bytes() == before
    assert not Path(tmp_path / "out.step").exists()
