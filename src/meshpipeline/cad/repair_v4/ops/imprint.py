# Responsibility: lane C23 - IMPRINT ops: add edges to faces so the meshers' discretisations of nearby faces stop
#                 crossing. `split_aligned` splits near-coaxial cylinders/cones at the SAME absolute angles.
# Owns: the `split_aligned` op and its kernel function `op_split_aligned`.
# Boundaries: the op class validates and calls the kernel; the kernel builds the half-planes, sections the target
#             faces with them and imprints the section edges with ONE BRepAlgoAPI_Splitter whose only argument is the
#             whole part (BOPAlgo computes no interference between sub-shapes of one argument, so touching solids
#             are not made conformal as a side effect). The geometry never moves: only edges are added.
"""Why `split_aligned` exists (C23, measured): 7 of the 32 overlapping_facets files are one part (ABC duplicates,
f9501251fb30 and six more) where a 6.096 mm shaft runs through a coaxial 6.35 mm bore of another body - a 0.254 mm
clearance, and the B-rep faces never touch. gmsh_default (no size setting) meshes the shaft's circles with 3 segments
and the bore's with 4: a triangle whose corners (6.096 mm from the axis) stick through a square whose sides are 4.49
mm from it, so the two surface meshes cross and 3D boundary recovery fails ("overlapping facets"). Splitting either
face with split_angle/split_closed only moves the error to the pieces (measured), because each face is split in its
OWN angular frame.

Splitting both faces at the SAME absolute angles puts their circles' mesh vertices on common rays, so the inner
polygon NESTS inside the outer one (aligned vertices; the inner chords lie inside the outer chords for any equal
segment count) whatever element size the mesher picks. It is a pure split: no point of the part moves, so layer 1,
volume and area are unchanged (the shared `guard` refuses anything else)."""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops import check_call, register, run_kernel_op
from meshpipeline.cad.repair_v4.ops.split import _finish, _indices
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

#: scope "all": two coaxial faces are a pair to align when they come within this fraction of the smaller radius of each
#: other - the chordal sag of a circle meshed with 3 segments (R(1 - cos 60 deg)), gmsh_default's measured worst case
NEAR_FRACTION_OF_RADIUS = 0.5
#: two axes are one when their directions differ by less than this (radians) and their lines pass within
#: COAXIAL_FRACTION of the part's diagonal of each other
COAXIAL_ANGLE_RAD = 1e-4
COAXIAL_FRACTION = 1e-6


def _axis_of(face: Any) -> Any | None:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder

    surface = BRepAdaptor_Surface(face)
    kind = surface.GetType()
    if kind == GeomAbs_Cylinder:
        return surface.Cylinder().Position()
    if kind == GeomAbs_Cone:
        return surface.Cone().Position()
    return None


def _radius_of(face: Any) -> float:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder

    surface = BRepAdaptor_Surface(face)
    if surface.GetType() == GeomAbs_Cylinder:
        return surface.Cylinder().Radius()
    return surface.Cone().RefRadius()


def _coaxial(a: Any, b: Any, diag: float) -> bool:
    parallel = (a.Direction().Angle(b.Direction()) <= COAXIAL_ANGLE_RAD
                or a.Direction().Angle(b.Direction().Reversed()) <= COAXIAL_ANGLE_RAD)
    return parallel and _line_distance(a, b) <= COAXIAL_FRACTION * diag


