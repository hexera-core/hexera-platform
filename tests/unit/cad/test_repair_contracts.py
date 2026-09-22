# Responsibility: Verify CAD repair reports have stable vocabulary, status derivation and JSON shape.
from __future__ import annotations

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairInput,
    RepairMode,
    RepairPolicy,
    RepairProfile,
    RepairReport,
    RepairResult,
    RepairStatus,
    RepairTarget,
    clean_report,
    status_from_defects,
)


def _input() -> RepairInput:
    return RepairInput(
        source_id="source-1",
        sha256="a" * 64,
        suffix=".step",
        interpretation_id="interp-1",
        unit="millimetre",
        basis="user_confirmed",
        size_bytes=123,
    )


def test_clean_report_derives_clean_status_and_serializes():
    policy = RepairPolicy(
        mode=RepairMode.inspect,
        profile=RepairProfile.conservative,
        target=RepairTarget.standalone,
    )
    result = RepairResult(
        status=status_from_defects(()),
        input=_input(),
        policy=policy,
        report=clean_report("No repair needed."),
    )

    payload = result.to_dict()

    assert payload["status"] == "clean"
    assert payload["policy"]["mode"] == "inspect"
    assert payload["report"]["summary"] == "No repair needed."
    assert payload["report"]["defects"] == []
    assert payload["output"] is None


def test_error_severity_is_repairable_and_warning_is_clean_enough():
    warning = RepairDefect(
        code=DefectCode.small_edge,
        severity=DefectSeverity.warning,
        message="small edges found",
        count=2,
    )
    error = RepairDefect(
        code=DefectCode.invalid_brep,
        severity=DefectSeverity.error,
        message="B-rep validity failed",
    )

    assert status_from_defects((warning,)) is RepairStatus.clean
    assert status_from_defects((warning, error)) is RepairStatus.repairable


def test_fatal_defect_is_unrepairable():
    defect = RepairDefect(
        code=DefectCode.engine_staging_failure,
        severity=DefectSeverity.fatal,
        message="could not read CAD file",
    )

    assert status_from_defects((defect,)) is RepairStatus.unrepairable


def test_report_serialization_keeps_codes_and_details_stable():
    defect = RepairDefect(
        code=DefectCode.duplicate_surface_data,
        severity=DefectSeverity.warning,
        message="duplicate triangles found",
        count=3,
        details={"duplicate_faces": 3},
    )
    report = RepairReport(
        defects=(defect,),
        measurements=(),
        operations=({"name": "inspect_surface", "mutated": False},),
        summary="Repair recommended before meshing.",
        diagnostics={"format": "stl"},
    )

    payload = report.to_dict()

    assert payload["defects"][0]["code"] == "duplicate_surface_data"
    assert payload["defects"][0]["details"] == {"duplicate_faces": 3}
    assert payload["operations"] == [{"name": "inspect_surface", "mutated": False}]
