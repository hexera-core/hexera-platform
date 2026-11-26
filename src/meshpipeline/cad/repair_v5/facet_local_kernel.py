# Responsibility: the facet_crossing_local class's kernel measurement: `v5_local_cross` - where each gmsh member's own
#                 2D mesh crosses itself (within a face, across a shared edge, at touching faces) and the CAUSE
#                 measurement of the faces and shared edges involved.
# Owns: `v5_local_cross` (a registered kernel fn) and the per-face cause facts.
# Boundaries: measures, never decides (the rule is facet_local.py). The 2D meshes are facet_local_gmsh.py's, one child
#             process per member, run in parallel; the dihedral is quality_kernel's, the parameter speed brep_facts'.
"""Local facet crossings and their cause, measured.

A call names FACES (kernel indices: the defect's site) or none (the whole part, up to `max_faces`). Named faces are
meshed with their first-ring neighbours (close_faces_kernel's ring: the neighbours' edges size the faces' rims), and a
crossing counts when at least one of its triangles is on a named face. Each crossing pair of faces gets its relation
(same face, adjacent = a shared edge, touching = a shared vertex or within `tol_mm`, apart) and, adjacent, the angle
between the two faces' outward normals at the shared edge; each face involved gets its cause facts (`face_facts`).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep
from meshpipeline.cad.repair_v5 import close_faces as CF
from meshpipeline.cad.repair_v5.facet_local import relation_of

#: faces whose cause is measured per call (the most crossed first), pairs whose gap is measured
MAX_FACT_FACES = 16
MAX_GAP_PAIRS = 64
#: the curvature grid per face
GRID = 7


def _box(shape: Any) -> list[float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    return list(box.Get())


def _children(base: dict, members: dict[str, str], timeout_s: float) -> dict[str, dict]:
    """One facet_local_gmsh child per member, in parallel; a member whose child died or ran out of time is absent."""
    from meshpipeline.cad.repair_v5 import facet_local_gmsh

    work = tempfile.mkdtemp(prefix="v5_local_cross_")
    procs = {}
    for member, profile in members.items():
        ask, answer = os.path.join(work, f"{member}.json"), os.path.join(work, f"{member}.answer.json")
        with open(ask, "w", encoding="utf-8") as fh:
            json.dump({**base, "member": member, "profile": profile}, fh)
        procs[member] = (subprocess.Popen([sys.executable, "-m", facet_local_gmsh.__name__, ask, answer],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), answer)
    deadline = time.monotonic() + timeout_s
    out: dict[str, dict] = {}
    for member, (proc, answer) in procs.items():
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            with open(answer, encoding="utf-8") as fh:
                got = json.load(fh)
            if "pairs" in got:
                out[member] = got
        except (subprocess.TimeoutExpired, OSError, ValueError):
            proc.kill()
    return out


#: at most this many first-ring neighbours ride with the named faces (their edges size the faces' rims)
MAX_RING = 60


def _incidence(shape: Any, faces: Any, edges: Any) -> tuple[list[set[int]], list[set[int]], dict[int, set[int]]]:
    """(each face's edge indices, each face's vertex indices, vertex index -> faces at it), 1-based face indices
    (index 0 unused). Indices of the shape's own indexed maps, so a shared edge or vertex is the same number."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedMapOfShape

    vertices = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, vertices)
    face_edges: list[set[int]] = [set()]
    face_vertices: list[set[int]] = [set()]
    vertex_faces: dict[int, set[int]] = {}
    for i in range(1, faces.Extent() + 1):
        own_e: set[int] = set()
        exp = TopExp_Explorer(faces.FindKey(i), TopAbs_EDGE)
        while exp.More():
            own_e.add(edges.FindIndex(exp.Current()))
            exp.Next()
        own_v: set[int] = set()
        exp = TopExp_Explorer(faces.FindKey(i), TopAbs_VERTEX)
        while exp.More():
            k = vertices.FindIndex(exp.Current())
            own_v.add(k)
            vertex_faces.setdefault(k, set()).add(i)
            exp.Next()
        face_edges.append(own_e - {0})
        face_vertices.append(own_v - {0})
    return face_edges, face_vertices, vertex_faces


