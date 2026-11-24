# Responsibility: The OCCT half of failure sites - boxes, locating a point, close face pairs, exact
#                 distances, the k = 2 neighbourhood of a site, and the site picture.
# Owns: the `sites_*` and `render_view` kernel functions.
# Boundaries: runs INSIDE a kernel worker; OCP is imported lazily. Shapes are BREP paths; entities
#             are `{"id", "type", "index"}` refs (index = kernel index) and every answer is plain
#             data keyed by the caller's id or by kernel names ("F12" = face index 12). The
#             parent-side rules (clustering, kinds, observation) are sites.py.
"""Geometry for failure sites.

`sites_distance` is `occ_distance` ported into a kernel fn: that module bounded a parent-side call
with a child process, and here the worker pool's SIGKILL is the bound, so the call is plain
BRepExtrema_DistShapeShape. Every call is deadline-bounded on its own too, because a worker killed
mid-call loses the answers already computed.
"""

from __future__ import annotations

import base64
import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep

#: Wall budget inside one call, under the pool's timeout so a partial answer beats a kill.
CALL_BUDGET_S = 60.0
MAX_CANDIDATE_PAIRS = 3000
MAX_GRAPH_NODES = 40
MAX_GRAPH_EDGES = 80
CURVED_FOR_SPAN = ("cylinder", "cone", "sphere", "torus")


def _sub(maps: dict, ref: dict) -> Any:
    indexed = maps[ref["type"]]
    index = int(ref["index"])
    if not 1 <= index <= indexed.Extent():
        raise ValueError(f"{ref['type']} index {index} is outside 1..{indexed.Extent()}")
    return indexed.FindKey(index)


def _box(sub: Any) -> tuple[float, float, float, float, float, float] | None:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(sub, box, False)
    if box.IsVoid():
        return None
    x0, y0, z0, x1, y1, z1 = box.Get()
    return (x0, y0, z0, x1, y1, z1)


def _gap(a: tuple, b: tuple) -> float:
    return math.sqrt(sum(max(a[i] - b[i + 3], b[i] - a[i + 3], 0.0) ** 2 for i in range(3)))


def _enum_name(value: Any) -> str:
    return str(value).split(".")[-1].replace("GeomAbs_", "")


def _vertices(sub: Any) -> Any:
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(sub, TopAbs_VERTEX, found)
    return found


def _touch(a: Any, b: Any) -> bool:
    """True when two sub-shapes share a vertex: adjacent, so their distance is 0 for a boring reason."""
    va, vb = _vertices(a), _vertices(b)
    return any(vb.Contains(va.FindKey(i)) for i in range(1, va.Extent() + 1))


def _extrema(a: Any, b: Any) -> dict:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    measured = BRepExtrema_DistShapeShape(a, b)
    if not measured.IsDone() or measured.NbSolution() < 1:
        return {"dist": None, "point_a": None, "point_b": None}
    pa, pb = measured.PointOnShape1(1), measured.PointOnShape2(1)
    return {"dist": float(measured.Value()),
            "point_a": [round(pa.X(), 6), round(pa.Y(), 6), round(pa.Z(), 6)],
            "point_b": [round(pb.X(), 6), round(pb.Y(), 6), round(pb.Z(), 6)]}


@register("sites_geometry")
def sites_geometry(payload: dict) -> dict:
    """`{"geometry": {id: {box, centroid, area?}}, "diagonal"}` for each entity ref."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    out: dict[str, dict] = {}
    for ref in payload["entities"]:
        sub = _sub(maps, ref)
        box = _box(sub)
        if box is None:
            continue
        entry: dict = {"box": [round(v, 6) for v in box],
                       "centroid": [round((box[i] + box[i + 3]) / 2, 6) for i in range(3)]}
        if ref["type"] == "face":
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(sub, props)
            entry["area"] = round(float(props.Mass()), 6)
        out[ref["id"]] = entry
    whole = _box(shape)
    diagonal = 0.0 if whole is None else math.dist(whole[:3], whole[3:])
    return {"geometry": out, "diagonal": diagonal}


@register("sites_locate_points")
def sites_locate_points(payload: dict) -> dict:
    """Per point, the kernel indices of up to three faces within `radius` of it, nearest first."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt

    deadline = time.monotonic() + CALL_BUDGET_S
    shape = read_brep(payload["brep_path"])
    faces = _maps(shape)["face"]
    radius = float(payload["radius"])
    boxes = {i: _box(faces.FindKey(i)) for i in range(1, faces.Extent() + 1)}
    answers: list[list[int]] = []
    for x, y, z in payload["points"]:
        found: list[tuple[float, int]] = []
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(float(x), float(y), float(z))).Vertex()
        for index, box in boxes.items():
            if box is None or time.monotonic() > deadline:
                continue
            if _gap((x, y, z, x, y, z), box) > radius:
                continue
            d = _extrema(vertex, faces.FindKey(index))["dist"]
            if d is not None and d <= radius:
                found.append((d, index))
        found.sort()
        answers.append([index for _, index in found[:3]])
    return {"faces": answers}


