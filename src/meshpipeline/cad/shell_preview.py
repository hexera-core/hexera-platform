# Responsibility: The SHELL CHECK of an uploaded CAD part, for the FEA stage (SHSTAGE-2): extract its mid-surface
# (cad/midsurface: shell.json + midsurface.brep), then draw what the stage shows - the sewn mid-surface as
# triangles, each with the wall it stands for (mm), the open edges, and where each region that stays solid sits.
# Run in a process of its own (main()): OpenCASCADE work that can crash costs the shell check, never the worker.
# Boundaries: geometry only, deterministic, no model call, no store. Writes into the folder it is given.
# Collaborates with: cad/midsurface.py (the extraction), application/shell_check.py (runs it in a child process,
# stores what it wrote, builds the stage's view), ui/js/viewer/geometry_stage.js (draws overlay.json).
"""overlay.json:

    {"version": 1, "unit": "mm", "xyz": <base64 float32, n*3>, "tri": <base64 uint32, m*3>,
     "wall": <base64 float32, m: each triangle's wall, mm>, "t_min", "t_max", "triangles",
     "open_at_mm": [[x, y, z], ...], "solid_at_mm": {"<region key>": [x, y, z]}}

Lengths in the millimetres of the part as read (`mm_per_unit`), the frame the stage's skin is drawn in.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

VERSION = 1
OVERLAY_FILE = "overlay.json"
#: The drawing's chord, as a share of the part's diagonal: a picture's tessellation, not a mesh.
CHORD_SHARE = 2e-3
ANGLE_RAD = 0.35
#: At most this many triangles are sent to the stage; a finer drawing is thinned evenly (every k-th face's
#: triangles are kept whole, so the drawing has no holes inside a face).
MAX_TRIANGLES = 400_000


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def face_names(shape) -> list[str]:
    """The names cad/midsurface gives the faces: Face_k for one body, Part_k_Face_j for several."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    smap, fmap = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, smap)
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    if smap.Extent() > 1:
        from meshpipeline.cad.fea_assembly import global_face_names

        return global_face_names(shape)
    return [f"Face_{k}" for k in range(1, fmap.Extent() + 1)]


def _mid_walls(record: dict) -> list[float]:
    """The wall (mm) of every sewn face, in the surface's own order (record "surface_faces")."""
    walls = {}
    for m in record.get("mids") or []:
        t = m.get("thickness_mean_mm") if m.get("fitted") else m.get("thickness_mm")
        walls[str(m.get("id"))] = float(t or 0.0)
    return [walls.get(str(k), 0.0) for k in record.get("surface_faces") or []]


