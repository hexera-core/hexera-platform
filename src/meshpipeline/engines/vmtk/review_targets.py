# Responsibility: Enumerate the named things a VMTK review must actually inspect.
# Boundaries: derived from the delivered mesh; it inspects nothing itself.
from __future__ import annotations

from dataclasses import dataclass

from meshpipeline.contracts.review_evidence import InspectionTarget, TargetKind
from meshpipeline.sandbox.polyline_source import Point, Polyline

# vmtk's OWN surface convention: CellEntityIds == 1 is the lumen wall, staged as the patch "wall"
# (see engines/vmtk/viewer_surface.py, `_WALL_ID = 1`). This is an engine-emitted tag, matched by
# exact equality - not a substring guess at what a boundary "sounds like".
_WALL_PATCHES = frozenset({"wall"})


@dataclass(frozen=True)
class VmtkOpeningTarget:

    target_id: str                     # opening:<patch>
    patch_name: str
    label: str
    renderable: bool
    kind: TargetKind = TargetKind.OPENING

    def to_inspection_target(self, *, required: bool, purpose: str) -> InspectionTarget:
        return InspectionTarget(target_id=self.target_id, kind=self.kind,
                                label=self.label, purpose=purpose, required=required)


@dataclass(frozen=True)
class VmtkLayerTarget:

    target_id: str                     # layer_region:<patch>
    patch_name: str
    label: str
    renderable: bool
    kind: TargetKind = TargetKind.LAYER_REGION

    def to_inspection_target(self, *, required: bool, purpose: str) -> InspectionTarget:
        return InspectionTarget(target_id=self.target_id, kind=self.kind,
                                label=self.label, purpose=purpose, required=required)


@dataclass(frozen=True)
class VmtkBranchTarget:

    target_id: str                     # branch:<index>
    point: Point                       # a representative location on the branch (its midpoint)
    span_mm: float                     # how wide a view to frame there
    n_points: int
    label: str
    renderable: bool
    kind: TargetKind = TargetKind.BRANCH

    def to_inspection_target(self, *, required: bool, purpose: str) -> InspectionTarget:
        return InspectionTarget(target_id=self.target_id, kind=self.kind,
                                label=self.label, purpose=purpose, required=required)


VmtkTarget = VmtkOpeningTarget | VmtkLayerTarget | VmtkBranchTarget


def _is_wall(patch_name: str) -> bool:
    return patch_name.strip() in _WALL_PATCHES


def discover_surface_targets(
    patch_names,
) -> tuple[tuple[VmtkOpeningTarget, ...], tuple[VmtkLayerTarget, ...]]:
    openings: list[VmtkOpeningTarget] = []
    layers: list[VmtkLayerTarget] = []
    for name in sorted({str(n).strip() for n in patch_names if str(n).strip()}):
        if _is_wall(name):
            layers.append(VmtkLayerTarget(
                target_id=f"layer_region:{name}", patch_name=name,
                label=f"lumen wall '{name}' (near-wall layer coverage)", renderable=True))
        else:
            openings.append(VmtkOpeningTarget(
                target_id=f"opening:{name}", patch_name=name,
                label=f"inlet/outlet cap '{name}'", renderable=True))
    return tuple(openings), tuple(layers)


def _branch_key(pl: Polyline) -> tuple:
    a = tuple(round(c, 6) for c in pl[0])
    b = tuple(round(c, 6) for c in pl[-1])
    lo, hi = sorted((a, b))
    return (lo, hi, len(pl))


def _midpoint(pl: Polyline) -> Point:
    mid = pl[len(pl) // 2]
    return (float(mid[0]), float(mid[1]), float(mid[2]))


def _span(pl: Polyline) -> float:
    xs = [p[0] for p in pl]
    ys = [p[1] for p in pl]
    zs = [p[2] for p in pl]
    diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2) ** 0.5
    # a fraction of the branch's own extent, floored so a degenerate branch still frames something
    return round(max(diag * 0.35, 1.0), 6)


def discover_branch_targets(polylines) -> tuple[VmtkBranchTarget, ...]:
    ordered = sorted((tuple(pl) for pl in polylines if pl), key=_branch_key)
    out: list[VmtkBranchTarget] = []
    for i, pl in enumerate(ordered):
        renderable = len(pl) >= 2
        out.append(VmtkBranchTarget(
            target_id=f"branch:{i}", point=_midpoint(pl), span_mm=_span(pl) if renderable else 1.0,
            n_points=len(pl), label=f"centerline branch {i} ({len(pl)} points)",
            renderable=renderable))
    return tuple(out)


@dataclass(frozen=True)
class VmtkSceneAudit:

    delivered_openings: int
    submitted_open_loops: int          # boundary loops of the submitted lumen; -1 = unknown
    branch_count: int
    mismatches: tuple[str, ...]


def audit_cross_artifacts(
    openings: tuple[VmtkOpeningTarget, ...],
    submitted_open_loops: int,
    branches: tuple[VmtkBranchTarget, ...],
) -> VmtkSceneAudit:
    mismatches: list[str] = []
    n_open = len(openings)
    if submitted_open_loops >= 0 and submitted_open_loops != n_open:
        mismatches.append(
            f"the submitted lumen has {submitted_open_loops} open profile(s) but the delivered "
            f"mesh exposes {n_open} inlet/outlet cap(s) - an opening was lost, merged or "
            f"spuriously added")
    return VmtkSceneAudit(
        delivered_openings=n_open, submitted_open_loops=submitted_open_loops,
        branch_count=len(branches), mismatches=tuple(mismatches))


def resolve_target(targets: tuple[VmtkTarget, ...], token: str):
    tok = (token or "").strip()
    by_id = {t.target_id: t for t in targets}
    if tok in by_id:
        return by_id[tok], None
    named = [t for t in targets if getattr(t, "patch_name", None) == tok]
    if len(named) == 1:
        return named[0], None
    if len(named) > 1:
        ids = ", ".join(t.target_id for t in named)
        return None, (f"'{tok}' names {len(named)} distinct targets ({ids}); "
                      f"call it by its stable id to disambiguate")
    return None, f"no opening, branch or layer region '{tok}' is declared for this mesh"
