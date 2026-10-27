# Responsibility: Select an internal mesh execution profile from preflight workload evidence.
# Boundaries: Pure policy only; no provider wiring, persistence, dispatch, or user-facing behavior.
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum

POLICY_VERSION = "mesh-execution-profile-v1"


class MeshExecutionProfile(StrEnum):
    STANDARD = "standard"
    HIGH_MEMORY = "high-memory"
    ULTRAMEM = "ultramem"


@dataclass(frozen=True)
class MeshWorkload:
    engine: str
    source_bytes: int | None = None
    surface_triangles: int | None = None
    requested_cells: int | None = None
    region_count: int | None = None
    boundary_layers: int | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name == "engine":
                continue
            value = getattr(self, field.name)
            if value is not None and value < 0:
                raise ValueError(f"{field.name} must be non-negative")


@dataclass(frozen=True)
class ProfileDecision:
    profile: MeshExecutionProfile
    policy_version: str
    reasons: tuple[str, ...]


_STANDARD_CEILINGS = {
    "source_bytes": 500_000_000,
    "surface_triangles": 5_000_000,
    "requested_cells": 8_000_000,
    "region_count": 64,
}
_HIGH_MEMORY_CEILINGS = {
    "source_bytes": 4_000_000_000,
    "surface_triangles": 50_000_000,
    "requested_cells": 100_000_000,
    "region_count": 512,
}
_LAYERED_ENGINES = frozenset({"cfmesh", "gmsh", "snappy", "snappy_multiregion", "vmtk"})
def select_profile(workload: MeshWorkload) -> ProfileDecision:
    evidence_names = (*_STANDARD_CEILINGS, "boundary_layers")
    known = [getattr(workload, name) for name in evidence_names]
    if all(value is None for value in known):
        return ProfileDecision(
            MeshExecutionProfile.STANDARD, POLICY_VERSION, ("no sizing evidence available",))

    standard_reasons: list[str] = []
    ultramem_reasons: list[str] = []
    for name, standard_ceiling in _STANDARD_CEILINGS.items():
        value = getattr(workload, name)
        if value is None:
            continue
        if value > _HIGH_MEMORY_CEILINGS[name]:
            ultramem_reasons.append(f"{name} exceeds high-memory ceiling")
        elif value > standard_ceiling:
            standard_reasons.append(f"{name} exceeds standard ceiling")

    engine = workload.engine.strip().lower()
    layers = workload.boundary_layers
    if engine in _LAYERED_ENGINES and layers is not None:
        if layers > 20:
            ultramem_reasons.append(f"{engine} boundary layers exceed high-memory complexity")
        elif layers > 6:
            standard_reasons.append(f"{engine} boundary layers exceed standard complexity")

    if ultramem_reasons:
        return ProfileDecision(
            MeshExecutionProfile.ULTRAMEM, POLICY_VERSION,
            tuple(ultramem_reasons + standard_reasons))
    if standard_reasons:
        return ProfileDecision(
            MeshExecutionProfile.HIGH_MEMORY, POLICY_VERSION, tuple(standard_reasons))
    missing = [name for name in evidence_names if getattr(workload, name) is None]
    reasons = ((f"sizing evidence incomplete: {', '.join(missing)} unavailable",)
               if missing else ())
    return ProfileDecision(MeshExecutionProfile.STANDARD, POLICY_VERSION, reasons)
