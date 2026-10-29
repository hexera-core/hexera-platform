# Responsibility: Mesh a part's sewn mid-surface (cad/midsurface: midsurface.brep + shell.json) with gmsh as a
# quad-dominant shell mesh - each face sized by the shell plan (engines/shell_plan.py), conformal along every
# junction because the faces share their edges - and hand back the engine-neutral ShellMesh: its elements'
# walls, mid-faces, parts and groups, the normals of each sheet turned one way, and its measured quality.
# Boundaries: runs gmsh in this process (the caller decides which process); reads the two files it is given
# and writes nothing. Deterministic: one thread, fixed options.
# Collaborates with: cad/midsurface.py, engines/shell_plan.py, engines/shell_mesh.py, engines/shell_quality.py,
# engines/shell_writers.py.
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np

from meshpipeline.engines import shell_mesh, shell_quality
from meshpipeline.engines import shell_plan as plan_rules
from meshpipeline.engines.shell_mesh import ShellMesh, edge_census, orient

logger = logging.getLogger(__name__)

#: gmsh: Frontal-Delaunay, then Blossom recombination into quads; quad-dominant (a few triangles where quads cannot
#: fit). Frontal-Delaunay-for-quads (8) left faces of B-spline mid-surfaces 7 times coarser than their size and
#: their quads past Nastran limits; QuadQS (11) made 3 to 5 times the elements asked for
ALGORITHM = 6
RECOMBINATION = 1
#: SHMESH-10: cut every quad past Nastran's own limits into two triangles
SPLIT_PAST = True
#: SHMESH-11: cut every closed sewn face at its seams before meshing; a face gmsh still cannot mesh as a periodic
#: surface is re-made as a B-spline (the same surface, exactly) and the mesh made again - at most RESPLINE_MAX faces
SPLIT_CLOSED = True
#: SHMESH-14: before any quad is cut, the inner nodes of flat faces round a quad past a limit are moved towards their
#: neighbours' middle when that brings fewer elements past a limit (at most RELAX_ROUNDS passes)
RELAX_ROUNDS = 6
#: SHMESH-13: swap the longest edge of a triangle past a limit with its neighbour's when both come out better
FLIP_PAST = True
#: SHMESH-12: merge the ends of sliver edges (under the floor and a fifth of their elements' longest edges)
COLLAPSE_SHORT = True
RESPLINE_MAX = 16
#: SHMESH-3/4 through gmsh's own curvature sizing: elements per full turn of every curve and curved face
PER_TURN = 4 * plan_rules.CURVE_PER_QUARTER


class _Periodic(Exception):
    """gmsh could not mesh sewn face `face` (0-based, in the surface's own order) as a periodic surface."""

    def __init__(self, face: int) -> None:
        super().__init__(f"face {face}")
        self.face = face


def _generate(gmsh, tag_of: dict[int, int]) -> None:
    """Mesh in 2D; a face gmsh cannot mesh as a periodic surface raises _Periodic for it (SHMESH-11)."""
    import re

    try:
        gmsh.model.mesh.generate(2)
    except Exception as exc:
        m = re.search(r"periodic surface (\d+)", str(exc))
        if m and int(m.group(1)) in tag_of:
            raise _Periodic(tag_of[int(m.group(1))]) from exc
        raise


def _arc_radius(gmsh, curve: int) -> float | None:
    """The radius (model units) of a gmsh curve that is a circle or a circular arc; None for any other curve."""
    if gmsh.model.getType(1, curve) != "Circle":
        return None
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    k = gmsh.model.getCurvature(1, curve, [0.5 * (lo[0] + hi[0])])
    return 1.0 / float(k[0]) if len(k) and float(k[0]) > 0 else None


