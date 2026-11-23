# Responsibility: The OCCT half of layer 3 - how far did the repaired shape move from the original?
# Owns: the `faithful_check` kernel call: two-sided sampled surface distance, area and volume
#       ratios, body counts and the fill-span rule.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. Shapes arrive as
#             BREP paths (always millimetres, see xcaf_io) so every number here is in mm whatever
#             unit the STEP file was written in. Verdict assembly is `faithful.py`.
"""Fidelity of a repair, measured on the surface.

Distances are taken to the FACES of the other shape, never to its solid: a point on a translated
box that falls inside the original solid would otherwise read as 0 mm away. Sampling is ported from
`cad/repair/deviation.py` (UV grid over the trimmed face, area-weighted) but made total: a face the
grid misses is still measured at one of its vertices, so no face goes unmeasured.
"""

from __future__ import annotations

import math
import re
import time

from meshpipeline.cad.repair_v4.kernel_calls import register

BOUND_ABS_MM = 0.1
BOUND_DIAG_FRACTION = 0.001
FILL_SPAN_FRACTION = 0.05
RATIO_TOLERANCE = 0.02
SAMPLES = 600
BUDGET_S = 240.0
_FACE_ID = re.compile(r"^F(\d+)$")


def _read(path: str):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, path, BRep_Builder()):
        raise ValueError(f"cannot read BREP {path!r}")
    return shape


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, found)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _compound(faces: list):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for face in faces:
        builder.Add(comp, face)
    return comp


def _box(shape) -> tuple[float, float, float, float, float, float] | None:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None
    return tuple(float(v) for v in box.Get())  # type: ignore[return-value]


def _diag(box) -> float:
    if box is None:
        return 0.0
    return math.dist(box[:3], box[3:])


def _area(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return float(props.Mass())


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, found)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _free_shell_count(shape) -> int:
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(shape, TopAbs_SHELL, TopAbs_SOLID)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    total = 0.0
    for solid in _solids(shape):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        total += abs(float(props.Mass()))
    return total


def _ratio(after: float, before: float) -> float:
    if before <= 0.0:
        return 1.0 if after <= 0.0 else math.inf
    return after / before


