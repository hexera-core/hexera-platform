"""Diagnostics-only CAD repair core."""

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairInput,
    RepairMeasurement,
    RepairMode,
    RepairPolicy,
    RepairProfile,
    RepairReport,
    RepairResult,
    RepairStatus,
    RepairTarget,
)


def inspect_geometry(*args, **kwargs):
    from meshpipeline.cad.repair.inspect import inspect_geometry as _inspect_geometry

    return _inspect_geometry(*args, **kwargs)


def repair_input_for(*args, **kwargs):
    from meshpipeline.cad.repair.inspect import repair_input_for as _repair_input_for

    return _repair_input_for(*args, **kwargs)

__all__ = [
    "DefectCode",
    "DefectSeverity",
    "RepairDefect",
    "RepairInput",
    "RepairMeasurement",
    "RepairMode",
    "RepairPolicy",
    "RepairProfile",
    "RepairReport",
    "RepairResult",
    "RepairStatus",
    "RepairTarget",
    "inspect_geometry",
    "repair_input_for",
]