def _near_groups(faces_map: Any, diag: float) -> list[list[int]]:
    """Every group of coaxial cylinders/cones in which each face comes within NEAR_FRACTION_OF_RADIUS of the smaller
    radius of another face of the group (union over such pairs). 1-based face indices, each group sorted."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopoDS import TopoDS

    revolved = []
    for i in range(1, faces_map.Extent() + 1):
        face = TopoDS.Face_s(faces_map.FindKey(i))
        axis = _axis_of(face)
        if axis is not None:
            box = Bnd_Box()
            BRepBndLib.Add_s(face, box)
            revolved.append((i, face, axis, _radius_of(face), box))
    parent = {r[0]: r[0] for r in revolved}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for k, (i, fi, ai, ri, bi) in enumerate(revolved):
        for j, fj, aj, rj, bj in revolved[k + 1:]:
            near = NEAR_FRACTION_OF_RADIUS * min(ri, rj)
            grown = Bnd_Box()
            grown.Add(bi)
            grown.Enlarge(near)
            if grown.IsOut(bj) or not _coaxial(ai, aj, diag):
                continue
            dist = BRepExtrema_DistShapeShape(fi, fj)
            if dist.IsDone() and dist.Value() <= near:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in parent:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def _line_distance(a: Any, b: Any) -> float:
    """Distance between the line of axis `b` and the location of axis `a` (both gp_Ax3; directions already parallel)."""
    from OCP.gp import gp_Lin

    return gp_Lin(b.Axis()).Distance(a.Location())


@register_kernel_fn("op_split_aligned")
def _split_aligned(payload: dict) -> dict:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_ListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    segments = int(payload["segments"])
    old = ws_kernel.read_brep(payload["brep_path"])
    faces_map = ws_kernel._maps(old)["face"]
    whole = Bnd_Box()
    BRepBndLib.Add_s(old, whole)
    x0, y0, z0, x1, y1, z1 = whole.Get()
    diag = math.dist((x0, y0, z0), (x1, y1, z1))
    if payload.get("scope") == "all":
        groups = _near_groups(faces_map, diag)
        if not groups:
            return {"declined": "no_change: no coaxial cylinders or cones come near each other"}
    else:
        groups = [list(payload["faces"])]
    tools = TopTools_ListOfShape()
    n_edges = 0
    for group in groups:
        faces = [TopoDS.Face_s(faces_map.FindKey(i)) for i in group]
        axes = [_axis_of(f) for f in faces]
        if any(a is None for a in axes):
            return {"declined": "not_revolved: split_aligned takes cylinders and cones only"}
        ref = axes[0]
        if not all(_coaxial(a, ref, diag) for a in axes[1:]):
            return {"declined": "not_coaxial: the target faces do not share one axis"}
        n_edges += _section_edges(faces, ref, segments, 2.0 * diag, tools)
    if n_edges < 0:
        return {"declined": "split_failed: a section of a target face did not complete"}
    return _imprint(old, tools, n_edges, payload, segments, start, groups)


def _section_edges(faces: list, ref: Any, segments: int, reach: float, tools: Any) -> int:
    """Section every face with `segments` half-planes through `ref`'s axis, in ONE angular frame (`ref`'s own X
    direction), appending the edges to `tools`; the number appended, or a large negative on a failed section."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Vec
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    axis_dir, x_dir = ref.Direction(), ref.XDirection()
    y_dir = axis_dir.Crossed(x_dir)
    n_edges = 0
    for k in range(segments):
        angle = 2.0 * math.pi * k / segments
        ray = gp_Dir(gp_Vec(x_dir).Multiplied(math.cos(angle)).Added(gp_Vec(y_dir).Multiplied(math.sin(angle))))
        # the half-plane through the axis: its local X is the axis, its local Y the ray outwards (v >= 0 only)
        plane = gp_Pln(gp_Ax3(ref.Location(), ray.Crossed(axis_dir), axis_dir))
        half = BRepBuilderAPI_MakeFace(plane, -reach, reach, 0.0, reach).Face()
        for face in faces:
            section = BRepAlgoAPI_Section(face, half)
            if not section.IsDone():
                return -10**9
            edges = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(section.Shape(), TopAbs_EDGE, edges)
            for i in range(1, edges.Extent() + 1):
                tools.Append(edges.FindKey(i))
                n_edges += 1
    return n_edges


def _imprint(old: Any, tools: Any, n_edges: int, payload: dict, segments: int, start: float, groups: list) -> dict:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape

    if n_edges == 0:
        return {"declined": "no_change: no half-plane crossed the target faces"}
    arguments = TopTools_ListOfShape()
    arguments.Append(old)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        return {"declined": "split_failed: the splitter did not complete"}
    params = {"segments": segments, "groups": len(groups), "faces": sum(len(g) for g in groups)}
    return _finish(old, splitter.Shape(), splitter.History(), payload, op="split_aligned", params=params, start=start)


@register
class SplitAligned:
    """Split near-coaxial cylinders and cones at the SAME absolute angles (`segments` equal sectors around their common
    axis), so the meshers' polygons of the faces nest instead of crossing. Targets: 2+ faces, cylinders or cones."""

    name = "split_aligned"
    params_schema = {
        "type": "object",
        "properties": {"segments": {"type": "integer", "enum": [4, 6, 8, 12, 16]},
                       "scope": {"type": "string", "enum": ["targets", "all"]}},
        "required": ["segments"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 0},  # scope "targets" needs 1+ (one face: split in its own frame)
    }

    def applies_to(self, site: Site) -> bool:
        return sum(1 for e in site.entities if e[:1] == "F") >= 2

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        scope = params.get("scope", "targets")
        if scope == "targets" and not targets:
            raise ValueError("split_aligned with scope 'targets' needs at least one face")
        return run_kernel_op(pool, ws, kernel_fn="op_split_aligned", op=self.name,
                             payload={"faces": _indices(ws, targets), "segments": params["segments"],
                                      "scope": scope}, timeout_s=timeout_s)
