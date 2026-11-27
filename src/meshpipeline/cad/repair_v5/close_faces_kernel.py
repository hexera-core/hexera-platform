# Responsibility: the close_faces class's two kernel measurements (v5 step 7): the WHOLE-PART close-pair scan and
#                 the FACET-CROSSING predictor.
# Owns: `v5_close_scan`, `v5_facet_cross` (registered kernel fns) and the face tessellation they share.
# Boundaries: measures, never decides. The per-edge segment rules, the shipped sizes and the triangle test are
#             close_faces.py (pure); the class rule is diagnose.py. A pair's geometry is diagnose_kernel's
#             `pair_facts_of`, reused, not copied.
"""Close non-adjacent face pairs, and whether their facets cross at each shipped gmsh member's settings.

`v5_close_scan` (owner-approved 2026-11-24): when a mesher failure names no faces (netgen_meshing_failed, HXT
`other`), the part is scanned ONCE for non-adjacent face pairs within `max_gap_mm` - bounding boxes swept on x, then
the exact gap - under a time cap; what the cap cut off is reported (`truncated`, `examined`, `candidates`).

`v5_facet_cross`: the pair's faces are meshed in 2D by each gmsh member's own settings (close_faces_gmsh, a child
process) and the two sides' triangles tested for intersecting pairs. A side is a face GROUP, so the same call
measures a pair after a split (each side = the pieces its face became).

WHY NOT BRepMesh (the brief's first choice; measured on jig, v5 step 7): BRepMesh does not honour a coarse angular
step on a closed B-spline rim - f9501251fb30's 6.35 mm bore came back as a two-point degenerate face - and neither
it nor a hand-rolled boundary triangulation reproduces gmsh's interior nodes (gmsh_default carries boundary sizes
inward), so both predicted 0 crossings where gmsh's own 2D mesh crosses that bore's 6.096 mm shaft 87 times. The
shipped mesher's own surface stage is the faithful tessellation.
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep
from meshpipeline.cad.repair_v5 import close_faces as CF

#: = diagnose.CONTACT_MM (= sites_kernel.CONTACT_MM); a kernel module does not import its parent module
CONTACT_MM = 1e-7


def _diag(shape: Any) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _box(shape: Any) -> list[float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    return list(box.Get())


def _child(payload: dict, timeout_s: float) -> dict:
    """Run close_faces_gmsh on `payload`; {} when the child died or ran out of time (nothing measured)."""
    import json
    import os
    import subprocess
    import sys
    import tempfile

    from meshpipeline.cad.repair_v5 import close_faces_gmsh

    work = tempfile.mkdtemp(prefix="v5_facet_cross_")
    ask, answer = os.path.join(work, "payload.json"), os.path.join(work, "answer.json")
    with open(ask, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    try:
        subprocess.run([sys.executable, "-m", close_faces_gmsh.__name__, ask, answer], timeout=timeout_s,
                       capture_output=True, check=False)
        with open(answer, encoding="utf-8") as fh:
            return json.load(fh)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {}


#: at most this many neighbour faces ride with a pair into the predictor's child (their edges size the pair's rims)
MAX_NEIGHBOURS = 60


def _incidence(faces: Any) -> tuple[list[set[int]], dict[int, set[int]]]:
    """(each face's vertex hashes, vertex hash -> faces at it), 1-based face indices (index 0 unused)."""
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer

    face_vertices: list[set[int]] = [set()]
    vertex_faces: dict[int, set[int]] = {}
    for i in range(1, faces.Extent() + 1):
        own: set[int] = set()
        exp = TopExp_Explorer(faces.FindKey(i), TopAbs_VERTEX)
        while exp.More():
            h = exp.Current().TShape().__hash__()
            own.add(h)
            vertex_faces.setdefault(h, set()).add(i)
            exp.Next()
        face_vertices.append(own)
    return face_vertices, vertex_faces


def _neighbours(group: list[int], vertex_faces: dict[int, set[int]], face_vertices: list[set[int]]) -> list[int]:
    ring = sorted({f for g in group for v in face_vertices[g] for f in vertex_faces.get(v, ())} - set(group))
    return ring[:MAX_NEIGHBOURS]


@register("v5_facet_cross")
def v5_facet_cross(payload: dict) -> dict:
    """`{"pairs": [{a, b, by_member: {member: crossings | None}, triangles: {member: [na, nb]}}], "truncated"}` for
    each `pairs` entry `[faces_a, faces_b]` (kernel face indices; each side a GROUP, so a split face is measured as
    its pieces). Each pair is meshed in 2D by each gmsh member (`members`, default `close_faces.PREDICTED_MEMBERS`)
    in a child process; a member that could not mesh it is None (not measured, never "no crossing")."""
    import os
    import tempfile

    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound

    deadline = time.monotonic() + float(payload.get("budget_s") or 60.0)
    shape = read_brep(payload["brep_path"])
    faces = _maps(shape)["face"]
    part_box = _box(shape)
    face_vertices, vertex_faces = _incidence(faces)
    members = {m: p for m, p in CF.PREDICTED_MEMBERS.items() if not payload.get("members") or m in payload["members"]}
    out, truncated = [], False
    work = tempfile.mkdtemp(prefix="v5_pair_")
    for n, (side_a, side_b) in enumerate(payload["pairs"]):
        ga = [int(i) for i in side_a if 1 <= int(i) <= faces.Extent()]
        gb = [int(i) for i in side_b if 1 <= int(i) <= faces.Extent()]
        row: dict = {"a": ga, "b": gb, "by_member": dict.fromkeys(members), "triangles": {}}
        left = deadline - time.monotonic()
        if not ga or not gb or left <= 1.0:
            truncated = truncated or left <= 1.0
            out.append(row)
            continue
        ring = _neighbours([*ga, *gb], vertex_faces, face_vertices)
        compound, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(compound)
        for i in (*ga, *gb, *ring):
            builder.Add(compound, faces.FindKey(i))
        brep = os.path.join(work, f"pair{n}.brep")
        BRepTools.Write_s(compound, brep)
        answer = _child({"brep": brep, "boxes": [_box(faces.FindKey(i)) for i in (*ga, *gb, *ring)],
                         "n_a": len(ga), "n_b": len(gb), "part_box": part_box, "members": members}, timeout_s=left)
        for member in members:
            got = answer.get(member) or {}
            if "crossings" in got:
                row["by_member"][member] = int(got["crossings"])
                row["triangles"][member] = got["triangles"]
        out.append(row)
    return {"pairs": out, "truncated": truncated, "members": sorted(members),
            "diagonal_mm": round(math.dist(part_box[:3], part_box[3:]), 6)}


@register("v5_close_scan")
def v5_close_scan(payload: dict) -> dict:
    """`{"pairs": [{a, b, solid_a, solid_b, ...pair_facts_of}], "truncated", "examined", "candidates"}`: every
    face pair of the part sharing no edge and no vertex, apart (gap over contact) and within `max_gap_mm`, nearest
    first, at most `max_pairs`, under `budget_s`."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v5.diagnose_kernel import _solid_of, pair_facts_of

    started = time.monotonic()
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    faces, edges = maps["face"], maps["edge"]
    vertices = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, vertices)
    limit = float(payload["max_gap_mm"])
    diag = _diag(shape)
    boxes, edge_sets = [], []
    for i in range(1, faces.Extent() + 1):
        face = faces.FindKey(i)
        box = Bnd_Box()
        BRepBndLib.Add_s(face, box, False)
        box.Enlarge(limit / 2.0)
        boxes.append(box.Get())
        # the face's edges AND vertices: faces meeting at a corner (a vertex, no edge) touch by design, not a defect
        own: set[int] = set()
        exp = TopExp_Explorer(face, TopAbs_EDGE)
        while exp.More():
            own.add(edges.FindIndex(exp.Current()))
            exp.Next()
        exp = TopExp_Explorer(face, TopAbs_VERTEX)
        while exp.More():
            own.add(-vertices.FindIndex(exp.Current()))
            exp.Next()
        edge_sets.append(own)
    candidates = CF.candidate_pairs(boxes, edge_sets)
    solid = _solid_of(maps)

    def measure(a: int, b: int) -> dict | None:
        facts = pair_facts_of(TopoDS.Face_s(faces.FindKey(a)), TopoDS.Face_s(faces.FindKey(b)), diag)
        if facts["gap_mm"] is None or facts["gap_mm"] > limit or facts["gap_mm"] <= CONTACT_MM:
            return None   # a contact is a surface crossing's, not a close pair
        return {"a": a, "b": b, "solid_a": solid.get(a), "solid_b": solid.get(b), **facts}

    found, examined, truncated = CF.examine(candidates, measure, budget_s=float(payload.get("budget_s") or 60.0),
                                            clock=time.monotonic)
    found.sort(key=lambda p: (p["gap_mm"], p["a"], p["b"]))
    cap = int(payload.get("max_pairs") or 64)
    return {"pairs": found[:cap], "truncated": truncated, "examined": examined, "candidates": len(candidates),
            "found": len(found), "faces": faces.Extent(), "seconds": round(time.monotonic() - started, 3),
            "diagonal_mm": round(diag, 6)}
