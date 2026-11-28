# Responsibility: Thickness-aware local sizing for the check panel's meshers (owner decision 2026-11-26: the check's
#                 meshers must not fail on VALID thin walls). Measure a part's thin material walls once, plan the
#                 element size each needs, and say how a gmsh member and the Netgen member apply it.
# Owns: the sizing constants (`config`, part of every member's cache key), `plan` (pure: wall readings -> sizes,
#       floor and element cap), `measure` (OCP: the wall scan, run in a child process), `plan_for` (the memoised,
#       time-capped child run a kernel function calls) and `gmsh_fields` (the gmsh side, inside the gmsh child).
# Boundaries: no verdict here. Each member keeps its own algorithm and global size; this only adds a local size on
#             thin walls (curvature sizing and the floor are set by the members themselves). The Netgen side is in
#             netgen_occ_script.py, which cannot import this module (its interpreter has no meshpipeline).
"""THIN WALLS GET ELEMENTS ACROSS THEM, AS THE PRODUCT MESHER GIVES THEM.

The product's FEA plan (engines/fea_plan, PLAN-fea-3) gives every measured wall `WALL_ELEMENTS` elements through its
thickness: size t / WALL_ELEMENTS, held to a reach of t from the wall and then growing at `GROWTH`
(engines/gmsh/fea_adapter: a Distance field and a Threshold on it). The check panel meshed every part at one fixed
size (a tenth of the diagonal for gmsh_prod), so a valid 0.06 mm wall under a 19 mm element put the wall's two faces'
triangles through each other and the 3D mesher failed - on geometry a production mesher resolves.

THE LAW HERE is the product's, measured across the wall: near each of a wall's two faces the size is h within the
reach of the OTHER face, growing at GROWTH with the distance to it beyond (a gmsh Threshold on a Distance field to
each face, and the larger of the two: small only where both faces are near, i.e. in the thin region, however small
a part of the faces that is), in the volume as on the surface (`SURFACE_ONLY`). Netgen, which has no field law,
holds h at the wall's thin points (`MeshingParameters.RestrictH`, growing at its own grading) and gets its
close-edge refinement switched on.

WHY EVERY WALL, NOT ONLY THE THINNEST (measured on 0052_00526908, a 0.064 mm wall among 1-2.5 mm ones): sizing only
the six 0.064 mm pairs passed gmsh_prod and gmsh_default but left gmsh_alt (Frontal 3D) at minSICN 3e-4, and the
twelve thinnest at 1e-3; all 24 thinnest passed all four members (prod 50 s, default 47 s, alt 215 s, Netgen 9 s).

WHAT IS A WALL. Two faces of the SAME solid that share no edge or vertex, closer than `THIN_OF_BASE` of the
production base size (diagonal x `SCAN_OF_DIAG`) to each other, that face each other over an area (not only at a corner), whose closest points' MIDPOINT is inside the solid's material. A gap
between two bodies, or an air gap between two faces of one body (near-tangent and near-coaxial pairs, a thin slot),
is not a wall and gets no sizing: those stay defects the repair must close (owner 2026-11-26, GAP CLOSING).

THE BOUNDS, so a check stays a check:
  * FLOOR: no element below `FLOOR_OF_DIAG` x diagonal (every member's Mesh.MeshSizeMin / Netgen minh). A wall
    thinner than twice the floor is meshed at the floor and may still fail: a 1e-5 x diagonal sliver is not resolved.
  * CAP: the walls' triangle estimate (`triangles`, read from the thickness samples) is held to
    `MAX_REFINED_TRIANGLES`. Over it, every wall size is scaled up by one factor (as the product scales a plan to its
    element limit) but never past the product's validity minimum (fea_plan.KEEP_THROUGH = one element through the
    wall); still over, the THICKEST walls are dropped. The plan says so (`capped`, `scale`, `dropped`).
  * At most `MAX_CANDIDATES` face pairs examined (nearest boxes first) and `MAX_WALLS` walls sized: bounded by COUNT,
    never by a clock, so the same file always gets the same plan. The child's clock (`MEASURE_TIMEOUT_S`) is a safety
    net; a file it fires on is meshed without local sizing and the plan says `unmeasured`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from meshpipeline.engines.fea_mesh_config import CURVATURE_POINTS as _PRODUCT_CURVATURE_POINTS
from meshpipeline.engines.fea_plan import GROWTH, KEEP_THROUGH, MIN_DIVISIONS, WALL_ELEMENTS

#: bump on any change to what `measure` or `plan` produce for the same file
SIZING_VERSION = 9
#: pairs closer than this share of the diagonal are scanned: the production base size (fea_plan.MIN_DIVISIONS)
SCAN_OF_DIAG = 1.0 / MIN_DIVISIONS
#: a wall is sized only when it is thinner than this share of the production base size (diagonal / 10): the walls
#: that break a mesher are FAR under its element (fix K's dev thin walls: 0.06 mm under a 19 mm element, ratios 50-3000;
#: the injected thin_wall tube 1/55). Measured on the clean control at 1 (every wall under one element sized): five
#: clean parts lost gmsh_default/gmsh_alt to low_quality on walls of 1/7 to 1/1.4 of the base size (00529999: a 45 mm
#: wall on a 636 m diagonal; 00565319: 3.9 mm under a 29 mm element) - walls the fixed size already meshed. At 1/20,
#: 0052_00526908 kept only its 0.064 mm wall and failed again: its 0.7-1.5 mm walls (1/13-1/26) need sizing too
THIN_OF_BASE = 1.0 / 10.0
#: no element below this share of the diagonal (also each member's Mesh.MeshSizeMin / Netgen minh)
FLOOR_OF_DIAG = 1e-4
#: gmsh members: Mesh.MeshSizeFromCurvature, the PRODUCT mesher's own value (engines/fea_mesh_config.CURVATURE_POINTS;
#: owner 2026-11-27: check and product stay matched). Measured at 12 on nist_ftc_08 with no wall sized (before the
#: wall rules were bounded): gmsh_default 8k -> 388k elements and pass -> low_quality - small rounds sized to the floor.
CURVATURE_POINTS = _PRODUCT_CURVATURE_POINTS
#: Netgen: close-edge refinement on (Netgen's own "moderate" value; off by default) and its curvature safety default
NETGEN_CLOSEEDGEFAC = 2.0
NETGEN_CURVATURESAFETY = 2.0
#: samples per face side (GRID x GRID in the face's parameters) that read the wall's local thickness
GRID = 12
#: a sample is a thin point of the wall (Netgen's RestrictH points) within this factor of the wall's thickness
SPOT_FACTOR = 3.0
MAX_CANDIDATES = 2000
#: walls sized, thinnest first: every one adds two Thresholds gmsh evaluates at every point it places
MAX_WALLS = 24
#: at most this many x MAX_WALLS of the thinnest pairs are read for their samples (corners are skipped, not sized)
WALL_SCAN_FACTOR = 4
#: thin points per wall handed to Netgen, at most
MAX_POINTS = 64
#: the walls' surface triangle estimate is held to this (the members' real element counts run 10-30x it: the volume
#: grows from the walls too). 10k was tried and was worse: walls scaled past one element through are not resolved
MAX_REFINED_TRIANGLES = 150_000
#: triangles per h^2 of surface (an equilateral triangle of side h is 0.433 h^2; meshers run a little denser)
TRIANGLES_PER_H2 = 2.3
#: gmsh Distance field sampling per face direction (the product's surfaces use up to 200 too)
SAMPLING_MIN, SAMPLING_MAX = 20, 120
#: the walls' fields held on the surface mesh only (a Restrict to every curve and surface)? NO: measured on
#: 0052_00526908 the volume then fills from tiny surface triangles with no grading, and HXT took 309 s against 49 s
#: with the product's law held in the volume too (and a partial wall set failed low_quality)
SURFACE_ONLY = False
MEASURE_TIMEOUT_S = 120.0
#: the candidate pairs' exact gaps are measured in this many forked processes
MEASURE_PROCESSES = 4
CONTACT_MM = 1e-7


def config() -> dict:
    """Everything that decides the local sizing, JSON-able: part of every panel member's cache key."""
    return {"version": SIZING_VERSION, "wall_elements": WALL_ELEMENTS, "keep_through": KEEP_THROUGH, "growth": GROWTH,
            "scan_of_diag": SCAN_OF_DIAG, "thin_of_base": THIN_OF_BASE, "floor_of_diag": FLOOR_OF_DIAG, "curvature_points": CURVATURE_POINTS,
            "netgen_closeedgefac": NETGEN_CLOSEEDGEFAC, "netgen_curvaturesafety": NETGEN_CURVATURESAFETY,
            "grid": GRID, "spot_factor": SPOT_FACTOR, "max_candidates": MAX_CANDIDATES, "max_walls": MAX_WALLS,
            "max_points": MAX_POINTS, "wall_scan_factor": WALL_SCAN_FACTOR, "max_refined_triangles": MAX_REFINED_TRIANGLES,
            "triangles_per_h2": TRIANGLES_PER_H2, "sampling": [SAMPLING_MIN, SAMPLING_MAX],
            "wall": "same solid, no shared edge/vertex, closest-point midpoint inside the material",
            "law": "max over the wall's two faces of Threshold(Distance to the face)"}


