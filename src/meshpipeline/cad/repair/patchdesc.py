# Responsibility: Isolate the local B-rep patch around one located defect and describe it as a
# fixed-length pose-invariant vector - the key the fix memory is queried by.
# Boundaries: pure read of a shape. Never mutates geometry, never decides a repair; what a
# prediction MEANS is fixmemory.py's business, and whether anything acts on it is the loop's.
"""Patch isolation and the hand-v2 descriptor.

Promoted from labs/defect_cad_rag (experiments 1-2,
docs/research/defect-cad-rag-experiment-1.md): the descriptor is exactly invariant under rigid
motion and stable through a STEP round trip, which is the property the fix memory keys on.

THREE NESTED LEVELS (design §Literature-backed direction, 1):
  L0  the defect's own faces: the face itself, an edge's owner faces, an opening's boundary
      owners;
  L1  L0 plus every face sharing an edge with it;
  L2  one more ring, clipped to a radius set by the defect's own scale.

INVARIANT BY CONSTRUCTION. Every number is intrinsic (a length, an area, a surface type, a count)
or a ratio of intrinsic numbers. Nothing reads an axis-aligned box: the localiser's boxes rotate
with the part, so a descriptor built on them would call one defect two names.

The descriptor is a baseline, not an encoder. Its job is to say whether local geometry carries any
signal beyond the defect code before anyone trains a network on 21 labels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Bump on any change to what `describe` computes. Records with another version are not
#: comparable and are never retrieved together.
DESCRIPTOR_VERSION = "hand-v2"

#: Lengths below this fraction of the part's scale are one number. A STEP round trip turns a
#: zero-length edge into a 1e-9 mm one; without a floor those two land 12 decades apart and the
#: same defect stops retrieving itself (measured on bm2_ec_t4-A, hand-v1).
LENGTH_FLOOR_RATIO = 1e-6

#: How far the L2 ring reaches, in multiples of the local scale.
L2_RADIUS_SCALES = 4.0

_SURFACES = ("plane", "cylinder", "cone", "sphere", "torus", "spline", "other")
_CURVES = ("line", "circle", "conic", "spline", "other")
_ENTITY_TYPES = ("edge", "face", "shell", "other")
_PER_LEVEL = 3 + len(_SURFACES) + len(_CURVES) + 4
#: Exact vector length, asserted by the tests so a silent change cannot reach the store.
DIMENSIONS = 3 * _PER_LEVEL + len(_ENTITY_TYPES) + 4


@dataclass(frozen=True, slots=True)
class Patch:
    """Face indices per level plus the intrinsic numbers the descriptor needs."""

    levels: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]
    #: The defect's own size in mm: an edge's length, a face's sqrt(area), an opening's boundary
    #: length. Intrinsic, so rotation cannot change it.
    scale_mm: float
    #: sqrt(total face area) of the whole part, the rotation-invariant stand-in for its diagonal.
    part_scale_mm: float
    entity_type: str
    facts: dict = field(default_factory=dict)


class _Topology:
    """Indexed faces and edges of one shape, and the face<->edge incidence between them."""

    def __init__(self, shape) -> None:
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer

        from meshpipeline.cad.repair.localize import _edge_length, _face_area, _indexed

        self.faces = _indexed(shape, TopAbs_FACE)
        self.edges = _indexed(shape, TopAbs_EDGE)
        self.face_edges: dict[int, set[int]] = {}
        self.edge_faces: dict[int, set[int]] = {}
        for fi in range(1, self.faces.Extent() + 1):
            explorer = TopExp_Explorer(self.faces.FindKey(fi), TopAbs_EDGE)
            owned: set[int] = set()
            while explorer.More():
                ei = self.edges.FindIndex(explorer.Current())
                if ei > 0:
                    owned.add(ei)
                    self.edge_faces.setdefault(ei, set()).add(fi)
                explorer.Next()
            self.face_edges[fi] = owned
        self.face_area = {fi: _face_area(self.faces.FindKey(fi))
                          for fi in range(1, self.faces.Extent() + 1)}
        self.edge_length = {ei: _edge_length(self.edges.FindKey(ei))
                            for ei in range(1, self.edges.Extent() + 1)}
        self._centroids: dict[int, tuple[float, float, float] | None] = {}

    def neighbours(self, faces: set[int]) -> set[int]:
        out: set[int] = set()
        for fi in faces:
            for ei in self.face_edges.get(fi, ()):
                out |= self.edge_faces.get(ei, set())
        return out - faces

    def centroid(self, fi: int) -> tuple[float, float, float] | None:
        """The face's area centroid: a point on the part, unlike an axis-aligned box centre."""
        if fi not in self._centroids:
            from OCP.BRepGProp import BRepGProp
            from OCP.GProp import GProp_GProps

            props = GProp_GProps()
            try:
                BRepGProp.SurfaceProperties_s(self.faces.FindKey(fi), props)
                p = props.CentreOfMass()
                self._centroids[fi] = (p.X(), p.Y(), p.Z())
            except Exception:  # noqa: BLE001 - a degenerate face has no centroid
                self._centroids[fi] = None
        return self._centroids[fi]


