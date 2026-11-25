"""D19 part 1: which files have a non-solid top-level root (shell/face/wire...) ahead of or among their solids, and what
Netgen's OCCGeometry sees on the engine form. No meshing.

usage: python count.py --list LIST.json --inputs DIR --out DIR --index I
"""
import argparse, json, subprocess, time, traceback
from pathlib import Path

NG = "/opt/netgen-env/bin/python"
PROBE = r'''
import sys, json
from netgen.occ import OCCGeometry
s = OCCGeometry(sys.argv[1]).shape
print("NGPROBE " + json.dumps({"type": str(s.type), "solids": len(list(s.solids)), "shells": len(list(s.shells)), "faces": len(list(s.faces))}))
'''


def roots(path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopoDS import TopoDS_Iterator
    from OCP.TopAbs import TopAbs_COMPOUND
    r = STEPControl_Reader()
    if r.ReadFile(path) != IFSelect_RetDone:
        return {"error": "read failed"}
    n = r.NbRootsForTransfer()
    r.TransferRoots()
    shape = r.OneShape()
    names = {0: "COMPOUND", 1: "COMPSOLID", 2: "SOLID", 3: "SHELL", 4: "FACE", 5: "WIRE", 6: "EDGE", 7: "VERTEX", 8: "SHAPE"}
    kids = []
    if shape.ShapeType() == TopAbs_COMPOUND:
        it = TopoDS_Iterator(shape)
        while it.More():
            kids.append(names.get(int(it.Value().ShapeType()), "?"))
            it.Next()
    else:
        kids = [names.get(int(shape.ShapeType()), "?")]
    return {"nb_roots": n, "one_shape": names.get(int(shape.ShapeType()), "?"), "children": kids[:50], "n_children": len(kids)}


def ng(path):
    p = subprocess.run([NG, "-c", PROBE, path], capture_output=True, text=True, timeout=900)
    for line in p.stdout.splitlines():
        if line.startswith("NGPROBE "):
            return json.loads(line[8:])
    return {"error": (p.stderr or p.stdout)[-300:]}


def main():
    ap = argparse.ArgumentParser()
    for a in ("--list", "--inputs", "--out"):
        ap.add_argument(a, required=True)
    ap.add_argument("--index", type=int, required=True)
    a = ap.parse_args()
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    f = json.load(open(a.list))[a.index]
    src = str(Path(a.inputs) / f["sha256"] / Path(f["name"]).name)
    rec = {"index": a.index, "sha256": f["sha256"], "name": f["name"], "split": f["split"], "netgen_label": f["netgen_label"]}
    t0 = time.monotonic()
    pool = WorkerPool(1)
    try:
        rec["original"] = roots(src)
        work = Path(a.out) / "work" / f["sha256"][:12]
        work.mkdir(parents=True, exist_ok=True)
        rd = pool.call("read_step_xcaf", {"path": src, "brep_out": str(work / "in.brep")}, timeout_s=900)
        assert rd.ok and rd.value is not None, rd.error
        ef = step_for(pool, rd.value["brep"])
        rec["engine_form"] = roots(ef)
        rec["netgen_sees"] = ng(ef)
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rec["tb"] = traceback.format_exc()[-1200:]
    finally:
        pool.close()
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    (Path(a.out) / f"{a.index:03d}_{f['sha256'][:12]}.json").write_text(json.dumps(rec))
    print(json.dumps({k: rec.get(k) for k in ("index", "wall_s", "error")}))


if __name__ == "__main__":
    main()