def _solids_of(maps: dict) -> dict[int, set[int]]:
    """face index -> the solids that hold it (a conformal interface face is in two)."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out: dict[int, set[int]] = {}
    for s in range(1, maps["solid"].Extent() + 1):
        faces = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(maps["solid"].FindKey(s), TopAbs_FACE, faces)
        for i in range(1, faces.Extent() + 1):
            idx = maps["face"].FindIndex(faces.FindKey(i))
            if idx:
                out.setdefault(idx, set()).add(s)
    return out


def face_facts(face: Any, h_mm: float, *, canonical_tol_mm: float) -> dict:
    """The cause facts of one face: type, closed, curvature over h, angular span, spline degree and continuity breaks,
    parameter-speed ratio, the analytic surface it really is (its gap), width (2 x area / perimeter)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps

    from meshpipeline.cad.repair_v5.brep_facts_kernel import _speed_ratio, _type

    out: dict[str, Any] = {"type": _type(face)}
    ad = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    out["closed"] = bool(ad.IsUClosed() or ad.IsVClosed())
    kmax = 0.0
    try:
        for i in range(GRID):
            for j in range(GRID):
                u = u0 + (u1 - u0) * (i + 0.5) / GRID
                v = v0 + (v1 - v0) * (j + 0.5) / GRID
                props = BRepLProp_SLProps(ad, u, v, 2, 1e-9)
                if props.IsCurvatureDefined():
                    kmax = max(kmax, abs(props.MaxCurvature()), abs(props.MinCurvature()))
    except Exception:  # noqa: BLE001 - an unmeasurable curvature leaves the fact out
        pass
    if kmax > 0:
        out["min_radius_mm"] = round(1.0 / kmax, 6)
        out["deg_per_h"] = round(math.degrees(h_mm * kmax), 3) if h_mm > 0 else None
    t = out["type"]
    if t in ("Cylinder", "Cone"):
        out["span_deg"] = round(math.degrees(u1 - u0), 3)
    elif t in ("Sphere", "Torus", "SurfaceOfRevolution"):
        out["span_deg"] = round(math.degrees(max(u1 - u0, v1 - v0 if t != "SurfaceOfRevolution" else 0.0)), 3)
    if t in ("BSplineSurface", "BezierSurface"):
        surface = BRep_Tool.Surface_s(face)
        try:
            udeg, vdeg = int(surface.UDegree()), int(surface.VDegree())
            out["degree"] = max(udeg, vdeg)
            if t == "BSplineSurface":
                c0 = c1 = 0
                for deg, n, mult in ((udeg, surface.NbUKnots(), surface.UMultiplicity),
                                     (vdeg, surface.NbVKnots(), surface.VMultiplicity)):
                    for k in range(2, int(n)):
                        m = int(mult(k))
                        c0 += m >= deg
                        c1 += m >= deg - 1
                out["c0_breaks"], out["c1_breaks"] = c0, c1
        except Exception:  # noqa: BLE001
            pass
        ratios = [r for r in (_speed_ratio(surface, u0, u1, v0, v1, True),
                              _speed_ratio(surface, u0, u1, v0, v1, False)) if r is not None]
        if ratios:
            out["param_speed_ratio"] = round(max(ratios), 3)
        try:
            from OCP.GeomConvert import GeomConvert_SurfToAnaSurf

            conv = GeomConvert_SurfToAnaSurf(surface)
            ana = conv.ConvertToAnalytical(canonical_tol_mm)
            if ana is not None and not (hasattr(ana, "IsNull") and ana.IsNull()):
                out["canonical_gap_mm"] = round(float(conv.Gap()), 9)
                out["canonical_type"] = type(ana).__name__
        except Exception:  # noqa: BLE001 - no analytic surface found (or the converter is unavailable)
            pass
    try:
        area, length = GProp_GProps(), GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, area)
        BRepGProp.LinearProperties_s(face, length)
        if length.Mass() > 0:
            out["area_mm2"] = round(float(area.Mass()), 6)
            width = 2.0 * float(area.Mass()) / float(length.Mass())
            out["width_mm"] = round(width, 6)
            out["aspect"] = round(0.5 * float(length.Mass()) / width, 3) if width > 0 else None
    except Exception:  # noqa: BLE001
        pass
    return out


