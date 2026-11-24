"""Lane C11 follow-up diagnosis: a body op, then follow-up ops on its result, each judged with the full verdict.
Usage: python chain.py <step> <out.json> '<plans json>'  where plans = [[ [op, params, targets], ... ], ...];
targets "all" = every body of the CURRENT state, else a list of stable ids. Every plan starts again from the input."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(__file__))
from diag import T, _judge  # noqa: E402

from meshpipeline.cad.repair_v4 import search as S  # noqa: E402,F401
from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


def _solids(brep: str) -> list[dict]:
    from OCP.BRep import BRep_Builder
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    s = TopoDS_Shape()
    BRepTools.Read_s(s, brep, BRep_Builder())
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(s, TopAbs_SOLID, m)
    out = []
    for i in range(1, m.Extent() + 1):
        so = m.FindKey(i)
        p = GProp_GProps()
        BRepGProp.VolumeProperties_s(so, p)
        f = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(so, TopAbs_FACE, f)
        out.append({"S": i, "volume": p.Mass(), "faces": f.Extent(), "valid": bool(BRepCheck_Analyzer(so).IsValid()),
                    "orientation": so.Orientation().name})
    return out


def main(step: str, out: str, plans: list) -> None:
    rec: dict = {"step": step, "plans": []}
    pool = WorkerPool(2)
    try:
        work = tempfile.mkdtemp(prefix="c11c-")
        read = pool.call("read_step_xcaf", {"path": step, "brep_out": os.path.join(work, "in.brep")}, timeout_s=T)
        ws0 = Workspace.from_brep(pool, read.value["brep"], read.value["labels"])
        h = S.h_mm_for(pool, ws0)
        for plan in plans:
            ws, steps = ws0, []
            rec["plans"].append({"plan": plan, "steps": steps})
            for op, params, targets in plan:
                bodies = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))
                tg = bodies if targets == "all" else targets
                row: dict = {"op": op, "params": params, "targets": tg}
                t0 = time.time()
                try:
                    new, diff = REGISTRY[op].apply(pool, ws, tg, params, timeout_s=T)
                    row.update(apply_s=round(time.time() - t0, 1), declined=diff.params.get("declined"),
                               detail=str(diff.params.get("detail") or "")[:300], measured=diff.params.get("measured"))
                    if new is not ws and not row["declined"]:
                        row["solids"] = _solids(new.brep_path)
                        row["after"], _ = _judge(pool, new, step, h, parent=ws)
                        ws = new
                except Exception as exc:  # noqa: BLE001
                    row["error"] = f"{type(exc).__name__}: {exc}"[:500]
                    row["tb"] = traceback.format_exc()[-1200:]
                steps.append(row)
                json.dump(rec, open(out, "w"), indent=1, default=str)
                if row.get("declined") or row.get("error"):
                    break
    finally:
        pool.close()
    json.dump(rec, open(out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], json.load(open(os.path.join(os.path.dirname(__file__), 'plans.json')))[sys.argv[3]])
