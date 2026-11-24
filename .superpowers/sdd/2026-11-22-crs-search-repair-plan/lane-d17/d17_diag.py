"""Lane D17 diagnosis: for a file that fails gmsh_prod or netgen_occ, WHICH member fails, WHERE, and WHY in geometric terms.

On the engine form (read_step_xcaf -> BREP -> step_for: what the verdict meshes), run gmsh_prod and netgen_occ exactly
as the full panel does (MEMBER_TIMEOUT_S), place their located failures on the workspace's faces with `find_sites`,
and measure each located face and the whole part. Read-only: no op is applied.

usage: python d17_diag.py --list LIST.json --inputs DIR --out DIR --index I
"""
from __future__ import annotations

import argparse
import json
import math
import time
import traceback
from pathlib import Path
from typing import Any

SHARP_DEG = 10.0      # interior angle below this at an edge: a knife edge / thin wedge
TANGENT_DEG = 5.0     # normals within this of each other across an edge: near-tangent (smooth) meeting


def _face_geometry(brep: str, h_mm: float) -> tuple[dict[int, dict], dict]:
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.Bnd import Bnd_Box
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, brep, BRep_Builder())
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)
    names = {int(v): k for k, v in GeomAbs_SurfaceType.__members__.items()} if hasattr(GeomAbs_SurfaceType, "__members__") else {}

    def normal(face: Any, p: Any) -> Any:
        surf = BRep_Tool.Surface_s(face)
        proj = GeomAPI_ProjectPointOnSurf(p, surf)
        if proj.NbPoints() == 0:
            return None
        u, v = proj.LowerDistanceParameters()
        props = BRepLProp_SLProps(BRepAdaptor_Surface(face), u, v, 1, 1e-9)
        if not props.IsNormalDefined():
            return None
        n = props.Normal()
        if face.Orientation() == TopAbs_REVERSED:
            n.Reverse()
        return n

    # per-edge: length, degenerate, and the angle between the two faces' normals at its midpoint
    edge_info: dict[int, dict] = {}
    for i in range(1, e2f.Extent() + 1):
        edge = TopoDS.Edge_s(e2f.FindKey(i))
        faces = list(e2f.FindFromIndex(i))
        deg = BRep_Tool.Degenerated_s(edge)
        info: dict = {"degenerate": bool(deg), "faces": [fmap.FindIndex(f) for f in faces]}
        if not deg:
            try:
                c = BRepAdaptor_Curve(edge)
                info["length"] = GCPnts_AbscissaPoint.Length_s(c)
                mid = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
                uniq = []
                for f in faces:
                    if all(not f.IsSame(g) for g in uniq):
                        uniq.append(f)
                if len(uniq) == 2:
                    n1, n2 = normal(TopoDS.Face_s(uniq[0]), mid), normal(TopoDS.Face_s(uniq[1]), mid)
                    if n1 is not None and n2 is not None:
                        ang = math.degrees(n1.Angle(n2))  # 0: tangent continuation; 180: folded back (knife edge)
                        info["normal_angle"] = ang
                        info["interior"] = 180.0 - ang
                elif len(uniq) == 1:
                    info["seam_or_free"] = True
            except Exception as exc:  # noqa: BLE001
                info["error"] = str(exc)[:80]
        edge_info[i] = info

    out: dict[int, dict] = {}
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    for idx in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(idx))
        a = BRepAdaptor_Surface(face)
        t = a.GetType()
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, g)
        box = Bnd_Box()
        BRepBndLib.Add_s(face, box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        d: dict = {"type": str(t).split(".")[-1], "area": g.Mass(),
                   "bbox_diag": math.dist((x0, y0, z0), (x1, y1, z1)),
                   "u_periodic": bool(a.IsUPeriodic()), "v_periodic": bool(a.IsVPeriodic()),
                   "u_closed": bool(a.IsUClosed()), "v_closed": bool(a.IsVClosed())}
        if d["type"] in ("GeomAbs_BSplineSurface", "GeomAbs_BezierSurface"):
            try:
                d["degree"] = [a.UDegree(), a.VDegree()]
                d["poles"] = [a.NbUPoles(), a.NbVPoles()]
                d["rational"] = bool(a.IsURational() or a.IsVRational())
            except Exception:  # noqa: BLE001
                pass
        seams = degen = 0
        lengths, interiors, normals = [], [], []
        ee = TopExp_Explorer(face, TopAbs_EDGE)
        seen = set()
        while ee.More():
            edge = TopoDS.Edge_s(ee.Current())
            ei = e2f.FindIndex(edge)
            if ei not in seen:
                seen.add(ei)
                if BRep_Tool.IsClosed_s(edge, face):
                    seams += 1
                info = edge_info.get(ei, {})
                degen += int(info.get("degenerate", False))
                if "length" in info:
                    lengths.append(info["length"])
                if "interior" in info:
                    interiors.append(info["interior"])
                    normals.append(info["normal_angle"])
            ee.Next()
        d.update({"edges": len(seen), "seams": seams, "degenerate_edges": degen,
                  "min_edge_mm": min(lengths) if lengths else None,
                  "min_edge_over_h": (min(lengths) / h_mm) if lengths and h_mm else None,
                  "min_interior_deg": min(interiors) if interiors else None,
                  "sharp_edges": sum(1 for x in interiors if x < SHARP_DEG),
                  "tangent_edges": sum(1 for x in normals if x < TANGENT_DEG),
                  "size_over_h": d["bbox_diag"] / h_mm if h_mm else None})
        out[idx] = d
    allint = [e["interior"] for e in edge_info.values() if "interior" in e]
    alllen = [e["length"] for e in edge_info.values() if "length" in e]
    types: dict[str, int] = {}
    for d in out.values():
        types[d["type"]] = types.get(d["type"], 0) + 1
    part = {"faces": len(out), "edges": len(edge_info), "types": types,
            "periodic_faces": sum(1 for d in out.values() if d["u_periodic"] or d["v_periodic"]),
            "seam_faces": sum(1 for d in out.values() if d["seams"]),
            "degenerate_edges": sum(1 for e in edge_info.values() if e.get("degenerate")),
            "min_edge_mm": min(alllen) if alllen else None,
            "edges_below_h_over_100": sum(1 for x in alllen if x < h_mm / 100),
            "min_interior_deg": min(allint) if allint else None,
            "sharp_edges": sum(1 for x in allint if x < SHARP_DEG),
            "edges_without_angle": sum(1 for e in edge_info.values() if not e.get("degenerate") and "interior" not in e)}
    return out, part


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--index", type=int, required=True)
    a = ap.parse_args()
    from meshpipeline.cad.repair_v4.sites import find_sites
    from meshpipeline.cad.repair_v4.verdict.assemble import MEMBER_TIMEOUT_S, Verdict
    from meshpipeline.cad.repair_v4.verdict.gmsh_members import GmshProd
    from meshpipeline.cad.repair_v4.verdict.netgen_member import NetgenOCC
    from meshpipeline.cad.repair_v4.verdict.valid import ValidReport
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace

    f = json.load(open(a.list))[a.index]
    src = str(Path(a.inputs) / f["sha256"] / Path(f["name"]).name)
    rec: dict = {"index": a.index, **{k: f[k] for k in ("sha256", "name", "split", "label_fail")}}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pool = WorkerPool(1)
    t0 = time.monotonic()
    try:
        work = out / "work" / f["sha256"][:12]
        work.mkdir(parents=True, exist_ok=True)
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(work / "in.brep")}, timeout_s=600)
        assert read.ok and read.value is not None, read.error
        brep = read.value["brep"]
        ws = Workspace.from_brep(pool, brep, read.value.get("labels") or {})
        step = step_for(pool, brep)
        members = []
        for m in (GmshProd(), NetgenOCC()):
            t = time.monotonic()
            r = m.run(pool, step, timeout_s=MEMBER_TIMEOUT_S)
            members.append(r)
            rec.setdefault("members", []).append({
                "name": r.name, "status": r.status, "error_kind": r.error_kind, "message": (r.message or "")[:400],
                "min_sicn": r.min_sicn, "elements": r.elements, "cause": r.cause, "seconds": round(time.monotonic() - t, 1),
                "located": [{"entity": x.entity, "xyz": x.xyz, "tag": x.mesher_tag} for x in r.located][:40]})
        # gmsh pads every bounding box by 1e-7 MODEL units; gmsh_prod meshes in metres, so its located boxes are
        # 1e-4 mm too big, about a fingerprint cell, and most never match a workspace face (lane D17). Re-derive the
        # prod surfaces' fingerprints with the padding removed, and record how many each way reached a live face.
        import dataclasses

        from meshpipeline.cad.repair.identity import fingerprint_of, quantum_for
        from meshpipeline.cad.repair_v4.sites import _near_fingerprints
        from meshpipeline.cad.repair_v4.verdict import mesh_child
        from meshpipeline.cad.repair_v4.verdict.panel import Located
        live = set(ws.ids.values()) | set(_near_fingerprints(pool, ws))
        import gmsh
        gmsh.initialize(interruptible=False)
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("Geometry.OCCScaling", 1e-3)
            gmsh.model.occ.importShapes(step)
            gmsh.model.occ.synchronize()
            pboxes = {int(t): gmsh.model.getBoundingBox(2, t) for _, t in gmsh.model.getEntities(2)}
        finally:
            gmsh.finalize()
        q = quantum_for(mesh_child._diagonal(pboxes) / 1e-3)
        fixed = []
        for i, m in enumerate(members):
            if m.name != "gmsh_prod" or not m.located:
                fixed.append(m)
                continue
            locs, asis, unpad = [], 0, 0
            for x in m.located:
                tag = x.mesher_tag
                if tag.startswith("surface ") and int(tag.split()[1]) in pboxes:
                    b = [v / 1e-3 for v in pboxes[int(tag.split()[1])]]
                    p = 1e-7 / 1e-3
                    un = [b[0] + p, b[1] + p, b[2] + p, b[3] - p, b[4] - p, b[5] - p]
                    fp = fingerprint_of("face", {"bbox_min": un[:3], "bbox_max": un[3:]}, quantum=q)
                    asis += x.entity in live
                    unpad += fp in live
                    locs.append(Located(fp, x.xyz, tag))
                else:
                    locs.append(x)
            rec["members"][i]["prod_surfaces_placed"] = {"as_is": asis, "unpadded": unpad,
                                                         "surfaces": sum(1 for x in m.located
                                                                         if x.mesher_tag.startswith("surface "))}
            fixed.append(dataclasses.replace(m, located=locs))
        members = fixed
        failing = [m for m in members if m.status != "pass"]
        diag = None
        try:
            from meshpipeline.cad.repair_v4.stages import diagonal_mm
            diag = float(diagonal_mm(pool, ws))
        except Exception as exc:  # noqa: BLE001
            rec["diag_error"] = str(exc)[:200]
        h = (diag or 0.0) / 10.0     # gmsh_prod's element size: a tenth of the diagonal
        rec["diagonal_mm"], rec["h_prod_mm"] = diag, h
        geo, part = _face_geometry(brep, h)
        rec["part"] = part
        if failing:
            v = Verdict(ValidReport(True, [], [], "mm", 1, True), failing, None, False, "full")
            sites = find_sites(pool, ws, v, h_mm=h or 1.0)
            rec["sites"] = []
            for s in sites:
                faces = [e for e in (s.graph.get("mesher_faces") or []) if e[:1] == "F"]
                rec["sites"].append({
                    "kind": s.kind, "meshers": s.meshers_failing, "coarse": bool(s.graph.get("coarse")),
                    "entities": len(s.entities), "mesher_faces": faces,
                    "xyz": [list(p) for p in s.xyz[:3]],
                    "faces": {fid: geo.get(ws.indices.get(fid)) for fid in faces[:12]}})
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"[:500]
        rec["tb"] = traceback.format_exc()[-2000:]
    finally:
        pool.close()
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    (out / f"{a.index:03d}_{f['sha256'][:12]}.json").write_text(json.dumps(rec, default=str))
    print(json.dumps({"i": a.index, "wall": rec["wall_s"], "err": rec.get("error"),
                      "members": [(m["name"], m["status"], m["error_kind"]) for m in rec.get("members", [])]}))


if __name__ == "__main__":
    main()
