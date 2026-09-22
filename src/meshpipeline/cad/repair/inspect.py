# Responsibility: Dispatch repair inspection for one verified materialized geometry.
# Boundaries: diagnostics only; it never stages, repairs, uploads, or replaces geometry.
from __future__ import annotations

from pathlib import Path

from meshpipeline.cad.repair.brep import inspect_brep_file
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
    status_from_defects,
)
from meshpipeline.cad.repair.surface import inspect_surface_file
from meshpipeline.contracts.geometry_source import MaterializedGeometry

_BREP_SUFFIXES = {".step", ".stp", ".iges", ".igs"}
_SURFACE_SUFFIXES = {".stl", ".vtp"}


def repair_input_for(geometry: MaterializedGeometry) -> RepairInput:
    return RepairInput(
        source_id=geometry.ref.source_id,
        sha256=geometry.ref.sha256,
        suffix=geometry.ref.suffix_hint,
        interpretation_id=geometry.interpretation.interpretation_id,
        unit=geometry.interpretation.unit,
        basis=geometry.interpretation.basis,
        size_bytes=geometry.ref.size_bytes,
    )


def _unsupported_report(suffix: str) -> RepairReport:
    return RepairReport(
        defects=(
            RepairDefect(
                code=DefectCode.engine_staging_failure,
                severity=DefectSeverity.fatal,
                message=f"CAD repair inspection does not support geometry suffix {suffix!r}.",
            ),
        ),
        measurements=(),
        operations=({"name": "inspect_dispatch", "mutated": False},),
        summary="Automatic repair was not safe for this geometry.",
        diagnostics={"path_suffix": suffix},
    )


def inspect_geometry(
    geometry: MaterializedGeometry,
    *,
    profile: RepairProfile = RepairProfile.conservative,
    target: RepairTarget = RepairTarget.standalone,
    engine: str = "",
) -> RepairResult:
    path = Path(geometry.path)
    suffix = path.suffix.lower()
    if suffix in _BREP_SUFFIXES:
        report = inspect_brep_file(path)
    elif suffix in _SURFACE_SUFFIXES:
        report = inspect_surface_file(path)
    else:
        report = _unsupported_report(suffix)

    return RepairResult(
        status=status_from_defects(report.defects),
        input=repair_input_for(geometry),
        policy=RepairPolicy(
            mode=RepairMode.inspect,
            profile=profile,
            target=target,
            engine=engine,
        ),
        report=report,
        output=None,
    )
