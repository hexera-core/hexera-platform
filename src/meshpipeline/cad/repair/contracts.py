# Responsibility: Define the stable product report emitted by CAD repair inspection.
# Boundaries: typed report shape only; it reads no geometry and mutates nothing.
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum


class RepairMode(str, Enum):
    inspect = "inspect"
    repair = "repair"


class RepairProfile(str, Enum):
    conservative = "conservative"
    mesh_ready = "mesh_ready"
    manual_review = "manual_review"


class RepairTarget(str, Enum):
    standalone = "standalone"
    meshing = "meshing"


class RepairStatus(str, Enum):
    clean = "clean"
    repairable = "repairable"
    repaired = "repaired"
    unrepairable = "unrepairable"
    inconclusive = "inconclusive"


class DefectSeverity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    fatal = "fatal"


class DefectCode(str, Enum):
    invalid_brep = "invalid_brep"
    open_shell = "open_shell"
    wire_gap = "wire_gap"
    small_edge = "small_edge"
    small_face = "small_face"
    degenerate_edge = "degenerate_edge"
    curve_inconsistency = "curve_inconsistency"
    self_intersection = "self_intersection"
    non_manifold_surface = "non_manifold_surface"
    duplicate_surface_data = "duplicate_surface_data"
    engine_staging_failure = "engine_staging_failure"


@dataclass(frozen=True, slots=True)
class RepairPolicy:
    mode: RepairMode
    profile: RepairProfile
    target: RepairTarget
    engine: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "profile": self.profile.value,
            "target": self.target.value,
            "engine": self.engine,
        }


@dataclass(frozen=True, slots=True)
class RepairInput:
    source_id: str
    sha256: str
    suffix: str
    interpretation_id: str
    unit: str
    basis: str
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "sha256": self.sha256,
            "suffix": self.suffix,
            "interpretation_id": self.interpretation_id,
            "unit": self.unit,
            "basis": self.basis,
            "size_bytes": int(self.size_bytes),
        }


@dataclass(frozen=True, slots=True)
class RepairDefect:
    code: DefectCode
    severity: DefectSeverity
    message: str
    count: int = 1
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "count": int(self.count),
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class RepairMeasurement:
    name: str
    value: object
    unit: str = ""

    def to_dict(self) -> dict:
        out = {"name": self.name, "value": self.value}
        if self.unit:
            out["unit"] = self.unit
        return out


@dataclass(frozen=True, slots=True)
class RepairReport:
    defects: tuple[RepairDefect, ...]
    measurements: tuple[RepairMeasurement, ...]
    operations: tuple[dict, ...]
    summary: str
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "defects": [d.to_dict() for d in self.defects],
            "measurements": [m.to_dict() for m in self.measurements],
            "operations": [dict(op) for op in self.operations],
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class RepairResult:
    status: RepairStatus
    input: RepairInput
    policy: RepairPolicy
    report: RepairReport
    output: dict | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "input": self.input.to_dict(),
            "policy": self.policy.to_dict(),
            "report": self.report.to_dict(),
            "output": dict(self.output) if self.output is not None else None,
        }


def status_from_defects(defects: Sequence[RepairDefect]) -> RepairStatus:
    severities = {d.severity for d in defects}
    if DefectSeverity.fatal in severities:
        return RepairStatus.unrepairable
    if DefectSeverity.error in severities:
        return RepairStatus.repairable
    return RepairStatus.clean


def clean_report(
    summary: str,
    measurements: Sequence[RepairMeasurement] = (),
    diagnostics: dict | None = None,
) -> RepairReport:
    return RepairReport(
        defects=(),
        measurements=tuple(measurements),
        operations=({"name": "inspect", "mutated": False},),
        summary=summary,
        diagnostics=diagnostics or {},
    )