def _rebuild(brep: Path, owners: list[str], out: Path, change) -> list[str]:
    """The sewn surface with some faces replaced: change(k, face) gives the faces that stand for face k (None:
    unchanged). The new faces and the rest are fused back into one conformal surface (a neighbour's edge is cut
    where a new corner lies on it), written to `out`; returns the owners, one per face of it."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    before = ms._faces(shape)
    if len(before) != len(owners):
        raise ValueError(f"the mid-surface has {len(before)} faces but the record names {len(owners)}")
    pieces: list[tuple[object, str]] = []
    changed = False
    for k, (f, own) in enumerate(zip(before, owners, strict=True)):
        got = change(k, f)
        changed = changed or got is not None
        pieces += [(g, own) for g in (got if got is not None else [f])]
    if not changed:
        BRepTools.Write_s(shape, str(out))
        return list(owners)
    if len(pieces) == 1:
        BRepTools.Write_s(pieces[0][0], str(out))
        return [pieces[0][1]]
    bop = BOPAlgo_Builder()
    for g, _own in pieces:
        bop.AddArgument(g)
    bop.SetRunParallel(False)
    bop.Perform()
    if bop.HasErrors():
        raise ValueError("the re-made faces of the mid-surface could not be fused back")
    fused = bop.Shape()
    after = ms._faces(fused)
    index = TopTools_IndexedMapOfShape()
    for f in after:
        index.Add(f)
    owner_of: dict[int, str] = {}
    for g, own in pieces:
        images = [TopoDS.Face_s(x) for x in bop.Modified(g)] or ([] if bop.IsDeleted(g) else [g])
        for x in images:
            k = index.FindIndex(x)
            if k > 0:
                owner_of.setdefault(k, own)
    if len(owner_of) != len(after):
        raise ValueError("a re-made face of the mid-surface could not be traced to its mid-face")
    BRepTools.Write_s(fused, str(out))
    return [owner_of[k] for k in range(1, len(after) + 1)]


def split_closed(brep: Path, owners: list[str], out: Path) -> list[str]:
    """SHMESH-11: every sewn face closed on itself (a full ring, a torus, a sphere: a seam in it) is cut at its seams
    into open pieces. gmsh cannot mesh some closed faces ("impossible to mesh periodic surface")."""
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideClosed

    from meshpipeline.cad import midsurface as ms

    def cut(_k, f):
        tool = ShapeUpgrade_ShapeDivideClosed(f)
        tool.SetNbSplitPoints(1)
        tool.Perform()
        got = ms._faces(tool.Result())
        return got if len(got) > 1 else None

    return _rebuild(brep, owners, out, cut)


def respline(brep: Path, owners: list[str], face: int, out: Path) -> list[str]:
    """SHMESH-11: sewn face `face` re-made as a B-spline surface (exactly the same surface, without its period)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert

    from meshpipeline.cad import midsurface as ms

    def convert(k, f):
        if k != face:
            return None
        nc = BRepBuilderAPI_NurbsConvert(f, True)
        return ms._faces(nc.Shape())

    return _rebuild(brep, owners, out, convert)


def _faces_and_facts(brep: Path, mm: float):
    """The sewn faces (OpenCASCADE MapShapes order), each with its centroid, width (mm) and curve radii (mm)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape

    from meshpipeline.cad import fea_measure as fm
    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    out = []
    for f in ms._faces(shape):
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        radii = []
        ex = TopExp_Explorer(f, TopAbs_EDGE)
        while ex.More():
            e = TopoDS.Edge_s(ex.Current())
            a = BRepAdaptor_Curve(e)
            if a.GetType() == GeomAbs_Circle:
                radii.append(a.Circle().Radius() * mm)
            ex.Next()
        out.append({"centroid": np.array([c.X(), c.Y(), c.Z()]), "area": g.Mass(), "width": ms.strip_width(f) * mm,
                    "box_width": ms.width_of(f) * mm, "radii": radii, "bend": fm.face_radius(f, mm),
                    "normal_face": f})
    return out


def _reference_normal(face) -> np.ndarray | None:
    """The face's oriented normal at its middle (the side its sheet's normals are turned to)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    from meshpipeline.cad import midsurface as ms

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    c = g.CentreOfMass()
    n = ms._face_normal_at(face, np.array([c.X(), c.Y(), c.Z()]))
    if n is None:
        return None
    from OCP.TopAbs import TopAbs_REVERSED

    return -n if face.Orientation() == TopAbs_REVERSED else n