def floor_mm(diag_mm: float) -> float:
    return FLOOR_OF_DIAG * diag_mm


def size_at(d: float, t: float, h: float, size_max: float) -> float:
    """The wall law at distance `d` from the other face: h within the reach (t), growing at GROWTH beyond."""
    return min(size_max, h + (GROWTH - 1.0) * max(0.0, d - t))


def triangles(wall: dict, h: float, size_max: float) -> float:
    """Estimated triangles the wall adds at size `h`, from its thickness samples: each side's samples stand for an
    equal share of that face's area, at the law's size there, plus the ring where the size grows back to `size_max`
    round the thinnest point (2 pi / (GROWTH - 1)^2 x ln(size_max / h): the ring's integral at that law)."""
    t = float(wall["t_mm"])
    total = 0.0
    for side in wall["sides"]:
        n = max(1, int(side["samples"]))
        share = float(side["area_mm2"]) / n
        total += sum(share / size_at(float(d), t, h, size_max) ** 2 for d in side["d_mm"])
    ring = 2.0 * math.pi / (GROWTH - 1.0) ** 2 * math.log(max(size_max / h, 1.0))
    return TRIANGLES_PER_H2 * (total + 2.0 * ring)


def plan(walls: Sequence[dict], diag_mm: float) -> dict:
    """The sizes for measured `walls` (each: pair, t_mm, sides [{probe, box, extent_mm, area_mm2, samples, d_mm}],
    points), thinnest first, at most MAX_WALLS: h = max(floor, t / WALL_ELEMENTS); then all scaled by one factor if
    the triangle estimate passes MAX_REFINED_TRIANGLES, never past t / KEEP_THROUGH; then the thickest dropped."""
    floor = floor_mm(diag_mm)
    size_max = SCAN_OF_DIAG * diag_mm
    chosen = sorted(walls, key=lambda w: (float(w["t_mm"]), list(w["pair"])))[:MAX_WALLS]
    dropped, cap_dropped = len(walls) - len(chosen), 0

    def sizes(k: float, ws: Sequence[dict]) -> list[float]:
        return [max(floor, min(float(w["t_mm"]) / WALL_ELEMENTS * k, float(w["t_mm"]) / KEEP_THROUGH)) for w in ws]

    def total(k: float, ws: Sequence[dict]) -> float:
        return sum(triangles(w, h, size_max) for w, h in zip(ws, sizes(k, ws), strict=True))

    estimate = total(1.0, chosen)
    scale = 1.0
    if estimate > MAX_REFINED_TRIANGLES:
        # one factor for every wall (as the product scales a plan to its element limit), never past the product's
        # validity minimum (fea_plan.KEEP_THROUGH elements through the wall): a wall sized coarser than that is not
        # resolved, and its transition only adds slivers (measured: with the budget at 10k and sizes scaled to fit,
        # 57 of 61 sized dev parts were scaled and clean parts LOST gmsh_default/gmsh_alt to low_quality)
        scale = float(WALL_ELEMENTS) / KEEP_THROUGH
        if total(scale, chosen) <= MAX_REFINED_TRIANGLES:
            lo, hi = 1.0, scale
            for _ in range(40):
                mid = math.sqrt(lo * hi)
                lo, hi = (mid, hi) if total(mid, chosen) > MAX_REFINED_TRIANGLES else (lo, mid)
            scale = hi
        while len(chosen) > 1 and total(scale, chosen) > MAX_REFINED_TRIANGLES:
            chosen = chosen[:-1]
            cap_dropped += 1
    sized = [{**w, "h_mm": h} for w, h in zip(chosen, sizes(scale, chosen), strict=True)]
    return {"status": "measured", "diag_mm": diag_mm, "floor_mm": floor, "walls": sized,
            "estimate_triangles": round(estimate), "final_triangles": round(total(scale, chosen)), "scale": scale,
            "capped": scale > 1.0 or cap_dropped > 0, "dropped": dropped + cap_dropped, "walls_read": len(walls)}


