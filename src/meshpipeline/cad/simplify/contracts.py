# Responsibility: Define the stable report CAD simplification inspection emits: which features of a part are candidates for removal, what each one is, and what it costs the mesher.
# Boundaries: typed report shape only; it reads no geometry and mutates nothing. The source identity is CAD repair's RepairInput, reused rather than copied, so both reports name the same file the same way.
# Collaborates with: cad/simplify/inspect.py (fills it), pipeline/simplify_inspect.py (records it), docs/superpowers/specs/2026-11-19-cad-simplification-design.md.
"""WHAT CAD SIMPLIFICATION INSPECTION SAYS ABOUT A PART.

The twin of cad/repair/contracts.py. Inspection finds candidates and prices them; it decides nothing
about removing any of them. Sizes are in the file's own unit (`input.unit`) and also as a fraction of
the part's diagonal, so a reader never has to guess a scale.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from meshpipeline.cad.repair.contracts import RepairInput


class SimplifyMode(str, Enum):
    inspect = "inspect"
    simplify = "simplify"


class SimplifyProfile(str, Enum):
    #: report candidates, change nothing (inspection)
    inspect_only = "inspect_only"
    #: remove candidates, and keep only the ones a local simulation check accepts within the
    #: error budget (cad/simplify/simplify.py, cad/simplify/verify.py)
    verified = "verified"


class SimplifyTarget(str, Enum):
    standalone = "standalone"
    meshing = "meshing"


class SimplifyStatus(str, Enum):
    #: no candidate features: nothing a simplification could remove
    none = "none"
    #: candidates found; nothing was removed (inspection never removes)
    candidates = "candidates"
    #: features were removed and the simulation check accepted the result
    simplified = "simplified"
    #: inspection did not reach an answer (not a solid, too large, timed out, failed); says
    #: nothing about the part
    inconclusive = "inconclusive"


class FeatureKind(str, Enum):
    #: a concave full cylinder, with the coaxial cones (chamfers, countersinks) at its mouths
    hole = "hole"
    #: a torus, or a cylinder spanning less than a half turn, under the blend size bound
    blend = "blend"
    #: a concave cylinder spanning a half turn or more but less than a full one: the round end of a
    #: slot or pocket. Reported so a reader sees it, never a removal candidate - measured on the NIST
    #: corpus, OpenCASCADE's defeaturing cannot remove one and hands the shape back unchanged
    slot_end = "slot_end"


@dataclass(frozen=True, slots=True)
class FeatureCandidate:
    id: str
    kind: FeatureKind
    faces: tuple[int, ...]          # 1-based face indices in TopExp.MapShapes order
    radius: float                   # file units
    radius_ratio: float             # radius / part diagonal
    concave: bool
    area: float                     # file units squared
    centroid: tuple[float, float, float]
    bbox: tuple[float, float, float, float, float, float]
    #: the element edge length the FEA plan's hot-spot rule asks of this feature (PLAN-fea-4: 6
    #: elements per quarter-turn), and the number of surface triangles that size puts on its faces.
    #: An ESTIMATE of what the feature costs, to be calibrated against the measured cells a removal
    #: saves (devtools/simplify/harness.py); a ranking, not a promise.
    feature_size: float
    est_surface_cells: int
    removable: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "faces": list(self.faces),
            "radius": self.radius,
            "radius_ratio": self.radius_ratio,
            "concave": self.concave,
            "area": self.area,
            "centroid": list(self.centroid),
            "bbox": list(self.bbox),
            "feature_size": self.feature_size,
            "est_surface_cells": self.est_surface_cells,
            "removable": self.removable,
        }


@dataclass(frozen=True, slots=True)
class SimplifyPolicy:
    mode: SimplifyMode
    profile: SimplifyProfile
    target: SimplifyTarget
    engine: str = ""

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "profile": self.profile.value, "target": self.target.value,
                "engine": self.engine}


@dataclass(frozen=True, slots=True)
class SimplifyReport:
    candidates: tuple[FeatureCandidate, ...]
    summary: str
    operations: tuple[dict, ...] = ({"name": "inspect", "mutated": False},)
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "candidates": [c.to_dict() for c in self.candidates],
            "operations": [dict(op) for op in self.operations],
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class SimplifyResult:
    status: SimplifyStatus
    input: RepairInput
    policy: SimplifyPolicy
    report: SimplifyReport
    output: dict | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "input": self.input.to_dict(),
            "policy": self.policy.to_dict(),
            "report": self.report.to_dict(),
            "output": dict(self.output) if self.output is not None else None,
        }


def status_from_candidates(candidates: Sequence[FeatureCandidate]) -> SimplifyStatus:
    return SimplifyStatus.candidates if any(c.removable for c in candidates) else SimplifyStatus.none
