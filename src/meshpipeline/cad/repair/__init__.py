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
from meshpipeline.cad.repair.inspect import inspect_geometry, repair_input_for

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