def unmeasured(why: str) -> dict:
    return {"status": f"unmeasured: {why}", "walls": [], "capped": False, "scale": 1.0}


# ---- the gmsh side (inside the gmsh child; `gmsh` is the imported module) ---------------------------------------

def sampling_for(extent: float, h: float) -> int:
    return int(min(SAMPLING_MAX, max(SAMPLING_MIN, math.ceil(extent / max(h, 1e-12)))))


def match_surface(gmsh: Any, probe: Sequence[float], box: Sequence[float], t: float,
                  boxes: dict[int, tuple]) -> int | None:
    """The gmsh surface a wall face is: the one the probe point (model units) lies on - within a quarter of the
    wall's thickness, so never the wall's other face - and of those, the one whose box is nearest the kernel's."""
    tol = 0.25 * t
    near: list[tuple[float, float, int]] = []
    for tag, b in boxes.items():
        if any(probe[i] < b[i] - tol or probe[i] > b[i + 3] + tol for i in range(3)):
            continue
        try:
            closest, _uv = gmsh.model.getClosestPoint(2, tag, list(probe))
        except Exception:  # noqa: BLE001, S112 - a surface gmsh cannot project to is not this face
            continue
        d = math.dist(closest[:3], probe)
        if d <= tol:
            near.append((round(sum(abs(b[i] - box[i]) for i in range(6)), 12), d, int(tag)))
    return min(near)[2] if near else None


