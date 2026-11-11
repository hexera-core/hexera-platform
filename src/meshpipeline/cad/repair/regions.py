# Responsibility: Cluster located CAD defects into regions - the unit of scoped editing, scoped
#                 verification and reversal.
# Owns: the clustering rule (grown bounding boxes that intersect, joined transitively), the
#       deterministic region order, and the `Region` value.
# Boundaries: pure arithmetic over already-located defects. It imports no kernel, measures no
#             shape, and edits nothing. It over-merges rather than under-merges.
# Collaborates with: cad/repair/localize.py (whose defects it clusters).
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

Point = tuple[float, float, float]

# WHY OVER-MERGE. A self-intersection tangle is reported once per member face and every member
# proposes the identical repair. Split one tangle across two regions and two packages would fight
# over the same geometry. When in doubt, merge.
#
# UNLOCATED DEFECTS ARE THE EXCEPTION. One with no measurable extent (`location` is empty) cannot
# be shown to share geometry with anything, so merging it with everything would collapse the part
# into one region and forfeit all scoping for no safety evidence. They are excluded from
# clustering and returned as ONE trailing region with `located=False` and no bounding box: kept in
# the result so a caller cannot silently drop them, flagged so it can escalate them.


@dataclass(frozen=True, slots=True)
class Region:
    index: int
    fingerprints: frozenset[str]
    #: None only on the unlocated region, which has no position. Never a fabricated origin.
    bbox_min: Point | None
    bbox_max: Point | None
    #: Kernel face indices of the member defects that are faces. Edges and vertices contribute
    #: only to the bounding box. A scoped kernel face walk filters on this.
    face_indices: frozenset[int] = frozenset()
    located: bool = True


def _box(defect) -> tuple[Point, Point] | None:
    loc = getattr(defect, "location", None) or {}
    lo, hi = loc.get("bbox_min"), loc.get("bbox_max")
    if lo is None or hi is None:
        return None
    return (tuple(float(v) for v in lo), tuple(float(v) for v in hi))  # type: ignore[return-value]


def decompose(defects: Iterable, *, diagonal_mm: float,
              merge_fraction: float = 0.01) -> tuple[Region, ...]:
    found = list(defects)
    boxes = [_box(d) for d in found]
    placed = [i for i, b in enumerate(boxes) if b is not None]
    unplaced = [i for i, b in enumerate(boxes) if b is None]
    grow = max(0.0, float(merge_fraction) * float(diagonal_mm))
    parent = {i: i for i in placed}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for n, i in enumerate(placed):
        for j in placed[n + 1:]:
            a, b = boxes[i], boxes[j]
            assert a is not None and b is not None
            (alo, ahi), (blo, bhi) = a, b
            if all(alo[k] - grow <= bhi[k] + grow and blo[k] - grow <= ahi[k] + grow
                   for k in range(3)):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[int]] = {}
    for i in placed:
        groups.setdefault(find(i), []).append(i)

    def members_of(members: list[int]) -> tuple[frozenset[str], frozenset[int]]:
        return (frozenset(found[i].fingerprint for i in members),
                frozenset(int(found[i].entity_index) for i in members
                          if found[i].entity_type == "face"))

    built = []
    for members in groups.values():
        boxed = [b for b in (boxes[i] for i in members) if b is not None]
        lo = (min(b[0][0] for b in boxed), min(b[0][1] for b in boxed),
              min(b[0][2] for b in boxed))
        hi = (max(b[1][0] for b in boxed), max(b[1][1] for b in boxed),
              max(b[1][2] for b in boxed))
        prints, faces = members_of(members)
        built.append((lo, hi, prints, faces))
    # Ascending bbox_min, then the fingerprints, so equal boxes still order the same way.
    built.sort(key=lambda r: (r[0], r[1], sorted(r[2])))
    regions = [Region(index=n, fingerprints=p, bbox_min=lo, bbox_max=hi, face_indices=f)
               for n, (lo, hi, p, f) in enumerate(built)]
    if unplaced:
        prints, faces = members_of(unplaced)
        regions.append(Region(index=len(regions), fingerprints=prints, bbox_min=None,
                              bbox_max=None, face_indices=faces, located=False))
    return tuple(regions)