def _seed(topo: _Topology, shape, defect) -> tuple[set[int], float, str]:
    """L0 faces, the defect's intrinsic scale and a normalised entity type."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.repair.operations import sub_shape

    kind = defect.entity_type
    boundary = [int(e) for e in (defect.measurements.get("boundary_edges") or ())]
    if boundary:
        faces = set().union(*(topo.edge_faces.get(e, set()) for e in boundary))
        length = float(defect.measurements.get("boundary_length")
                       or sum(topo.edge_length.get(e, 0.0) for e in boundary))
        return faces, length, kind if kind in _ENTITY_TYPES else "other"
    if kind == "edge":
        return (set(topo.edge_faces.get(defect.entity_index, set())),
                topo.edge_length.get(defect.entity_index, 0.0), "edge")
    if kind == "face":
        return ({defect.entity_index},
                math.sqrt(max(topo.face_area.get(defect.entity_index, 0.0), 0.0)), "face")
    sub = sub_shape(shape, kind, defect.entity_index)
    faces = set()
    if sub is not None:
        explorer = TopExp_Explorer(sub, TopAbs_FACE)
        while explorer.More():
            fi = topo.faces.FindIndex(explorer.Current())
            if fi > 0:
                faces.add(fi)
            explorer.Next()
    area = sum(topo.face_area.get(fi, 0.0) for fi in faces)
    return faces, math.sqrt(max(area, 0.0)), kind if kind in _ENTITY_TYPES else "other"


def isolate_patch(shape, defect, *, topology: _Topology | None = None) -> Patch:
    """The three nested face sets around `defect`. Pure: reads the shape, changes nothing."""
    topo = topology if topology is not None else _Topology(shape)
    l0, scale, kind = _seed(topo, shape, defect)
    l1 = l0 | topo.neighbours(l0)
    ring = topo.neighbours(l1)
    l1_edges = sorted(topo.edge_length[e] for f in l1 for e in topo.face_edges.get(f, ())
                      if topo.edge_length.get(e, 0.0) > 0)
    local = max(scale, l1_edges[len(l1_edges) // 2] if l1_edges else 0.0)
    radius = L2_RADIUS_SCALES * local
    anchors = [c for c in (topo.centroid(f) for f in l0) if c is not None]
    kept: set[int] = set()
    for fi in ring:
        c = topo.centroid(fi)
        if c is None or not anchors or radius <= 0:
            continue
        if min(math.dist(c, a) for a in anchors) <= radius:
            kept.add(fi)
    part = math.sqrt(max(sum(topo.face_area.values()), 0.0))
    facts = {"closed_loop": bool(defect.measurements.get("closed_loop", False)),
             "boundary_edges": len(defect.measurements.get("boundary_edges") or ()),
             "ring_clipped": len(ring) - len(kept)}
    return Patch(levels=(tuple(sorted(l0)), tuple(sorted(l1)), tuple(sorted(l1 | kept))),
                 scale_mm=float(scale), part_scale_mm=float(part), entity_type=kind, facts=facts)


def _surface_kind(face) -> str:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import (
        GeomAbs_BezierSurface,
        GeomAbs_BSplineSurface,
        GeomAbs_Cone,
        GeomAbs_Cylinder,
        GeomAbs_Plane,
        GeomAbs_Sphere,
        GeomAbs_Torus,
    )

    try:
        t = BRepAdaptor_Surface(face).GetType()
    except Exception:  # noqa: BLE001
        return "other"
    return {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cylinder", GeomAbs_Cone: "cone",
            GeomAbs_Sphere: "sphere", GeomAbs_Torus: "torus", GeomAbs_BSplineSurface: "spline",
            GeomAbs_BezierSurface: "spline"}.get(t, "other")


def _curve_kind(edge) -> str:
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import (
        GeomAbs_BezierCurve,
        GeomAbs_BSplineCurve,
        GeomAbs_Circle,
        GeomAbs_Ellipse,
        GeomAbs_Hyperbola,
        GeomAbs_Line,
        GeomAbs_Parabola,
    )

    try:
        if BRep_Tool.Degenerated_s(edge):
            return "other"
        t = BRepAdaptor_Curve(edge).GetType()
    except Exception:  # noqa: BLE001
        return "other"
    return {GeomAbs_Line: "line", GeomAbs_Circle: "circle", GeomAbs_Ellipse: "conic",
            GeomAbs_Hyperbola: "conic", GeomAbs_Parabola: "conic",
            GeomAbs_BSplineCurve: "spline", GeomAbs_BezierCurve: "spline"}.get(t, "other")


def _log_ratio(value: float, scale: float) -> float:
    """log10(value/scale), clipped to [-6, 6] so a zero-length edge is a number, not -inf."""
    if value <= 0 or scale <= 0:
        return -6.0
    return max(-6.0, min(6.0, math.log10(value / scale)))


def describe(patch: Patch, *, topology: _Topology) -> list[float]:
    """A `DIMENSIONS`-long vector: per level counts, type mixes and scale-free sizes."""
    topo = topology
    floor = LENGTH_FLOOR_RATIO * patch.part_scale_mm
    s = max(patch.scale_mm, floor)

    def ratio(value: float) -> float:
        return _log_ratio(max(value, floor), s)

    out: list[float] = []
    for faces in patch.levels:
        edges = sorted({e for f in faces for e in topo.face_edges.get(f, ())})
        free = sum(1 for e in edges if len(topo.edge_faces.get(e, ())) < 2)
        out += [math.log1p(len(faces)), math.log1p(len(edges)), free / len(edges) if edges else 0.0]
        kinds = [_surface_kind(topo.faces.FindKey(f)) for f in faces]
        out += [kinds.count(k) / len(kinds) if kinds else 0.0 for k in _SURFACES]
        curves = [_curve_kind(topo.edges.FindKey(e)) for e in edges]
        out += [curves.count(k) / len(curves) if curves else 0.0 for k in _CURVES]
        lengths = sorted(topo.edge_length.get(e, 0.0) for e in edges)
        area = sum(topo.face_area.get(f, 0.0) for f in faces)
        # THE LOWER QUARTILE, NOT THE MINIMUM: a round trip can merge or split one tiny edge in
        # the ring, and the minimum then jumps by decades (bug25045_ick_0433, hand-v1).
        out += [ratio(math.sqrt(max(area, 0.0))),
                ratio(lengths[len(lengths) // 4]) if lengths else -6.0,
                ratio(lengths[len(lengths) // 2]) if lengths else -6.0,
                ratio(lengths[-1]) if lengths else -6.0]
    out += [1.0 if patch.entity_type == k else 0.0 for k in _ENTITY_TYPES]
    out += [_log_ratio(s, patch.part_scale_mm),
            1.0 if patch.facts.get("closed_loop") else 0.0,
            math.log1p(patch.facts.get("boundary_edges", 0)),
            math.log1p(patch.facts.get("ring_clipped", 0))]
    assert len(out) == DIMENSIONS, (len(out), DIMENSIONS)
    return out


def scale_band(patch: Patch) -> str:
    """A coarse facet: the defect's size relative to the part, in decades."""
    floor = LENGTH_FLOOR_RATIO * patch.part_scale_mm
    return f"1e{math.floor(_log_ratio(max(patch.scale_mm, floor), patch.part_scale_mm))}"
