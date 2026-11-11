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
# over the same geometry. When in doubt, merge. A defect with no measurable extent (`location` is
# empty) cannot be placed, so it is treated as unbounded and joins every other defect.


@dataclass(frozen=True, slots=True)
class Region:
    index: int
    fingerprints: frozenset[str]
    bbox_min: Point
    bbox_max: Point
    #: Kernel face indices of the member defects that are faces. Edges and vertices contribute
    #: only to the bounding box. A scoped kernel face walk filters on this.
    face_indices: frozenset[int] = frozenset()


def _box(defect) -> tuple[Point, Point] | None:
    loc = getattr(defect, "location", None) or {}
    lo, hi = loc.get("bbox_min"), loc.get("bbox_max")
    if lo is None or hi is None:
        return None
    return (tuple(float(v) for v in lo), tuple(float(v) for v in hi))  # type: ignore[return-value]


def decompose(defects: Iterable, *, diagonal_mm: float,
              merge_fraction: float = 0.01) -> tuple[Region, ...]:
    found = list(defects)
    if not found:
        return ()
    grow = max(0.0, float(merge_fraction) * float(diagonal_mm))
    boxes = [_box(d) for d in found]
    parent = list(range(len(found)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            a, b = boxes[i], boxes[j]
            if a is None or b is None:
                union(i, j)
            else:
                (alo, ahi), (blo, bhi) = a, b
                if all(alo[k] - grow <= bhi[k] + grow and blo[k] - grow <= ahi[k] + grow
                       for k in range(3)):
                    union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(found)):
        groups.setdefault(find(i), []).append(i)

    built = []
    for members in groups.values():
        located = [b for b in (boxes[i] for i in members) if b is not None]
        if located:
            lo = (min(b[0][0] for b in located), min(b[0][1] for b in located),
                  min(b[0][2] for b in located))
            hi = (max(b[1][0] for b in located), max(b[1][1] for b in located),
                  max(b[1][2] for b in located))
        else:
            lo = hi = (0.0, 0.0, 0.0)
        faces = frozenset(int(found[i].entity_index) for i in members
                          if found[i].entity_type == "face")
        prints = frozenset(found[i].fingerprint for i in members)
        built.append((lo, hi, prints, faces))
    # Ascending bbox_min, then the fingerprints, so equal boxes still order the same way.
    built.sort(key=lambda r: (r[0], r[1], sorted(r[2])))
    return tuple(Region(index=n, fingerprints=p, bbox_min=lo, bbox_max=hi, face_indices=f)
                 for n, (lo, hi, p, f) in enumerate(built))