#: A gap at or under this (mm) is a CONTACT, not a near miss: below it the two faces touch as far as any mesher
#: can tell, and the proximity split has no gap to size a plan from.
_CONTACT_MM = 1e-7


def _shares_edge(a: Any, b: Any) -> bool:
    """Do the two faces share an EDGE? (`_touch` asks about a VERTEX, which a contact at a corner also shares.)"""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    ma, mb = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(a, TopAbs_EDGE, ma)
    TopExp.MapShapes_s(b, TopAbs_EDGE, mb)
    return any(ma.Contains(mb.FindKey(k)) for k in range(1, mb.Extent() + 1))


def _face_area(face: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return float(props.Mass())


@register("sites_proximity")
def sites_proximity(payload: dict) -> dict:
    """Pairs of faces closer than `max_dist` that do not share an EDGE: the part is pinching, or already
    touching, itself there. Nearest first, at most `max_pairs`; each pair says whether it TOUCHES (gap 0).

    A CONTACT - two faces at gap 0 that share no edge, usually meeting at a vertex - used to be dropped here
    (the rule excluded any pair sharing a vertex). Lane A11 measured it as the commonest cause of a mesher
    failure on a VALID part: 18 of 35 located failures sit on such a face. They are kept now, and because they
    are at distance 0 they sort to the front of the capped list, ahead of the merely near pairs. Faces that
    share an EDGE are still excluded: they are legitimate neighbours, not a defect."""
    deadline = time.monotonic() + CALL_BUDGET_S
    shape = read_brep(payload["brep_path"])
    faces = _maps(shape)["face"]
    limit = float(payload["max_dist"])
    boxes = {i: _box(faces.FindKey(i)) for i in range(1, faces.Extent() + 1)}
    order = sorted((i for i, b in boxes.items() if b is not None), key=lambda i: boxes[i][0])  # type: ignore[index]
    candidates: list[tuple[int, int]] = []
    for pos, i in enumerate(order):
        bi = boxes[i]
        assert bi is not None
        for j in order[pos + 1:]:
            bj = boxes[j]
            assert bj is not None
            if bj[0] > bi[3] + limit:
                break
            if _gap(bi, bj) <= limit:
                candidates.append((i, j))
        if len(candidates) > MAX_CANDIDATE_PAIRS:
            break
    pairs: list[dict] = []
    for i, j in candidates[:MAX_CANDIDATE_PAIRS]:
        if time.monotonic() > deadline:
            break
        a, b = faces.FindKey(i), faces.FindKey(j)
        if _shares_edge(a, b):
            continue   # neighbours by construction: an op aimed here would split a sound join
        d = _extrema(a, b)
        if d["dist"] is not None and d["dist"] < limit:
            pairs.append({"a": i, "b": j, "dist": round(d["dist"], 6),
                          "point_a": d["point_a"], "point_b": d["point_b"],
                          # a contact needs different ops from a near miss: the proximity split sizes its plan
                          # from the gap, so a zero gap asks it for an infinitely fine split
                          "touching": bool(d["dist"] <= _CONTACT_MM),
                          "area_a_mm2": round(_face_area(a), 6), "area_b_mm2": round(_face_area(b), 6)})
    # Ties are common (coplanar sides); break them by WHERE the pair is, never by face order, which
    # changes whenever an op rebuilds the shape: the capped set must not depend on indices.
    pairs.sort(key=lambda p: (round(p["dist"], 4), [round(c, 3) for c in p["point_a"] or []],
                              [round(c, 3) for c in p["point_b"] or []], p["a"], p["b"]))
    return {"pairs": pairs[: int(payload["max_pairs"])]}


@register("sites_distance")
def sites_distance(payload: dict) -> dict:
    """Exact distance between each pair of entity refs, and whether they touch (share a vertex)."""
    deadline = time.monotonic() + CALL_BUDGET_S
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    out: list[dict] = []
    for pair in payload["pairs"]:
        a, b = _sub(maps, pair["a"]), _sub(maps, pair["b"])
        if time.monotonic() > deadline:
            out.append({"dist": None, "adjacent": False, "point_a": None, "point_b": None})
            continue
        out.append({**_extrema(a, b), "adjacent": _touch(a, b)})
    return {"distances": out}


def _surface_facts(face: Any) -> dict:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps

    surface = _enum_name(BRepAdaptor_Surface(face).GetType()).lower()
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    area = float(props.Mass())
    width = None
    if area > 0:
        # The short side of a rectangle (sqrt(3) x r of a disc): the feature size the mesher sees.
        width = (12.0 * max(min(props.PrincipalProperties().Moments()), 0.0) / area) ** 0.5
    span = None
    if surface in CURVED_FOR_SPAN:
        u0, u1, _v0, _v1 = BRepTools.UVBounds_s(face)
        span = (u1 - u0) * 180.0 / math.pi
    return {"surface": surface, "area": area, "min_width": width, "angular_span_deg": span}


def _edge_facts(edge: Any, f1: Any, f2: Any) -> dict:
    """Continuity, tolerance vs the measured gap between the two faces' images of the edge, and the
    interior dihedral angle (180 = flat continuation, 90 = a box edge, ~0 = a knife edge)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d, BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec

    out: dict = {"continuity": None, "tolerance": float(BRep_Tool.Tolerance_s(edge)),
                 "gap": None, "dihedral_deg": None}
    try:
        out["continuity"] = _enum_name(BRep_Tool.Continuity_s(edge, f1, f2))
    except Exception:  # noqa: BLE001 - one missing figure must not lose the edge
        pass
    try:
        c1, c2 = BRepAdaptor_Curve2d(edge, f1), BRepAdaptor_Curve2d(edge, f2)
        s1, s2 = BRepAdaptor_Surface(f1), BRepAdaptor_Surface(f2)
        t0, t1 = c1.FirstParameter(), c1.LastParameter()
        u0, u1 = c2.FirstParameter(), c2.LastParameter()
        gap = 0.0
        for k in range(5):
            f = k / 4.0
            p1 = c1.Value(t0 + f * (t1 - t0))
            p2 = c2.Value(u0 + f * (u1 - u0))
            gap = max(gap, s1.Value(p1.X(), p1.Y()).Distance(s2.Value(p2.X(), p2.Y())))
        out["gap"] = gap
        m1, m2 = c1.Value(0.5 * (t0 + t1)), c2.Value(0.5 * (u0 + u1))
        normals = []
        for face, uv in ((f1, m1), (f2, m2)):
            point, normal = gp_Pnt(), gp_Vec()
            BRepGProp_Face(face).Normal(uv.X(), uv.Y(), point, normal)
            normals.append(normal)
        if normals[0].Magnitude() > 0 and normals[1].Magnitude() > 0:
            cosine = max(-1.0, min(1.0, normals[0].Normalized().Dot(normals[1].Normalized())))
            out["dihedral_deg"] = 180.0 - math.degrees(math.acos(cosine))
    except Exception:  # noqa: BLE001
        pass
    return out


def _neighbourhood(shape: Any, seeds: list[Any], faces: Any, edges: Any, hops: int) -> dict:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
    )

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)

    def faces_of(edge: Any) -> list[Any]:
        if not ancestors.Contains(edge):
            return []
        return list(ancestors.FindFromKey(edge))

    def edges_of(face: Any) -> list[Any]:
        own = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(face, TopAbs_EDGE, own)
        return [own.FindKey(i) for i in range(1, own.Extent() + 1)]

    hop_of: dict[int, int] = {}
    frontier: list[int] = []
    for seed in seeds:
        found = [seed] if seed.ShapeType() == TopAbs_FACE else faces_of(seed)
        for face in found:
            index = faces.FindIndex(face)
            if index and index not in hop_of:
                hop_of[index] = 0
                frontier.append(index)
    truncated = False
    for hop in range(1, hops + 1):
        following: list[int] = []
        for index in frontier:
            for edge in edges_of(faces.FindKey(index)):
                for face in faces_of(edge):
                    other = faces.FindIndex(face)
                    if other and other not in hop_of:
                        if len(hop_of) >= MAX_GRAPH_NODES:
                            truncated = True
                            continue
                        hop_of[other] = hop
                        following.append(other)
        frontier = following

    nodes = []
    for index, hop in sorted(hop_of.items(), key=lambda kv: (kv[1], kv[0])):
        try:
            facts = _surface_facts(TopoDS.Face_s(faces.FindKey(index)))
        except Exception as exc:  # noqa: BLE001
            facts = {"surface": "unmeasured", "error": type(exc).__name__}
        nodes.append({"id": f"F{index}", "hop": hop, **facts})

    seen: set[int] = set()
    out_edges: list[dict] = []
    defects: list[dict] = []
    for index in hop_of:
        for edge in edges_of(faces.FindKey(index)):
            e = edges.FindIndex(edge)
            if e in seen:
                continue
            seen.add(e)
            pair = sorted({faces.FindIndex(f) for f in faces_of(edge)})
            inside = [p for p in pair if p in hop_of]
            if not inside or BRep_Tool.Degenerated_s(TopoDS.Edge_s(edge)):
                continue
            defect = None
            if len(pair) == 1 and not BRep_Tool.IsClosed_s(TopoDS.Edge_s(edge), TopoDS.Face_s(
                    faces.FindKey(pair[0]))):
                defect = "free"  # one face and no seam: the boundary of an open shell
            elif len(pair) > 2:
                defect = "non_manifold"
            if defect is not None:
                # For an open or invalid site these ARE the defect, so they come first and are kept.
                defects.append({"edge": f"E{e}", "faces": [f"F{p}" for p in inside], "kind": defect,
                                "tolerance": float(BRep_Tool.Tolerance_s(TopoDS.Edge_s(edge)))})
                continue
            if len(pair) != 2 or len(inside) != 2:
                continue  # a seam, or an edge leaving the neighbourhood
            if len(out_edges) >= MAX_GRAPH_EDGES:
                truncated = True  # regular edges are full, but keep scanning: free edges on LATER
                continue          # faces are the defect and must still be found
            facts = _edge_facts(TopoDS.Edge_s(edge), TopoDS.Face_s(faces.FindKey(pair[0])),
                                TopoDS.Face_s(faces.FindKey(pair[1])))
            out_edges.append({"edge": f"E{e}", "faces": [f"F{p}" for p in pair], **facts})
    return {"nodes": nodes, "edges": [*defects[:MAX_GRAPH_EDGES], *out_edges][:MAX_GRAPH_EDGES],
            "truncated": truncated or len(defects) + len(out_edges) > MAX_GRAPH_EDGES}


@register("sites_graph")
def sites_graph(payload: dict) -> dict:
    """The `k`-hop face neighbourhood of each requested site: per face its surface type, area, min
    width and angular span; per shared edge its continuity, tolerance, measured gap and dihedral."""
    deadline = time.monotonic() + CALL_BUDGET_S
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    out: dict[str, dict] = {}
    for site in payload["sites"]:
        if time.monotonic() > deadline:
            break
        seeds = [_sub(maps, ref) for ref in site["seeds"]]
        out[site["id"]] = _neighbourhood(shape, seeds, maps["face"], maps["edge"], int(payload["k"]))
    return {"graphs": out}


def volume_possibility(shape: Any) -> dict:
    """What can be said, from topology alone, about whether `make_volume` could ever find a closed volume in `shape`.

    A closed shell uses every non-degenerate edge at least twice (a seam counts twice in its own face). So take a
    face set and repeatedly drop any face with an edge used by fewer than two of the faces that remain: if nothing is
    left, NO subset of the set encloses a volume, and `make_volume` over it must decline. If something is left the
    answer is "possible", which is NOT a promise: it is only that this test cannot rule it out.

    `loose` is that test over the faces outside every solid (what `make_volume` builds from when given no targets);
    `any` is over every face (a superset of any explicit target list, since a subset of a set with no closed subset
    has none either). Also `free_edges`: edges bordering exactly one face (a seam is used twice by its own face,
    so it is not free); 0 here means `sew` has nothing to join."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    in_solid = TopTools_IndexedMapOfShape()
    solids = TopExp_Explorer(shape, TopAbs_SOLID)
    while solids.More():
        inner = TopExp_Explorer(solids.Current(), TopAbs_FACE)
        while inner.More():
            in_solid.Add(inner.Current())
            inner.Next()
        solids.Next()
    faces = _maps(shape)["face"]
    edges = TopTools_IndexedMapOfShape()
    uses: list[list[int]] = []  # per face: edge index per use, degenerate edges left out
    for i in range(1, faces.Extent() + 1):
        mine: list[int] = []
        explorer = TopExp_Explorer(faces.FindKey(i), TopAbs_EDGE)
        while explorer.More():
            edge = TopoDS.Edge_s(explorer.Current())
            if not BRep_Tool.Degenerated_s(edge):
                mine.append(edges.Add(edge))
            explorer.Next()
        uses.append(mine)
    loose = [i - 1 for i in range(1, faces.Extent() + 1) if not in_solid.Contains(faces.FindKey(i))]

    def closable(members: list[int]) -> bool:
        alive = set(members)
        while True:
            count: dict[int, int] = {}
            for f in alive:
                for e in uses[f]:
                    count[e] = count.get(e, 0) + 1
            drop = {f for f in alive if any(count[e] < 2 for e in uses[f])}
            if not drop:
                return bool(alive)
            alive -= drop

    every = list(range(faces.Extent()))
    per_edge: dict[int, int] = {}
    for mine in uses:
        for e in mine:
            per_edge[e] = per_edge.get(e, 0) + 1
    return {"loose": closable(loose), "any": closable(every),
            "free_edges": sum(1 for n in per_edge.values() if n == 1)}


@register("sites_free_loops")
def sites_free_loops(payload: dict) -> dict:
    """The closed loops of free edges (edges bordering exactly one face), each as its kernel edge indices and
    the diagonal of its bounding box: `{"loops": [{"edges": [i, ...], "span": mm}], "open_wires": n, "diagonal"}`.

    This is the edge list `fill_loop` needs and an open-shell finding does not carry (layer 1 names a shell, which has no
    stable id). The edges are the shape's own, so the indices are the workspace's. A free chain that does not close
    cannot be filled and is only counted."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_HSequenceOfShape, TopTools_IndexedDataMapOfShapeListOfShape

    shape = read_brep(payload["brep_path"])
    edge_index = _maps(shape)["edge"]
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    free = TopTools_HSequenceOfShape()
    for i in range(1, ancestors.Extent() + 1):
        edge = TopoDS.Edge_s(ancestors.FindKey(i))
        if not BRep_Tool.Degenerated_s(edge) and ancestors.FindFromIndex(i).Size() == 1:
            free.Append(edge)
    wires = TopTools_HSequenceOfShape()
    if free.Length():
        ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(free, 1e-7, True, wires)
    loops: list[dict] = []
    open_wires = 0
    for w in range(1, wires.Length() + 1):
        wire = wires.Value(w)
        if not wire.Closed():
            open_wires += 1
            continue
        indices: list[int] = []
        explorer = TopExp_Explorer(wire, TopAbs_EDGE)
        while explorer.More():
            index = edge_index.FindIndex(TopoDS.Edge_s(explorer.Current()))
            if index > 0 and index not in indices:
                indices.append(index)
            explorer.Next()
        box = Bnd_Box()
        BRepBndLib.Add_s(wire, box)
        if box.IsVoid() or not indices:
            continue
        x0, y0, z0, x1, y1, z1 = box.Get()
        loops.append({"edges": sorted(indices), "span": math.dist((x0, y0, z0), (x1, y1, z1))})
    whole = _box(shape)
    return {"loops": loops, "open_wires": open_wires,
            "diagonal": 0.0 if whole is None else math.dist(whole[:3], whole[3:]),
            "volume": volume_possibility(shape)}


@register("render_view")
def render_view(payload: dict) -> dict:
    """A PNG of the part framed on the given entities (and points), `size` px square.

    The tessellation and the off-screen draw run in `investigator_tools.render_region`'s bounded
    fresh interpreter, never in this worker: vtk leaks a GL context per call and BRepMesh holds the
    GIL. `{"png_b64": None}` when nothing was drawn.
    """
    from meshpipeline.cad.repair.investigator_tools import render_region

    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    boxes = [b for b in (_box(_sub(maps, ref)) for ref in payload["entities"]) if b is not None]
    for x, y, z in payload.get("xyz", []):
        boxes.append((x, y, z, x, y, z))
    whole = _box(shape)
    if whole is None:
        return {"png_b64": None}
    boxes = boxes or [whole]
    lo = [min(b[i] for b in boxes) for i in range(3)]
    hi = [max(b[i + 3] for b in boxes) for i in range(3)]
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    part = [(whole[i] + whole[i + 3]) / 2 for i in range(3)]
    away = [centre[i] - part[i] for i in range(3)]
    length = math.sqrt(sum(v * v for v in away))
    normal = [v / length for v in away] if length > 1e-9 else [0.577, 0.577, 0.577]
    camera = {"centroid_mm": centre, "normal": normal,
              "extent_mm": max(max(hi[i] - lo[i] for i in range(3)), 1e-6)}
    size = int(payload.get("size", 512))
    drawn = render_region(shape, [camera], size=(size, size), budget_s=float(payload.get("budget_s", 45)))
    if not drawn:
        return {"png_b64": None}
    return {"png_b64": base64.b64encode(drawn[0]).decode("ascii")}
