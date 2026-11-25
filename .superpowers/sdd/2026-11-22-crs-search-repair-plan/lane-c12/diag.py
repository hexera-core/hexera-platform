"""Lane C11 diagnosis: what the BODY ops leave behind.

For one input STEP: full verdict on the input (layer 1 findings, every panel member with its located entities and
the sites the search would build), then every body op the det sweep would propose on each overlap/intersection site
(conformalise fragment, cut_by each body, fuse), each judged with the FULL verdict, and the same breakdown after.
Faces a failing member names are tagged NEW (minted by the op: the cut seam) or OLD (untouched by the op).
Usage: python diag.py <step> <out.json>
"""
from __future__ import annotations

import json
import sys
import time
import traceback

from meshpipeline.cad.repair_v4 import search as S
from meshpipeline.cad.repair_v4.ops import REGISTRY
from meshpipeline.cad.repair_v4.sites import find_sites
from meshpipeline.cad.repair_v4.verdict.assemble import verdict
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Workspace

T = 900


def _strip_empty(brep: str) -> tuple[str | None, int]:
    """`brep` with every EMPTY sub-compound dropped (what body_kernel._rebuild leaves when a drop empties one), or
    (None, 0) when there is none. Same TShapes otherwise, so every fingerprint is unchanged."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_COMPOUND
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator, TopoDS_Shape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, brep, BRep_Builder())
    dropped = [0]

    def walk(node):
        comp = TopoDS_Compound()
        b = BRep_Builder()
        b.MakeCompound(comp)
        it = TopoDS_Iterator(node)
        n = 0
        while it.More():
            c = it.Value()
            it.Next()
            if c.ShapeType() == TopAbs_COMPOUND:
                sub, k = walk(TopoDS.Compound_s(c))
                if k == 0:
                    dropped[0] += 1
                    continue
                b.Add(comp, sub)
            else:
                b.Add(comp, c)
            n += 1
        return comp, n

    if shape.ShapeType() != TopAbs_COMPOUND:
        return None, 0
    out, _ = walk(shape)
    if not dropped[0]:
        return None, 0
    path = brep[:-5] + ".stripped.brep"
    BRepTools.Write_s(out, path)
    return path, dropped[0]


def _members(v, ws, parent=None):
    fp_to_id = {fp: sid for sid, fp in ws.ids.items()}
    new = set()
    if parent is not None:
        new = {sid for sid, fp in ws.ids.items() if parent.ids.get(sid) != fp}
    out = []
    for m in v.panel:
        loc = []
        for x in (m.located or [])[:12]:
            sid = fp_to_id.get(x.entity) if x.entity else None
            loc.append({"id": sid, "new": (sid in new) if sid else None, "xyz": x.xyz, "tag": x.mesher_tag})
        out.append({"name": m.name, "status": m.status, "kind": m.error_kind, "sicn": m.min_sicn,
                    "msg": (m.message or "")[:300], "s": round(m.seconds, 1), "located": loc,
                    "located_total": len(m.located or [])})
    return out


def _judge(pool, ws, original, h, parent=None):
    t0 = time.time()
    v = S._judged(verdict, pool, ws, original)
    sites = find_sites(pool, ws, v, h_mm=h)
    return {
        "seconds": round(time.time() - t0, 1), "valid": v.valid.ok, "clean": v.clean,
        "hard": [{"code": f.code, "entities": f.entities[:8], "detail": {k: f.detail[k] for k in list(f.detail)[:6]}}
                 for f in v.valid.hard],
        "solids": v.valid.solids, "panel_pass": sum(m.status == "pass" for m in v.panel), "panel": _members(v, ws, parent),
        "sites": [{"id": s.id, "kind": s.kind, "entities": s.entities[:10], "n": len(s.entities),
                   "meshers": s.meshers_failing, "whole_part": ((s.graph or {}).get("coarse") or {}).get("whole_part")}
                  for s in sites],
        "faithful": None if v.faithful is None else {"ok": getattr(v.faithful, "ok", None),
                                                     "max_dev_mm": getattr(v.faithful, "max_dev_mm", None)},
    }, sites


def main(step: str, out: str) -> None:
    rec = {"step": step}
    pool = WorkerPool(2)
    try:
        import os
        import tempfile
        work = tempfile.mkdtemp(prefix="c11-")
        read = pool.call("read_step_xcaf", {"path": step, "brep_out": os.path.join(work, "in.brep")}, timeout_s=T)
        ws = Workspace.from_brep(pool, read.value["brep"], read.value["labels"])
        h = S.h_mm_for(pool, ws)
        rec["h_mm"] = h
        rec["bodies"] = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))
        rec["input"], sites = _judge(pool, ws, step, h)
        json.dump(rec, open(out, "w"), indent=1, default=str)
        tried = set()
        rec["ops"] = []
        for site in sites:
            if site.kind not in ("overlap", "intersection", "self_overlap"):
                continue
            bodies = [e for e in site.entities if e.startswith("B")]
            if len(bodies) < 2:
                continue
            calls = [("conformalise", {"mode": "fragment"})]
            calls += [("conformalise", {"mode": f"cut_by:{b}"}) for b in bodies]
            calls += [("fuse", {})]
            for op, params in calls:
                key = (op, json.dumps(params), tuple(bodies))
                if key in tried:
                    continue
                tried.add(key)
                row = {"site": site.id, "op": op, "params": params, "targets": bodies}
                t0 = time.time()
                try:
                    new, diff = REGISTRY[op].apply(pool, ws, bodies, params, timeout_s=T)
                    row["apply_s"] = round(time.time() - t0, 1)
                    row["declined"] = diff.params.get("declined")
                    row["detail"] = str(diff.params.get("detail") or "")[:300]
                    row["measured"] = diff.params.get("measured")
                    if new is not ws and not row["declined"]:
                        row["after"], _ = _judge(pool, new, step, h, parent=ws)
                        stripped, n = _strip_empty(new.brep_path)
                        row["empty_compounds"] = n
                        if stripped:
                            import dataclasses
                            row["after_stripped"], _ = _judge(pool, dataclasses.replace(new, brep_path=stripped),
                                                              step, h, parent=ws)
                except Exception as exc:  # noqa: BLE001 - diagnosis keeps going
                    row["error"] = f"{type(exc).__name__}: {exc}"[:500]
                    row["tb"] = traceback.format_exc()[-1500:]
                rec["ops"].append(row)
                json.dump(rec, open(out, "w"), indent=1, default=str)
    finally:
        pool.close()
    json.dump(rec, open(out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
