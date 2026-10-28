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

from meshpipeline.engines import shell_plan as plan_rules
from meshpipeline.engines import shell_quality
from meshpipeline.engines.shell_mesh import ShellMesh, edge_census, orient

logger = logging.getLogger(__name__)

#: gmsh: Frontal-Delaunay for quads, Blossom recombination; quad-dominant (a few triangles where quads cannot fit)
ALGORITHM = 8
RECOMBINATION = 1
#: SHMESH-3/4 through gmsh's own curvature sizing: elements per full turn of every curve and curved face
PER_TURN = 4 * plan_rules.CURVE_PER_QUARTER


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
        out.append({"centroid": np.array([c.X(), c.Y(), c.Z()]), "width": ms.strip_width(f) * mm,
                    "box_width": ms.width_of(f) * mm, "radii": radii, "bend": fm.face_radius(f, mm),
                    "normal_face": f})
    return out


def _reference_normal(face) -> np.ndarray | None:
    """The face's oriented normal at its middle (the side its sheet's normals are turned to)."""
    from meshpipeline.cad import midsurface as ms

    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

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
    import gmsh

    mm = float(mm_per_unit)
    facts = _faces_and_facts(Path(brep), mm)
    owners = record["surface_faces"]
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
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
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
        # each gmsh surface to its sewn face, by centroid (never by trusting the order)
        cents = np.array([f["centroid"] for f in facts])
        tag_of: dict[int, int] = {}
        for t in surfaces:
            c = np.array(gmsh.model.occ.getCenterOfMass(2, t))
            k = int(np.argmin(np.linalg.norm(cents - c, axis=1)))
            if k in tag_of.values():
                raise ValueError("two gmsh faces matched one mid-surface face")
            tag_of[t] = k
        # SHMESH-1/2: a constant size per face; SHMESH-3/4/6: curves sized by curvature, grown at GROWTH
        fields = []
        for t, k in sorted(tag_of.items()):
            fid = gmsh.model.mesh.field.add("Constant")
            gmsh.model.mesh.field.setNumber(fid, "VIn", rows[k]["size_mm"] / mm)
            gmsh.model.mesh.field.setNumbers(fid, "SurfacesList", [t])
            gmsh.model.mesh.field.setNumber(fid, "IncludeBoundary", 1)
            fields.append(fid)
            if "curve_mm" in rows[k]:
                curves = [abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)]
                dist = gmsh.model.mesh.field.add("Distance")
                gmsh.model.mesh.field.setNumbers(dist, "CurvesList", curves)
                gmsh.model.mesh.field.setNumber(dist, "Sampling", 50)
                thr = gmsh.model.mesh.field.add("Threshold")
                h0, h1 = rows[k]["curve_mm"] / mm, rows[k]["size_mm"] / mm
                gmsh.model.mesh.field.setNumber(thr, "InField", dist)
                gmsh.model.mesh.field.setNumber(thr, "SizeMin", h0)
                gmsh.model.mesh.field.setNumber(thr, "SizeMax", h1)
                gmsh.model.mesh.field.setNumber(thr, "DistMin", 0.0)
                gmsh.model.mesh.field.setNumber(thr, "DistMax", max(h1 - h0, 0.0) / (plan_rules.GROWTH - 1.0))
                fields.append(thr)
        mn = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(mn, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(mn)
        gmsh.model.mesh.generate(2)
        node_tags, coords, _p = gmsh.model.mesh.getNodes()
        index = {int(t): i for i, t in enumerate(node_tags)}
        xyz = np.asarray(coords, dtype=float).reshape(-1, 3) * mm
        quads, trias, q_face, t_face = [], [], [], []
        for t, k in sorted(tag_of.items()):
            types, _tags, conns = gmsh.model.mesh.getElements(2, t)
            for et, conn in zip(types, conns, strict=True):
                c = np.vectorize(index.get)(np.asarray(conn, dtype=np.int64))
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
        if log is not None:
            log.extend(gmsh.logger.get())
    finally:
        gmsh.finalize()
    Q = np.vstack(quads) if quads else np.zeros((0, 4), dtype=np.int64)
    T = np.vstack(trias) if trias else np.zeros((0, 3), dtype=np.int64)
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
    reference = {}
    for k, f in enumerate(facts):
        n = _reference_normal(f["normal_face"])
        if n is not None:
            reference.setdefault(owners[k], n)
    turned = orient(mesh, reference)
    census = edge_census(mesh)
    quality = shell_quality.measure(mesh)
    quality["edges"] = {k: v for k, v in census.items() if k != "free_edges"}
    quality["turned"] = turned
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