def mesh_shell(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None) -> tuple[ShellMesh, dict]:
    """Mesh the sewn mid-surface: (the shell mesh in millimetres, the plan as applied: one row per face)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="shell_mesh_") as tmp:
        owners = list(record["surface_faces"])
        if SPLIT_CLOSED:
            cut = Path(tmp) / "cut.brep"
            owners = split_closed(Path(brep), owners, cut)
            brep = cut
        resplined = 0
        while True:
            try:
                mesh, applied = _mesh_once(Path(brep), record, owners, float(mm_per_unit), log)
            except _Periodic as p:
                if resplined >= RESPLINE_MAX:
                    raise ValueError(f"gmsh could not mesh {resplined + 1} periodic faces") from p
                nxt = Path(tmp) / f"respline_{resplined}.brep"
                owners = respline(Path(brep), owners, p.face, nxt)
                brep = nxt
                resplined += 1
                continue
            applied["quality"]["resplined_faces"] = resplined
            if resplined:
                mesh.notes.append(f"{resplined} face(s) gmsh could not mesh as periodic surfaces were re-made as "
                                  "B-splines (the same surface) and meshed (SHMESH-11).")
            return mesh, applied


def _mesh_once(brep: Path, record: dict, owners: list[str], mm: float, log: list | None) -> tuple[ShellMesh, dict]:
    import gmsh

    facts = _faces_and_facts(Path(brep), mm)
    if len(owners) != len(facts):
        raise ValueError(f"the mid-surface has {len(facts)} faces but the record names {len(owners)}")
    rows = []
    for k, f in enumerate(facts):
        width = min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"]
        rows.append({"face": k + 1, **plan_rules.face_plan(record, owners[k], width=width, radii=f["radii"],
                                                         bend=f["bend"])})
    walls = [r["wall_mm"] for r in rows]
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Geometry.OCCParallel", 0)
        gmsh.option.setNumber("Mesh.Algorithm", ALGORITHM)
        gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.RecombinationAlgorithm", RECOMBINATION)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", PER_TURN)
        # SHMESH-6 is held by the grading fields below; gmsh's own spread of edge sizes into a face carried a
        # narrow neighbour's size over the whole of a wide face (one 2.5 mm strip: 800,000 elements)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeMin", plan_rules.FLOOR_WALLS * min(walls) / mm)
        gmsh.option.setNumber("Mesh.MeshSizeMax", max(r["size_mm"] for r in rows) / mm)
        gmsh.option.setNumber("Mesh.Smoothing", 5)
        gmsh.model.add("shell")
        gmsh.model.occ.importShapes(str(brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        surfaces = [t for _d, t in gmsh.model.getEntities(2)]
        if len(surfaces) != len(facts):
            raise ValueError(f"gmsh read {len(surfaces)} faces of the mid-surface's {len(facts)}")
        # each gmsh surface to its sewn face, by centroid and area (never by trusting the order); faces alike in
        # both (a cone's apex slivers of 1e-6 mm2, all at one point) go in order, each sewn face once
        cents = np.array([f["centroid"] for f in facts])
        areas = np.array([f["area"] for f in facts])
        scale = 1e-9 * max(1.0, float(np.abs(cents).max()) if len(cents) else 1.0)
        tag_of: dict[int, int] = {}
        free = np.ones(len(facts), dtype=bool)
        for t in sorted(surfaces):
            c = np.array(gmsh.model.occ.getCenterOfMass(2, t))
            a = float(gmsh.model.occ.getMass(2, t))
            dist = np.where(free, np.linalg.norm(cents - c, axis=1), np.inf)
            best = float(dist.min())
            if not np.isfinite(best):
                raise ValueError("gmsh read more faces than the mid-surface has")
            near = np.flatnonzero(dist <= best + scale)
            k = int(near[np.argmin(np.abs(areas[near] - a))])
            free[k] = False
            tag_of[t] = k
        # SHMESH-12: a sewn face smaller than a square a fifth of the floor across (a cone's apex slivers of 1e-6
        # mm2) is no wall: it is taken out before meshing (gmsh stopped at one, "the 1D mesh seems not to be
        # forming a closed loop"); its edges stay, so its neighbours mesh up to them
        tiny = sorted(t for t, k in tag_of.items()
                      if facts[k]["area"] * mm * mm < (shell_mesh.SHORT_SHARE * plan_rules.FLOOR_WALLS
                                                       * rows[k]["wall_mm"]) ** 2)
        if tiny:
            gmsh.model.occ.remove([(2, t) for t in tiny])
            gmsh.model.occ.synchronize()
            for t in tiny:
                del tag_of[t]
        slivers = len(tiny)
        # SHMESH-1/2: a constant size per face; SHMESH-3/4/6: curves sized by curvature, grown at GROWTH
        fields = []
        top = max(rows[k]["size_mm"] for k in tag_of.values()) / mm

        def grow(curves: list[int], h0: float) -> None:
            """SHMESH-6: size h0 on these curves, growing by GROWTH per element up to the largest size in the
            part - never capped at a smaller one, which would hold every coarser face to it."""
            if h0 >= top or not curves:
                return
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "CurvesList", sorted(set(curves)))
            gmsh.model.mesh.field.setNumber(dist, "Sampling", 50)
            thr = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(thr, "InField", dist)
            gmsh.model.mesh.field.setNumber(thr, "SizeMin", h0)
            gmsh.model.mesh.field.setNumber(thr, "SizeMax", top)
            gmsh.model.mesh.field.setNumber(thr, "DistMin", 0.0)
            gmsh.model.mesh.field.setNumber(thr, "DistMax", (top - h0) / (plan_rules.GROWTH - 1.0))
            fields.append(thr)

        # SHMESH-6: a face finer than the coarsest grades its neighbours from its own edges
        rims: dict[float, list[int]] = {}
        for t, k in sorted(tag_of.items()):
            rims.setdefault(float(rows[k]["size_mm"]), []).extend(
                abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False))
        for h, curves in sorted(rims.items()):
            grow(curves, h / mm)
        for t, k in sorted(tag_of.items()):
            fid = gmsh.model.mesh.field.add("Constant")
            gmsh.model.mesh.field.setNumber(fid, "VIn", rows[k]["size_mm"] / mm)
            gmsh.model.mesh.field.setNumbers(fid, "SurfacesList", [t])
            gmsh.model.mesh.field.setNumber(fid, "IncludeBoundary", 1)
            fields.append(fid)
            if "curve_mm" in rows[k]:
                # SHMESH-3: each arc of the face refined to its OWN radius's size, then grown; its straight edges
                # and wider arcs keep the face's size (one small hole no longer refines the whole rim)
                by_size: dict[float, list[int]] = {}
                for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False):
                    r = _arc_radius(gmsh, abs(c))
                    if r is None:
                        continue
                    h0, _rule = plan_rules.curve_size(r * mm, wall=rows[k]["wall_mm"])
                    if h0 < rows[k]["size_mm"]:
                        by_size.setdefault(float(f"{h0:.6g}"), []).append(abs(c))
                for h0_mm, arcs in sorted(by_size.items()):
                    grow(arcs, h0_mm / mm)
        mn = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(mn, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(mn)
        _generate(gmsh, tag_of)
        node_tags, coords, _p = gmsh.model.mesh.getNodes()
        tags = np.asarray(node_tags, dtype=np.int64)
        index = np.full(int(tags.max()) + 1 if len(tags) else 1, -1, dtype=np.int64)
        index[tags] = np.arange(len(tags))
        xyz = np.asarray(coords, dtype=float).reshape(-1, 3) * mm
        quads, trias, q_face, t_face = [], [], [], []
        for t, k in sorted(tag_of.items()):
            types, _tags, conns = gmsh.model.mesh.getElements(2, t)
            for et, conn in zip(types, conns, strict=True):
                c = index[np.asarray(conn, dtype=np.int64)]
                if et == 3:
                    c = c.reshape(-1, 4)
                    quads.append(c)
                    q_face += [k] * len(c)
                elif et == 2:
                    c = c.reshape(-1, 3)
                    trias.append(c)
                    t_face += [k] * len(c)
                else:
                    raise ValueError(f"gmsh made an element of type {et} on a shell face")
        # SHMESH-14: the nodes inside a flat face (on no CAD edge) may move within its plane
        movable = np.zeros(len(tags), dtype=bool)
        for t in sorted(tag_of):
            if gmsh.model.getType(2, t) == "Plane":
                inner, _c, _p = gmsh.model.mesh.getNodes(2, t, includeBoundary=False)
                if len(inner):
                    movable[index[np.asarray(inner, dtype=np.int64)]] = True
        if log is not None:
            log.extend(gmsh.logger.get())
    finally:
        gmsh.finalize()
    Q = np.vstack(quads) if quads else np.zeros((0, 4), dtype=np.int64)
    T = np.vstack(trias) if trias else np.zeros((0, 3), dtype=np.int64)
    merged = 0
    if COLLAPSE_SHORT:
        # SHMESH-12: an edge under the floor, far shorter than its elements' others, is a CAD sliver: one node
        limit = np.array([plan_rules.FLOOR_WALLS * r["wall_mm"] / mm for r in rows])
        Q, T, q_face, t_face, merged = shell_mesh.collapse_short(xyz / mm, Q, T, q_face, t_face, limit)
    relaxed = 0
    if RELAX_ROUNDS:
        xyz, relaxed = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS)
    split = 0
    if SPLIT_PAST:
        # SHMESH-10: a quad past Nastran's own limits becomes two triangles
        keep, cut, came = shell_quality.split_past_quads(xyz, Q)
        if len(cut):
            qf = np.array(q_face, dtype=np.int64)
            T = np.vstack([T, cut])
            t_face = t_face + qf[came].tolist()
            Q, q_face = Q[keep], qf[keep].tolist()
            split = len(came) // 2
        if COLLAPSE_SHORT and split:
            # a quad folded nearly flat (two opposite corners a sliver apart) cuts into two slivers along that short
            # diagonal: it closes up, its edges zipped pairwise (SHMESH-12 on the cut)
            Q, T, q_face, t_face, again = shell_mesh.collapse_short(xyz / mm, Q, T, q_face, t_face, limit)
            merged += again
    flipped = 0
    if FLIP_PAST and len(T):
        # SHMESH-13: a triangle past a limit swaps its longest edge with its neighbour's when both come out better
        T, flipped = shell_quality.flip_past_trias(xyz, T, t_face)
    # keep only the nodes the elements use, renumbered in order
    used = np.unique(np.concatenate([Q.ravel(), T.ravel()]))
    remap = np.full(len(xyz), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    Q, T, xyz = remap[Q], remap[T], xyz[used]
    face_of = np.array(q_face + t_face, dtype=np.int64)
    mids = {m["id"]: m for m in record["mids"]}
    pairs = {p["id"]: p for p in record["pairs"]}
    mid_of = [owners[k] for k in face_of.tolist()]
    wall = np.array([float(pairs[mids[m]["pair"]]["thickness_mm"]) for m in mid_of])
    part = [mids[m]["body"] for m in mid_of]
    mesh = ShellMesh(xyz=xyz, quads=Q, trias=T, wall=wall, mid=mid_of, part=part)
    # groups: one per wall pair (its CAD faces), one per part
    for m in sorted(set(mid_of), key=lambda s: int(s.split("_")[1])):
        name = "+".join(mids[m]["faces"])
        mesh.groups.setdefault(name, np.zeros(0, dtype=np.int64))
        mesh.groups[name] = np.concatenate([mesh.groups[name], np.flatnonzero(np.array(mid_of) == m)])
    for p in sorted(set(part)):
        mesh.groups[p] = np.flatnonzero(np.array(part) == p)
    reference: dict[str, np.ndarray] = {}
    for k, f in enumerate(facts):
        n = _reference_normal(f["normal_face"])
        if n is not None:
            reference.setdefault(owners[k], n)
    turned = orient(mesh, reference)
    census = edge_census(mesh)
    quality = shell_quality.measure(mesh)
    quality["edges"] = {k: v for k, v in census.items() if k != "free_edges"}
    quality["turned"] = turned
    quality["split"] = split
    quality["relaxed_nodes"] = relaxed
    quality["merged_nodes"] = merged
    quality["flipped"] = flipped
    quality["sliver_faces"] = slivers
    if slivers:
        mesh.notes.append(f"{slivers} sewn face(s) under a fifth of the floor across (CAD slivers) carry no "
                          "elements (SHMESH-12).")
    if merged:
        mesh.notes.append(f"{merged} node(s) at the ends of sliver edges (under the floor, a fifth of their elements' longest) "
                          "were merged (SHMESH-12).")
    if split:
        mesh.notes.append(f"{split} quad(s) past Nastran's own limits were cut into two triangles each (SHMESH-10).")
    floored = [r["face"] for r in rows if r["floored"]]
    if floored:
        mesh.notes.append(f"{len(floored)} face(s) asked for elements finer than half their wall; they are meshed at "
                          "half the wall (SHMESH-2).")
    return mesh, {"faces": rows, "quality": quality, "per_turn": PER_TURN, "growth": plan_rules.GROWTH}


def mesh_run(out_dir: Path | str, *, mm_per_unit: float = 1.0) -> tuple[ShellMesh, dict]:
    """Mesh the mid-surface cad/midsurface wrote into `out_dir`, and write shell_plan.json there."""
    from meshpipeline.cad import midsurface as ms

    out = Path(out_dir)
    record = json.loads((out / ms.RECORD_FILE).read_text())
    mesh, applied = mesh_shell(out / ms.SURFACE_FILE, record, mm_per_unit=mm_per_unit)
    (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
    return mesh, applied


def element_turn_deg(radius_mm: float, size_mm: float) -> float:
    """How far one element turns on a curve (degrees): the SHMESH-4 check."""
    return math.degrees(size_mm / radius_mm) if radius_mm > 0 else 0.0


__all__ = ["ALGORITHM", "PER_TURN", "mesh_run", "mesh_shell"]
