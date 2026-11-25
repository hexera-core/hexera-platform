"""0051: cut_by:B4, then the same output with its ZERO-VOLUME solids dropped, both judged in full."""
import dataclasses, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from diag import T, _judge
from chain import _solids


def drop_zero(brep, eps):
    from OCP.BRep import BRep_Builder
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator, TopoDS_Shape
    s = TopoDS_Shape(); BRepTools.Read_s(s, brep, BRep_Builder())
    dropped = []
    def walk(n):
        c = TopoDS_Compound(); b = BRep_Builder(); b.MakeCompound(c); it = TopoDS_Iterator(n); k = 0
        while it.More():
            x = it.Value(); it.Next()
            if x.ShapeType() == TopAbs_COMPOUND:
                sub, m = walk(TopoDS.Compound_s(x))
                if m: b.Add(c, sub); k += 1
                continue
            if x.ShapeType() == TopAbs_SOLID:
                p = GProp_GProps(); BRepGProp.VolumeProperties_s(x, p)
                if abs(p.Mass()) <= eps:
                    dropped.append(p.Mass()); continue
            b.Add(c, x); k += 1
        return c, k
    out, _ = walk(s)
    path = brep[:-5] + ".nozero.brep"; BRepTools.Write_s(out, path)
    return path, dropped


def main(step, out):
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace
    pool = WorkerPool(2); rec = {}
    try:
        w = tempfile.mkdtemp()
        r = pool.call("read_step_xcaf", {"path": step, "brep_out": os.path.join(w, "in.brep")}, timeout_s=T)
        ws = Workspace.from_brep(pool, r.value["brep"], r.value["labels"]); h = S.h_mm_for(pool, ws)
        new, d = REGISTRY["conformalise"].apply(pool, ws, ["B1", "B2", "B3", "B4"], {"mode": "cut_by:B4"}, timeout_s=T)
        rec["cut_solids"] = _solids(new.brep_path)
        rec["cut"], _ = _judge(pool, new, step, h, parent=ws)
        path, dropped = drop_zero(new.brep_path, 1e-9)
        rec["dropped"] = dropped
        stripped = Workspace.from_brep(pool, path)  # fresh ids: judged as a part, faithfulness vs the input BREP
        stripped = dataclasses.replace(stripped, original=ws.original)
        rec["nozero_solids"] = _solids(path)
        rec["nozero"], _ = _judge(pool, stripped, step, h)
    finally:
        pool.close()
        json.dump(rec, open(out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