def gmsh_fields(gmsh: Any, sizing: dict, scale: float, size_max: float, boxes: dict[int, tuple]) -> dict:
    """Add each wall's fields (model units = mm x `scale`) and set the Min of all walls as the background field: per
    face a Distance field (sampled at about the wall's size) and a Threshold on it, h out to the reach (t plus the
    sampling's own error), growing at GROWTH up to the member's `size_max`; the wall is the Max of its two faces'
    Thresholds. Returns what was applied."""
    walls = sizing.get("walls") or []
    fields: list[int] = []
    matched = 0
    g1 = GROWTH - 1.0
    # one Distance field per surface, shared by every wall the surface is a side of, sampled for the finest of them
    plans: list[tuple[dict, list[int]]] = []
    finest: dict[int, float] = {}
    for w in walls:
        t, h = float(w["t_mm"]) * scale, float(w["h_mm"]) * scale
        if h >= size_max or t > THIN_OF_BASE * size_max:   # a wall this member's own size already spans
            continue
        found = [match_surface(gmsh, [float(v) * scale for v in s["probe"]], [float(v) * scale for v in s["box"]],
                               t, boxes) for s in w["sides"]]
        matched_tags = [tag for tag in found if tag is not None]
        if len(matched_tags) != len(found) or matched_tags[0] == matched_tags[1]:
            continue
        plans.append((w, matched_tags))
        for tag in matched_tags:
            finest[tag] = min(finest.get(tag, math.inf), h)
    distance: dict[int, tuple[int, int]] = {}
    for w, tags in plans:
        t, h = float(w["t_mm"]) * scale, float(w["h_mm"]) * scale
        matched += 1
        thresholds = []
        for side, tag in zip(w["sides"], tags, strict=True):
            extent = float(side["extent_mm"]) * scale
            if tag not in distance:
                n = sampling_for(extent, finest[tag])
                dist = gmsh.model.mesh.field.add("Distance")
                gmsh.model.mesh.field.setNumbers(dist, "SurfacesList", [tag])
                gmsh.model.mesh.field.setNumber(dist, "Sampling", n)
                distance[tag] = (dist, n)
            dist, n = distance[tag]
            reach = t + 0.75 * extent / n
            th = gmsh.model.mesh.field.add("Threshold")
            for key, val in (("InField", dist), ("SizeMin", h), ("SizeMax", size_max), ("DistMin", reach),
                             ("DistMax", reach + (size_max - h) / g1)):
                gmsh.model.mesh.field.setNumber(th, key, float(val))
            thresholds.append(th)
        both = gmsh.model.mesh.field.add("Max")
        gmsh.model.mesh.field.setNumbers(both, "FieldsList", thresholds)
        fields.append(both)
    if fields:
        least = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(least, "FieldsList", fields)
        background = least
        if SURFACE_ONLY:
            surface = gmsh.model.mesh.field.add("Restrict")
            gmsh.model.mesh.field.setNumber(surface, "InField", least)
            gmsh.model.mesh.field.setNumbers(surface, "CurvesList", [int(t) for _d, t in gmsh.model.getEntities(1)])
            gmsh.model.mesh.field.setNumbers(surface, "SurfacesList", [int(t) for _d, t in gmsh.model.getEntities(2)])
            background = surface
        gmsh.model.mesh.field.setAsBackgroundMesh(background)
    return {"walls": len(walls), "matched": matched, "fields": len(fields), "distances": len(distance),
            "capped": bool(sizing.get("capped")), "status": sizing.get("status")}


