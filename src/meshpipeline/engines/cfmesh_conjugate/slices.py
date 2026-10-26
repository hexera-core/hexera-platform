# Responsibility: Name the cuts a cooled solid is looked at through, and where they are.
# Boundaries: geometry and names only. It renders nothing and judges nothing.
# Collaborates with: cfmesh_conjugate/finalize.py (which publishes them as inspection_regions).
from __future__ import annotations

#: Off the exact middle by this fraction of the span. A block meshed with an even number of cells
#: has a grid plane down the middle, and a cut along a face of cells shows no cells at all.
_OFF_CENTRE = 1e-3

_AXIS_OF = {"x": 0, "y": 1, "z": 2}


def flow_axis(inlet: str, outlet: str) -> int | None:
    """Which axis the coolant runs along, from the block faces it enters and leaves by.

    The block faces are named xmin/xmax/ymin/ymax/zmin/zmax, so the port names carry the axis.
    None when the part is sealed, which is a legal plan: the branching test part has no ports at
    all and is still worth meshing.
    """
    for name in (inlet or "", outlet or ""):
        axis = _AXIS_OF.get(name[:1])
        if axis is not None and name[1:] in ("min", "max"):
            return axis
    return None


def review_slices(lo, hi, *, inlet: str = "", outlet: str = "") -> list[dict]:
    """The three cuts, as engines/manifest publishes an inspection region.

    WHAT EACH ONE IS FOR, because a slice nobody can say the purpose of is a slice a reviewer
    skips. A cold plate is a flat block with coolant running through it, and the three questions
    about such a mesh are: how many cells are across each channel (cut square across the flow),
    where the coolant reaches and where the metal is (cut through the plate at mid-thickness), and
    how many cells are through the channel's height with metal above and below it (cut along the
    flow). One cut answers each.

    `lo`/`hi` are the block in metres. Returns [] for a degenerate block rather than publishing a
    promise that renders to nothing.
    """
    lo = [float(v) for v in lo]
    hi = [float(v) for v in hi]
    span = [hi[k] - lo[k] for k in range(3)]
    if min(span) <= 0.0:
        return []
    mid = [(lo[k] + hi[k]) / 2.0 for k in range(3)]
    frame = [lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]

    flow = flow_axis(inlet, outlet)
    thin = min(range(3), key=lambda k: span[k])
    if flow is None:
        # SEALED, so there is no flow direction to cut across. The long axis stands in: it is the
        # one a passage of any shape most likely runs along.
        flow = max(range(3), key=lambda k: span[k])
    along = next((k for k in range(3) if k not in (flow, thin)), thin)

    def _cut(name: str, axis: int, label: str, why: str) -> dict:
        origin = list(mid)
        origin[axis] += _OFF_CENTRE * span[axis]
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        return {"name": name, "kind": "slice", "normal": normal, "origin": origin,
                "clip_box": list(frame), "label": label, "why": why}

    cuts = [
        _cut("across_the_flow", flow, "Square across the coolant's path",
             "the cells across each passage, and the metal between them"),
        _cut("through_the_plate", thin, "Through the part at mid-thickness",
             "where the coolant reaches and where the metal is"),
    ]
    if along not in (flow, thin):
        cuts.append(_cut("along_the_flow", along, "Along the coolant's path",
                         "the cells through the passage's height, with metal above and below"))
    return cuts


__all__ = ["flow_axis", "review_slices"]
