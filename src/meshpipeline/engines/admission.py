# Responsibility: Carry the evidence and rejection shapes used when deciding whether geometry may enter an engine.
# Boundaries: value types only; the decision itself is pipeline/geometry_admission.py.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# NOTE: dimensionality is this codebase's Dimensionality StrEnum value ("2D"/"3D"),
# a str - NOT an int. surface_analysis is the geometry.analysis.analyze_surface() dict
# (optionally carrying "self_intersecting"), passed only in the measured phase.

AdmissionPhase = Literal["declared", "measured"]


@dataclass(frozen=True)
class PatchSummary:
    name: str
    type: str


@dataclass(frozen=True)
class RegionSummary:
    name: str
    kind: str = ""


@dataclass(frozen=True)
class AdmissionEvidence:
    engine: str
    purpose: str
    input_kind: str | None = None
    dimensionality: str | None = None
    patches: tuple[PatchSummary, ...] = ()
    regions: tuple[RegionSummary, ...] = ()
    engine_params: Mapping[str, Any] = field(default_factory=dict)
    surface_analysis: dict | None = None


@dataclass(frozen=True)
class Rejection:
    code: str
    phase: AdmissionPhase
    message: str
    field: str | None = None
    expected: Any | None = None
    actual: Any | None = None
    fix_hint: str | None = None