# ---- measuring (OCP; in a child process of its own) --------------------------------------------------------------

def _read_step(step: str) -> Any:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(step) != IFSelect_RetDone:
        raise ValueError("STEP unreadable")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError("STEP holds no shape")
    return shape


def _box(shape: Any) -> list[float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    return [round(v, 9) for v in box.Get()]


def _samples(face: Any) -> list[Any]:
    """The points of a GRID x GRID lattice over the face's parameter box that are inside the face (gp_Pnt)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    surf = BRepAdaptor_Surface(face)
    out = []
    for i in range(GRID):
        for j in range(GRID):
            u = u0 + (u1 - u0) * (i + 0.5) / GRID
            v = v0 + (v1 - v0) * (j + 0.5) / GRID
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_IN:
                out.append(surf.Value(u, v))
    return out


def _thickness_at(points: list, other: Any, limit: float) -> list[tuple[Any, float]]:
    """(point, distance to the `other` face) for each point whose foot on `other`'s surface is inside `other`,
    within `limit`."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON

    surface = BRep_Tool.Surface_s(other)
    adaptor = BRepAdaptor_Surface(other)
    u0, _u1, v0, _v1 = BRepTools.UVBounds_s(other)
    out = []
    for p in points:
        proj = GeomAPI_ProjectPointOnSurf(p, surface)
        if proj.NbPoints() < 1:
            continue
        d = float(proj.LowerDistance())
        if d > limit or d <= CONTACT_MM:
            continue
        u, v = proj.LowerDistanceParameters()
        if adaptor.IsUPeriodic():
            u = u0 + (u - u0) % adaptor.UPeriod()
        if adaptor.IsVPeriodic():
            v = v0 + (v - v0) % adaptor.VPeriod()
        if BRepClass_FaceClassifier(other, gp_Pnt2d(u, v), 1e-7).State() in (TopAbs_IN, TopAbs_ON):
            out.append((p, d))
    return out


def _xyz(p: Any) -> list[float]:
    return [round(p.X(), 9), round(p.Y(), 9), round(p.Z(), 9)]


def _side(face: Any, own: list, read: list[tuple[Any, float]], t: float, closest: Any) -> tuple[dict, list]:
    """One face of a wall: its probe (a sample INSIDE it when one is thin - the closest point may sit on an edge the
    neighbouring face shares), box, area, how many samples it has and the thickness each thin-enough one read; and
    its thin points (the closest point and every sample within SPOT_FACTOR of t)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    box = _box(face)
    ordered = sorted(read, key=lambda pd: (pd[1], pd[0].X(), pd[0].Y(), pd[0].Z()))
    thin = [_xyz(p) for p, d in ordered if d <= SPOT_FACTOR * t]
    side = {"probe": thin[0] if thin else _xyz(closest), "box": box, "extent_mm": round(math.dist(box[:3], box[3:]), 9),
            "area_mm2": round(float(props.Mass()), 6), "samples": len(own),
            "d_mm": [round(d, 9) for _p, d in ordered]}
    return side, [_xyz(closest), *thin]


_FAN: dict[str, Any] = {}


def _fan_chunk(i: int) -> list:
    return _FAN["fn"](_FAN["chunks"][i])


def _fan_out(fn: Any, items: Sequence[Any]) -> list:
    """`fn` over `items` in MEASURE_PROCESSES forked processes (the panel's other members wait on this measurement,
    so their cores are idle), results in item order. One process, or no fork, runs it in place: same answer."""
    import multiprocessing

    n = min(MEASURE_PROCESSES, max(1, len(items) // 50))
    if n <= 1 or "fork" not in multiprocessing.get_all_start_methods():
        return list(fn(items))
    chunks = [items[k::n] for k in range(n)]
    _FAN.update(fn=fn, chunks=chunks)
    try:
        with multiprocessing.get_context("fork").Pool(n) as pool:
            parts = pool.map(_fan_chunk, range(n))
    finally:
        _FAN.clear()
    return [row for part in parts for row in part]


def measure(step: str) -> dict:
    """The part's thin material walls, planned (`plan`). Deterministic: bounded by counts, not a clock."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape, BRepExtrema_IsInFace
    from OCP.Extrema import Extrema_ExtFlag_MIN
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_IN, TopAbs_VERTEX
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: F401 - loads the kernel modules in their own order
    from meshpipeline.cad.repair_v4.ws_kernel import _maps
    from meshpipeline.cad.repair_v5 import close_faces as CF
    from meshpipeline.cad.repair_v5.diagnose_kernel import _solid_of

    shape = _read_step(step)
    whole = _box(shape)
    diag = math.dist(whole[:3], whole[3:])
    if not diag > 0.0:
        return unmeasured("degenerate bounding box")
    maps = _maps(shape)
    faces, edges, solids = maps["face"], maps["edge"], maps["solid"]
    vertices = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, vertices)
    limit = SCAN_OF_DIAG * diag
    boxes: list[tuple[float, float, float, float, float, float]] = []
    shared: list[set[int]] = []
    half = limit / 2.0
    for i in range(1, faces.Extent() + 1):
        face = faces.FindKey(i)
        b = _box(face)
        boxes.append((b[0] - half, b[1] - half, b[2] - half, b[3] + half, b[4] + half, b[5] + half))
        own: set[int] = set()
        for kind, index, sign in ((TopAbs_EDGE, edges, 1), (TopAbs_VERTEX, vertices, -1)):
            exp = TopExp_Explorer(face, kind)
            while exp.More():
                own.add(sign * index.FindIndex(exp.Current()))
                exp.Next()
        shared.append(own)
    solid_of = _solid_of(maps)
    candidates = [(s, a, b) for s, a, b in CF.candidate_pairs(boxes, shared)
                  if solid_of.get(a) is not None and solid_of.get(a) == solid_of.get(b)][:MAX_CANDIDATES]
    sample_cache: dict[int, list] = {}

    def samples(i: int) -> list:
        if i not in sample_cache:
            sample_cache[i] = _samples(TopoDS.Face_s(faces.FindKey(i)))
        return sample_cache[i]

    def gaps(chunk: Sequence[tuple[float, int, int]]) -> list[tuple[float, int, int, list, list]]:
        rows = []
        for _sep, a, b in chunk:
            fa, fb = TopoDS.Face_s(faces.FindKey(a)), TopoDS.Face_s(faces.FindKey(b))
            m = BRepExtrema_DistShapeShape(fa, fb, Extrema_ExtFlag_MIN)
            if not m.IsDone() or m.NbSolution() < 1:
                continue
            gap = round(float(m.Value()), 9)
            if gap <= CONTACT_MM or gap > THIN_OF_BASE * limit:
                continue
            # a parallel pair (coaxial cylinders) has several equally close solutions, the first often on the faces'
            # edges, where the midpoint is ON the solid and says nothing: read the solutions inside both faces first
            order = sorted(range(1, m.NbSolution() + 1), key=lambda k: (
                not (m.SupportTypeShape1(k) == BRepExtrema_IsInFace and m.SupportTypeShape2(k) == BRepExtrema_IsInFace), k))
            solid = solids.FindKey(solid_of[a])
            for k in order:
                pa, pb = m.PointOnShape1(k), m.PointOnShape2(k)
                mid = gp_Pnt((pa.X() + pb.X()) / 2, (pa.Y() + pb.Y()) / 2, (pa.Z() + pb.Z()) / 2)
                if BRepClass3d_SolidClassifier(solid, mid, 1e-7).State() == TopAbs_IN:
                    rows.append((gap, a, b, [pa.X(), pa.Y(), pa.Z()], [pb.X(), pb.Y(), pb.Z()]))
                    break   # otherwise an air gap (or a near-tangent pair from outside): not a wall
        return rows

    found = _fan_out(gaps, candidates)
    # the thickness samples (the expensive part) only for the walls `plan` can keep: the thinnest MAX_WALLS, in
    # plan's own order, so the plan is the one every wall's samples would give
    found.sort(key=lambda w: (w[0], w[1], w[2]))
    walls: list[dict] = []
    corners = read = 0
    for gap, a, b, xa, xb in found[:WALL_SCAN_FACTOR * MAX_WALLS]:
        if len(walls) >= MAX_WALLS:
            break
        read += 1
        fa, fb = TopoDS.Face_s(faces.FindKey(a)), TopoDS.Face_s(faces.FindKey(b))
        pa, pb = gp_Pnt(*xa), gp_Pnt(*xb)
        sides, points = [], []
        for face, other, closest, idx in ((fa, fb, pa, a), (fb, fa, pb, b)):
            side, thin = _side(face, samples(idx), _thickness_at(samples(idx), other, limit), gap, closest)
            sides.append(side)
            points += thin
        if not any(d <= SPOT_FACTOR * gap for s in sides for d in s["d_mm"]):
            # A CORNER, NOT A WALL: the two faces come this close only at their edges (no sample of either face
            # reads the other within SPOT_FACTOR x the gap across). Sized as a wall, the refinement round a corner
            # made HXT fail low_quality on clean-control 00522449 (256 such 0.32 mm "walls" on a 50 mm part)
            corners += 1
            continue
        walls.append({"pair": [a, b], "t_mm": gap, "sides": sides, "points": points[:MAX_POINTS]})
    out = plan(walls, diag)
    out["dropped"] += len(found) - len(walls) - corners
    out["corners"] = corners
    out["walls_read"] = len(found)
    out["examined"] = len(candidates)
    return out


