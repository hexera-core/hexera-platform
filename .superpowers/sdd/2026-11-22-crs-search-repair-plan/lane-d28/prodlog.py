"""D28: gmsh_prod's REAL failure text on one file's engine form: the exception, the stage, gmsh's logger and the C-level
stdout/stderr HXT/TetGen print. Exactly the GmshProd profile (metres, fea_mesh_config options, reproducible)."""
import json, os, sys, time, traceback
from pathlib import Path


def main(i: int, out: str) -> None:
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict import mesh_child
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    f = json.load(open('/c/list.json'))[i]
    src = str(Path('/c/in') / f['sha256'] / Path(f['name']).name)
    pool = WorkerPool(1)
    rec = {"i": i, "sha": f["sha256"]}
    try:
        rd = pool.call("read_step_xcaf", {"path": src, "brep_out": f"/tmp/p{i}.brep"}, timeout_s=900)
        step = step_for(pool, rd.value["brep"])
    finally:
        pool.close()
    import gmsh
    scale, _ = mesh_child._profile("prod", 1.0)
    gmsh.initialize(interruptible=False)
    t = time.monotonic()
    stage = "import"
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        for k, v in mesh_child.REPRODUCIBLE.items():
            mesh_child._set(gmsh, k, v)
        gmsh.logger.start()
        mesh_child._set(gmsh, "Geometry.OCCScaling", scale)
        gmsh.model.occ.importShapes(step)
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        boxes = {int(t_): tuple(gmsh.model.getBoundingBox(2, t_)) for _, t_ in gmsh.model.getEntities(2)}
        _s, opts = mesh_child._profile("prod", mesh_child._diagonal(boxes))
        for k, v in opts.items():
            mesh_child._set(gmsh, k, v)
        rec["options"] = {k: v for k, v in opts.items()}
        with mesh_child._captured_stdout() as printed:
            stage = "2d"
            gmsh.model.mesh.generate(2)
            stage = "3d"
            gmsh.model.mesh.generate(3)
        rec["ok"] = True
    except Exception as exc:
        rec["ok"] = False
        rec["exception"] = str(exc)[:2000]
        rec["stage"] = stage
        try:
            rec["printed"] = printed["text"][-6000:]
        except Exception:
            pass
    rec["log"] = [l for l in gmsh.logger.get() if any(w in l for w in ("Error", "Warning", "error", "fail", "Fail"))][-80:]
    rec["s"] = round(time.monotonic() - t, 1)
    gmsh.finalize()
    Path(out).write_text(json.dumps(rec))
    print(json.dumps({"i": i, "ok": rec.get("ok"), "stage": rec.get("stage"), "exc": (rec.get("exception") or "")[:80]}))


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
