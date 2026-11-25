"""Plain BRepBndLib.Add (what ws_kernel.diagonal_of uses) vs AddOptimal, on every input STEP under a directory, read
through read_step_xcaf as the engine reads it. Usage: python diag3.py <dir> <out.json>"""
import glob, json, os, sys, tempfile, time


def main(root, out):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: F401 - import order
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep
    pool = WorkerPool(1); res = {}
    try:
        for p in sorted(glob.glob(os.path.join(root, "*", "*"))):
            w = tempfile.mkdtemp()
            r = pool.call("read_step_xcaf", {"path": p, "brep_out": os.path.join(w, "in.brep")}, timeout_s=600)
            if not r.ok:
                continue
            s = read_brep(r.value["brep"])
            d = {}
            for how in ("plain", "opt"):
                b = Bnd_Box(); t = time.time()
                (BRepBndLib.AddOptimal_s(s, b, False, False) if how == "opt" else BRepBndLib.Add_s(s, b))
                x = b.Get(); d[how] = ((x[3]-x[0])**2 + (x[4]-x[1])**2 + (x[5]-x[2])**2) ** 0.5
                d[how + "_s"] = round(time.time() - t, 3)
            res[p.split(os.sep)[-2][:12]] = d
            print(p.split(os.sep)[-2][:12], round(d["plain"], 2), round(d["opt"], 2), round(d["plain"] / d["opt"], 4), d["opt_s"], flush=True)
            json.dump(res, open(out, "w"))
    finally:
        pool.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
