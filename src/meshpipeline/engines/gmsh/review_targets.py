# Responsibility: Enumerate the named things a Gmsh review must actually inspect.
# Boundaries: derived from the delivered mesh; it inspects nothing itself.
from __future__ import annotations

from dataclasses import dataclass

from meshpipeline.contracts.review_evidence import InspectionTarget, TargetKind


@dataclass(frozen=True)
class GmshGroupTarget:

    target_id: str                     # group:<dim>:<tag> - physical identity, order-independent
    dim: int
    tag: int
    name: str                          # "" for an unnamed group
    member_entities: tuple[int, ...]
    label: str
    renderable: bool                   # a group with no renderable members cannot be inspected

    def to_inspection_target(self, *, required: bool, purpose: str) -> InspectionTarget:
        return InspectionTarget(target_id=self.target_id, kind=TargetKind.GROUP,
                                label=self.label, purpose=purpose, required=required)


def _dim_word(dim: int) -> str:
    return {0: "point", 1: "edge", 2: "surface", 3: "volume"}.get(dim, f"dim{dim}")


def discover_group_targets(raw_groups) -> tuple[GmshGroupTarget, ...]:
    out: list[GmshGroupTarget] = []
    seen: set[str] = set()
    for dim, tag, name, members in sorted(raw_groups, key=lambda g: (g[0], g[1])):
        tid = f"group:{int(dim)}:{int(tag)}"
        if tid in seen:                # a malformed mesh cannot make one id mean two groups
            continue
        seen.add(tid)
        nm = (name or "").strip()
        label = f"{nm} ({_dim_word(dim)})" if nm else f"unnamed {_dim_word(dim)} group {tag}"
        out.append(GmshGroupTarget(
            target_id=tid, dim=int(dim), tag=int(tag), name=nm,
            member_entities=tuple(int(e) for e in members),
            label=label, renderable=bool(members)))
    return tuple(out)


def resolve_target(targets: tuple[GmshGroupTarget, ...], token: str):
    tok = (token or "").strip()
    by_id = {t.target_id: t for t in targets}
    if tok in by_id:
        return by_id[tok], None
    named = [t for t in targets if t.name and t.name == tok]
    if len(named) == 1:
        return named[0], None
    if len(named) > 1:
        ids = ", ".join(t.target_id for t in named)
        return None, (f"'{tok}' names {len(named)} distinct physical groups ({ids}); "
                      f"call it by its stable id to disambiguate")
    return None, f"no physical group '{tok}' is declared for this mesh"