def _memo_dir() -> Path:
    path = Path(tempfile.gettempdir()) / f"crs_thin_sizing_v{SIZING_VERSION}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def plan_for(step: str, timeout_s: float = MEASURE_TIMEOUT_S) -> dict:
    """The sizing plan of `step`, measured once per file content in a child process (an OCCT crash or hang there is
    never the mesher's verdict) and memoised on disk under a lock, so a panel's members share one measurement."""
    import fcntl

    try:
        sha = hashlib.sha256(Path(step).read_bytes()).hexdigest()
    except OSError as exc:  # the mesher reports the unreadable file; the sizing only says it measured nothing
        return unmeasured(f"cannot read {step!r}: {exc}")
    memo = _memo_dir() / f"{sha}.json"
    lock = _memo_dir() / f"{sha}.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        if memo.is_file():
            with contextlib.suppress(ValueError):
                return json.loads(memo.read_text())
        answer = memo.with_suffix(f".{os.getpid()}.answer")
        try:
            proc = subprocess.run([sys.executable, "-m", __name__, step, str(answer)], capture_output=True,
                                  text=True, timeout=timeout_s, check=False)
            if proc.returncode == 0 and answer.is_file():
                result = json.loads(answer.read_text())
            else:
                result = unmeasured(f"measure exited {proc.returncode}: {(proc.stderr or '').strip()[-200:]}")
        except subprocess.TimeoutExpired:
            result = unmeasured(f"measure exceeded {timeout_s:.0f}s")
        finally:
            with contextlib.suppress(OSError):
                answer.unlink()
        tmp = memo.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, sort_keys=True))
        tmp.replace(memo)
        return result


def main(argv: Sequence[str]) -> int:
    step, out = argv
    try:
        result = measure(step)
    except Exception as exc:  # noqa: BLE001 - an unreadable part is meshed without local sizing, and says so
        result = unmeasured(f"{type(exc).__name__}: {exc}"[:300])
    Path(out).write_text(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