@register("v5_local_cross")
def v5_local_cross(payload: dict) -> dict:
    """`{"pairs": [{a, b, relation, by_member: {member: n}, point, edge_angle_deg?, gap_mm?}], "faces": {index: facts},
    "members": [measured], "triangles": {member: n}, "scope": "faces"|"whole", "truncated"}` (kernel face indices)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    from meshpipeline.cad.repair_v5.quality_kernel import dihedral_deg

    started = time.monotonic()
    budget = float(payload.get("budget_s") or 90.0)
    h_mm = float(payload.get("h_mm") or 0.0)
    tol = float(payload.get("tol_mm") or 0.1)
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    faces = maps["face"]
    n = faces.Extent()
    part_box = _box(shape)
    members = {m: p for m, p in CF.PREDICTED_MEMBERS.items() if not payload.get("members") or m in payload["members"]}
    named = [int(i) for i in payload.get("faces") or [] if 1 <= int(i) <= n]
    face_edges, face_vertices, vertex_faces = _incidence(shape, faces, maps["edge"])
    if named:
        ring = sorted({f for g in named for v in face_vertices[g] for f in vertex_faces.get(v, ())} - set(named))
        order = [*named, *ring[:MAX_RING]]
        compound, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(compound)
        for i in order:
            builder.Add(compound, faces.FindKey(i))
        brep = os.path.join(tempfile.mkdtemp(prefix="v5_local_"), "faces.brep")
        BRepTools.Write_s(compound, brep)
        scope = "faces"
    else:
        if n > int(payload.get("max_faces") or 1500):
            return {"pairs": [], "faces": {}, "members": [], "scope": "whole", "truncated": True,
                    "why": f"{n} faces > max_faces"}
        order, brep, scope = list(range(1, n + 1)), payload["brep_path"], "whole"
    base = {"brep": brep, "boxes": [_box(faces.FindKey(i)) for i in order], "n_focus": len(named) or len(order),
            "part_box": part_box, "cap": 20_000}
    answers = _children(base, members, timeout_s=max(5.0, budget - (time.monotonic() - started) - 10.0))

    rows: dict[tuple[int, int], dict] = {}
    for member, got in answers.items():
        for i, j, count, point in got.get("pairs") or []:
            a, b = sorted((order[int(i)], order[int(j)]))
            row = rows.setdefault((a, b), {"a": a, "b": b, "by_member": dict.fromkeys(answers, 0), "point": point})
            row["by_member"][member] = int(count)
    edges = maps["edge"]
    # facets of two DIFFERENT bodies crossing fail no per-volume mesher (step 7: shaft_in_bore meshes clean), so a
    # pair whose faces share no solid is counted aside, never a row
    solids = _solids_of(maps)
    other_body = 0
    for key in list(rows):
        a, b = key
        if a != b and solids.get(a) and solids.get(b) and not (solids[a] & solids[b]):
            other_body += sum(rows.pop(key)["by_member"].values())

    gaps_left = MAX_GAP_PAIRS
    for (a, b), row in rows.items():
        shared = face_edges[a] & face_edges[b] if a != b else set()
        gap = None
        if a != b and not shared and not (face_vertices[a] & face_vertices[b]) and gaps_left > 0:
            gaps_left -= 1
            ext = BRepExtrema_DistShapeShape(faces.FindKey(a), faces.FindKey(b))
            gap = float(ext.Value()) if ext.IsDone() else None
            row["gap_mm"] = None if gap is None else round(gap, 9)
        row["relation"] = relation_of(a, b, shared_edge=bool(shared), shared_vertex=bool(face_vertices[a]
                                                                                         & face_vertices[b]),
                                      gap_mm=gap, tol_mm=tol)
        if shared:
            angles = [dihedral_deg(TopoDS.Edge_s(edges.FindKey(k)), TopoDS.Face_s(faces.FindKey(a)),
                                   TopoDS.Face_s(faces.FindKey(b))) for k in sorted(shared)[:4]]
            angles = [x for x in angles if x is not None]
            if angles:
                row["edge_angle_deg"] = round(max(angles), 3)  # type: ignore[type-var, arg-type]
    crossed: dict[int, int] = {}
    for (a, b), row in rows.items():
        total = sum(row["by_member"].values())
        crossed[a] = crossed.get(a, 0) + total
        crossed[b] = crossed.get(b, 0) + total
    facts = {}
    for i in sorted(crossed, key=lambda k: -crossed[k])[:MAX_FACT_FACES]:
        try:
            facts[str(i)] = face_facts(TopoDS.Face_s(faces.FindKey(i)), h_mm,
                                       canonical_tol_mm=float(payload.get("fit_tol_mm") or 0.025))
        except Exception as exc:  # noqa: BLE001 - one face's facts failing is that face's "not measured"
            facts[str(i)] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"pairs": sorted(rows.values(), key=lambda r: -sum(r["by_member"].values())), "faces": facts,
            "members": sorted(answers), "triangles": {m: a.get("triangles") for m, a in answers.items()},
            "scope": scope, "truncated": len(answers) < len(members), "other_body_crossings": other_body,
            "seconds": round(time.monotonic() - started, 3)}