def overlay(surface, record: dict, *, mm_per_unit: float, diag_mm: float) -> dict:
    """The sewn mid-surface as the stage draws it: triangles in mm, each with its face's wall."""
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Iterator

    from meshpipeline.cad.occ_box import incremental_mesh

    mm = float(mm_per_unit)
    walls = _mid_walls(record)
    faces = []
    it = TopoDS_Iterator(surface)
    while it.More():
        faces.append(TopoDS.Face_s(it.Value()))
        it.Next()
    if faces:
        incremental_mesh(surface, max(CHORD_SHARE * diag_mm / mm, 1e-9), ANGLE_RAD, relative=False, parallel=False)
    blocks, tris, tri_wall = [], [], []
    base = 0
    for i, face in enumerate(faces):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
        trsf = loc.Transformation()
        pts = np.array([(lambda p: (p.X(), p.Y(), p.Z()))(tri.Node(k).Transformed(trsf))
                        for k in range(1, tri.NbNodes() + 1)], dtype=float) * mm
        t = np.array([tri.Triangle(k).Get() for k in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1 + base
        blocks.append(pts)
        tris.append(t)
        tri_wall.append(np.full(len(t), walls[i] if i < len(walls) else 0.0))
        base += len(pts)
    xyz = np.vstack(blocks) if blocks else np.zeros((0, 3))
    tri_all = np.vstack(tris) if tris else np.zeros((0, 3), dtype=np.int64)
    wall = np.concatenate(tri_wall) if tri_wall else np.zeros(0)
    if len(tri_all) > MAX_TRIANGLES:
        step = int(np.ceil(len(tri_all) / MAX_TRIANGLES))
        keep = np.arange(len(tri_all)) % step == 0
        tri_all, wall = tri_all[keep], wall[keep]
    shown = wall[wall > 0]
    return {"version": VERSION, "unit": "mm", "xyz": _b64(xyz.astype(np.float32).ravel()),
            "tri": _b64(tri_all.astype(np.uint32).ravel()), "wall": _b64(wall.astype(np.float32)),
            "triangles": int(len(tri_all)),
            "t_min": round(float(shown.min()), 4) if len(shown) else None,
            "t_max": round(float(shown.max()), 4) if len(shown) else None,
            "open_at_mm": [list(map(float, q)) for row in record.get("sewn") or [] for q in row.get("open_at_mm") or []]}


def solid_places(shape, record: dict, names: list[str], *, mm_per_unit: float) -> dict[str, list[float]]:
    """Where each region that stays solid sits (the centre of its faces' area, mm), keyed "<body>:<k>" - the
    k-th solid region of that body - and "<body>" for a body that stays solid whole."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    index = {n: k for k, n in enumerate(names, start=1)}
    mm = float(mm_per_unit)

    def centre(face_names: list[str]) -> list[float] | None:
        total, acc = 0.0, np.zeros(3)
        for n in face_names:
            k = index.get(str(n))
            if not k:
                continue
            g = GProp_GProps()
            BRepGProp.SurfaceProperties_s(TopoDS.Face_s(fmap.FindKey(k)), g)
            a = float(g.Mass())
            c = g.CentreOfMass()
            acc += a * np.array([c.X(), c.Y(), c.Z()])
            total += a
        return [round(float(v) * mm, 4) for v in acc / total] if total > 0 else None

    out: dict[str, list[float]] = {}
    for row in record.get("bodies") or []:
        body = str(row.get("body"))
        for k, reg in enumerate(row.get("solid_regions") or [], start=1):
            at = centre(list(reg.get("faces") or []))
            if at:
                out[f"{body}:{k}"] = at
        if row.get("verdict") == "solid":
            mine = [str(f["face"]) for f in record.get("faces") or [] if str(f.get("body")) == body]
            at = centre(mine)
            if at:
                out[body] = at
    return out


def mixed_auto(shape, surface, record: dict, *, mm_per_unit: float) -> dict:
    """MIXED-4 / MIXED-8 for the stage's Auto: {"ok": Auto recommends Mixed, "why": the reason in words, "too_thick":
    the walls kept solid}. Mixed is recommended only with every shell wall's reach measured (engines/mixed_run
    .recommend reads MIXED-8 only when given it): a reach that cannot be measured, or one wall without it, is no
    Mixed - the box and the casting the reach keeps solid would otherwise be offered."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx
    from meshpipeline.engines import mixed_run as mr

    started = time.time()
    ok, why = mr.recommend(record)
    out: dict = {"ok": False, "why": why, "too_thick": []}
    if not ok:
        return out
    try:
        solids = mx._solids(ms.solidify(shape, float(mm_per_unit))[0])
        if len(solids) != 1:
            out["why"] = f"The part reads as {len(solids)} solids, not one closed body. It is meshed as a solid."
            return out
        reach = mr.reach_of(record, solids[0], surface, float(mm_per_unit))
    except Exception as exc:  # noqa: BLE001 - no reach, no Mixed: Auto says solid
        logger.warning("shell check: the walls' reach could not be measured (%s)", type(exc).__name__)
        reach = {}
    shell_ids = {str(m.get("id")) for m in record.get("mids") or [] if m.get("shell")}
    if not reach or not shell_ids <= set(reach):
        out["why"] = ("Its walls' reach from their joints could not be measured, so a shell-and-solid mesh is not "
                      "offered. It is meshed as a solid.")
        return out
    ok, why = mr.recommend(record, reach)
    bad, _share, _stop = mr.too_thick(record, reach)
    out.update(ok=bool(ok), why=why, too_thick=list(bad), seconds=round(time.time() - started, 2))
    return out


def run(path: Path | str, out_dir: Path | str, *, mm_per_unit: float) -> dict:
    """Extract, draw and write: shell.json, midsurface.brep and overlay.json in `out_dir`. Returns the record."""
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad.scout import read_cad

    started = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shape = read_cad(Path(path))
    names = face_names(shape)
    try:
        record, surface = ms.extract(shape, mm_per_unit=mm_per_unit, names=names)
    except Exception as exc:  # noqa: BLE001 - SHELL-6: never fail; the part is meshed solid, with the reason
        logger.warning("shell check: the extraction failed (%s)", exc)
        record = ms.solid_record(shape, f"the mid-surface could not be extracted ({type(exc).__name__})")
        surface = ms._compound([])
    if any(str(b.get("verdict")) == "mixed" for b in record.get("bodies") or []):
        record["mixed_auto"] = mixed_auto(shape, surface, record, mm_per_unit=mm_per_unit)
    BRepTools.Write_s(surface, str(out / ms.SURFACE_FILE))
    (out / ms.RECORD_FILE).write_text(json.dumps(record, indent=1, sort_keys=True))
    diag = float(record.get("diag_mm") or 0.0)
    if diag <= 0:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        b = Bnd_Box()
        BRepBndLib.Add_s(shape, b)
        x0, y0, z0, x1, y1, z1 = b.Get()
        diag = float(np.linalg.norm([x1 - x0, y1 - y0, z1 - z0])) * mm_per_unit
    drawn = overlay(surface, record, mm_per_unit=mm_per_unit, diag_mm=diag)
    try:
        drawn["solid_at_mm"] = solid_places(shape, record, names, mm_per_unit=mm_per_unit)
    except Exception as exc:  # noqa: BLE001 - the list still says what stays solid, unmarked
        logger.warning("shell check: solid regions not placed (%s)", type(exc).__name__)
        drawn["solid_at_mm"] = {}
    drawn["seconds"] = round(time.time() - started, 2)
    (out / OVERLAY_FILE).write_text(json.dumps(drawn))
    return record


def main(argv: list[str] | None = None) -> int:
    """python -m meshpipeline.cad.shell_preview <cad file> <out dir> <mm per unit>"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(main.__doc__, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING)
    run(args[0], args[1], mm_per_unit=float(args[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MAX_TRIANGLES", "OVERLAY_FILE", "VERSION", "face_names", "main", "mixed_auto", "overlay", "run", "solid_places"]
