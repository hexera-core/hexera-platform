# Responsibility: Run CAD simplification inspection on one verified materialized geometry: read the solid, find its removable features, and price them.
# Boundaries: diagnostics only; it never stages, simplifies, uploads, or replaces geometry. A file that is not a CAD solid is reported inconclusive, not refused.
# Collaborates with: cad/simplify/recognise.py (the features), cad/repair/brep.py (the same reader CAD repair uses), cad/repair/inspect.repair_input_for (the same source identity), pipeline/simplify_inspect.py (the node that calls it).
from __future__ import annotations

from pathlib import Path

from meshpipeline.cad.simplify.contracts import (
    SimplifyMode,
    SimplifyPolicy,
    SimplifyProfile,
    SimplifyReport,
    SimplifyResult,
    SimplifyStatus,
    SimplifyTarget,
    status_from_candidates,
)
from meshpipeline.contracts.geometry_source import MaterializedGeometry
from meshpipeline.contracts.intake_formats import GeometryKind, geometry_kind


def _summary(candidates) -> str:
    removable = [c for c in candidates if c.removable]
    if not removable:
        return "No feature a simplification could remove."
    holes = sum(c.kind.value == "hole" for c in removable)
    blends = sum(c.kind.value == "blend" for c in removable)
    cells = sum(c.est_surface_cells for c in removable)
    return (f"{len(removable)} removable features ({holes} holes, {blends} blends), about {cells} surface "
            f"cells between them. Nothing was changed.")


def inspect_features(
    geometry: MaterializedGeometry,
    *,
    profile: SimplifyProfile = SimplifyProfile.inspect_only,
    target: SimplifyTarget = SimplifyTarget.standalone,
    engine: str = "",
) -> SimplifyResult:
    from meshpipeline.cad.repair.inspect import repair_input_for

    policy = SimplifyPolicy(mode=SimplifyMode.inspect, profile=profile, target=target, engine=engine)
    path = Path(geometry.path)
    if geometry_kind(path) is not GeometryKind.cad:
        # a triangle surface has no faces to defeature; it is not a finding about the part
        return SimplifyResult(
            status=SimplifyStatus.inconclusive, input=repair_input_for(geometry), policy=policy,
            report=SimplifyReport(candidates=(), summary="Simplification reads CAD solids only.",
                                  diagnostics={"not_inspected": "not_a_cad_solid", "path_suffix": path.suffix.lower()}))

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.simplify.recognise import recognise

    candidates, diag = recognise(_read_shape(path))
    return SimplifyResult(
        status=status_from_candidates(candidates),
        input=repair_input_for(geometry),
        policy=policy,
        report=SimplifyReport(candidates=tuple(candidates), summary=_summary(candidates),
                              diagnostics={"diagonal": diag, "unit": geometry.interpretation.unit}),
    )