def _face_points(face, count: int) -> list[tuple[float, float, float]]:
    """About `count` points on the trimmed face; never empty (falls back to a vertex)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_OUT, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    face = TopoDS.Face_s(face)
    umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
    surface = BRepAdaptor_Surface(face)
    tol = BRep_Tool.Tolerance_s(face)
    side = max(1, math.ceil(math.sqrt(count)))
    points: list[tuple[float, float, float]] = []
    for i in range(side):
        for j in range(side):
            u = umin + (umax - umin) * (i + 0.5) / side
            v = vmin + (vmax - vmin) * (j + 0.5) / side
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), tol).State() == TopAbs_OUT:
                continue
            p = surface.Value(u, v)
            points.append((p.X(), p.Y(), p.Z()))
    if not points:
        explorer = TopExp_Explorer(face, TopAbs_VERTEX)
        if explorer.More():
            p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(explorer.Current()))
            points.append((p.X(), p.Y(), p.Z()))
    return points


def _share_out(faces: list) -> list[int]:
    """`SAMPLES` shared out by area, at least one point per face."""
    areas = [max(_area(face), 0.0) for face in faces]
    total = sum(areas)
    return [max(1, round(SAMPLES * area / total) if total > 0 else 1) for area in areas]


def _vertex_dist(point, target) -> float:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    dist = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex(), target)
    if not dist.IsDone():
        raise RuntimeError("BRepExtrema_DistShapeShape did not complete")
    return float(dist.Value())


def _worst(kept: list, others: list, other_keys: list, reach: float, deadline: float) -> float:
    """Furthest sampled point of `kept` from the faces `others`. A spent budget raises: a maximum
    over part of the points is a lower bound, and a lower bound would read as faithful.

    Each sampled face is measured against only the other faces whose box is within `reach` of its
    own: a point whose nearest face lies within `reach` has that face's box within `reach` of the
    face's box, so the local answer is exact whenever it is <= reach. Beyond `reach` (or with no
    candidate) the point is measured against everything, so no distance is ever under-reported."""
    everything = _compound(others)
    worst = 0.0
    for face, count in zip(kept, _share_out(kept), strict=True):
        box = _box(face)
        if box is None:
            continue
        near = [
            o
            for o, (_, ob) in zip(others, other_keys, strict=True)
            if all(ob[k] <= box[k + 3] + reach and ob[k + 3] >= box[k] - reach for k in range(3))
        ]
        local = _compound(near) if near else None
        for point in _face_points(face, count):
            if time.monotonic() > deadline:
                raise RuntimeError("faithful_check sampling budget exceeded")
            d = _vertex_dist(point, local) if local is not None else math.inf
            if d > reach:
                d = _vertex_dist(point, everything)
            worst = max(worst, d)
    return worst


class DeclaredIdMismatch(ValueError):
    """A declared id does not name what the caller claims. A caller bug, never infrastructure."""


def _declared(ids: list[str], faces: list, side: str) -> list:
    picked = []
    for raw in ids:
        match = _FACE_ID.match(raw)
        if match is None or not 1 <= int(match.group(1)) <= len(faces):
            raise DeclaredIdMismatch(
                f"declared {side} face {raw!r} is not an index 'F<k>' into the {side} shape's faces"
            )
        picked.append(faces[int(match.group(1)) - 1])
    return picked


def _keys(faces: list) -> list[tuple[float, tuple[float, ...]]]:
    """(area, bounding box) per face: the cheap fingerprint two copies of one face share."""
    return [(_area(face), _box(face) or (0.0,) * 6) for face in faces]


def _twinned(keys: list, other_keys: list, tol: float) -> set[int]:
    """Indices of `keys` that have a twin in `other_keys`: same area (1e-6 relative) and a bounding
    box within `tol` on every side. Candidates come from a grid on the box's min corner, so a part
    made of thousands of identical faces is not quadratic."""
    cell = 4.0 * tol
    grid: dict[tuple[int, int, int], list[int]] = {}
    for i, (_, box) in enumerate(other_keys):
        grid.setdefault((int(box[0] // cell), int(box[1] // cell), int(box[2] // cell)), []).append(i)
    found: set[int] = set()
    for index, (area, box) in enumerate(keys):
        slack = 1e-6 * max(area, 1.0) + tol * tol
        cx, cy, cz = int(box[0] // cell), int(box[1] // cell), int(box[2] // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for k in grid.get((cx + dx, cy + dy, cz + dz), ()):
                        o_area, o_box = other_keys[k]
                        if abs(o_area - area) <= slack and all(
                            abs(x - y) <= tol for x, y in zip(box, o_box, strict=True)
                        ):
                            found.add(index)
                            break
                    if index in found:
                        break
                if index in found:
                    break
            if index in found:
                break
    return found


def _require_genuinely_new(declared: list, ids: list[str], others: list, tol: float, claim: str) -> None:
    """Ids are indices into a re-read BREP; an in-range index can name the wrong face. A face that
    has an identical twin on the other side was not added (or removed) at all, so the claim and
    the id disagree: refuse, because honouring it would exempt the wrong face from measurement."""
    twinned = _twinned(_keys(declared), _keys(others), tol)
    for index, raw in enumerate(ids):
        if index in twinned:
            raise DeclaredIdMismatch(f"declared {claim} face {raw!r} has an identical face on the other side")


def _groups(faces: list) -> list[list]:
    """Declared-added faces joined by a shared edge: one group is one fill. The spec's rule is the
    span of a fill's loop, so it is measured per fill, never over the union of unrelated fills."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    edges = TopTools_IndexedMapOfShape()
    parent = list(range(len(faces)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[int, int] = {}
    for index, face in enumerate(faces):
        local = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(face, TopAbs_EDGE, local)
        for k in range(1, local.Extent() + 1):
            key = edges.Add(local.FindKey(k))
            if key in owner:
                parent[find(index)] = find(owner[key])
            else:
                owner[key] = index
    groups: dict[int, list] = {}
    for index, face in enumerate(faces):
        groups.setdefault(find(index), []).append(face)
    return list(groups.values())


def _is_in(face, group: list) -> bool:
    return any(face.IsSame(other) for other in group)


@register("faithful_check")
def _faithful_check(payload: dict) -> dict:
    original = _read(payload["original_brep"])
    repaired = _read(payload["repaired_brep"])
    added_ids = list(payload.get("declared_added", []))
    removed_ids = list(payload.get("declared_removed", []))
    o_faces, r_faces = _faces(original), _faces(repaired)
    added = _declared(added_ids, r_faces, "repaired")
    removed = _declared(removed_ids, o_faces, "original")

    o_box, r_box = _box(original), _box(repaired)
    diag = _diag(o_box)
    bound = min(BOUND_ABS_MM, BOUND_DIAG_FRACTION * diag)
    reasons: list[str] = []
    if o_box is None or diag <= 0.0:
        raise ValueError("original shape has no extent")
    if r_box is None:
        reasons.append("repaired_empty")

    if r_box is not None:
        tol = max(bound, 1e-9)
        _require_genuinely_new(added, added_ids, o_faces, tol, "added")
        _require_genuinely_new(removed, removed_ids, r_faces, tol, "removed")

    deadline = time.monotonic() + BUDGET_S
    max_dev = math.inf
    if r_faces and o_faces:
        # repaired -> original: declared added faces are new surface, so they are exempt.
        # A face with an identical twin on the other side is unchanged (0 mm by construction), so it
        # is not sampled; the search's states differ from the original in a few faces only, and this
        # is what keeps layer 3 affordable. The twin test is far tighter than the bound.
        twin_tol = max(0.01 * bound, 1e-9)
        o_keys, r_keys = _keys(o_faces), _keys(r_faces)
        r_twin = _twinned(r_keys, o_keys, twin_tol)
        o_twin = _twinned(o_keys, r_keys, twin_tol)
        r_kept = [f for i, f in enumerate(r_faces) if i not in r_twin and not _is_in(f, added)]
        o_kept = [f for i, f in enumerate(o_faces) if i not in o_twin and not _is_in(f, removed)]
        reach = 2.0 * bound
        dev_r = _worst(r_kept, o_faces, o_keys, reach, deadline) if r_kept else 0.0
        # original -> repaired: declared removed faces are gone on purpose, so they are exempt.
        dev_o = _worst(o_kept, r_faces, r_keys, reach, deadline) if o_kept else 0.0
        max_dev = max(dev_r, dev_o)
    if max_dev > bound:
        reasons.append("deviation_exceeds_bound")

    fill_span = max((_diag(_box(_compound(group))) for group in _groups(added)), default=0.0)
    if fill_span > FILL_SPAN_FRACTION * diag:
        reasons.append("fill_too_large")

    area_ratio = _ratio(_area(repaired), _area(original))
    volume_ratio = _ratio(_volume(repaired), _volume(original))
    if abs(area_ratio - 1.0) > RATIO_TOLERANCE:
        reasons.append("area_changed")
    if abs(volume_ratio - 1.0) > RATIO_TOLERANCE:
        reasons.append("volume_changed")
    before = len(_solids(original)) + _free_shell_count(original)
    after = len(_solids(repaired)) + _free_shell_count(repaired)
    if before != after:
        reasons.append("body_count_changed")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "max_dev_mm": max_dev,
        "bound_mm": bound,
        "diag_mm": diag,
        "fill_span_mm": fill_span,
        "area_ratio": area_ratio,
        "volume_ratio": volume_ratio,
        "bodies_before": before,
        "bodies_after": after,
    }
