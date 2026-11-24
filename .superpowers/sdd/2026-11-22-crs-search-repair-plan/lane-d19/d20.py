"""D20: netgen_occ on a multi-root STEP, FIRST ROOT ONLY (what the member has done) versus EVERY ROOT, with the
member's own code: same script, same maxh rule, same Glue of touching solids, same measurement and verdict. Full
meshing both ways, no shortcuts. Read-only.

usage: python d20.py INDEX OUT.json   (reads /c/list.json, /c/in)
"""
import json, shutil, sys, tempfile, time
from pathlib import Path

TIMEOUT_S = 600.0   # the panel's 300 s plus its one retry at twice the budget, as one call


def leg(N, path, work):
    msh = str(work / "netgen_volume.msh")
    t = time.monotonic()
    rec = N._run_netgen(path, msh, TIMEOUT_S)
    if rec.get("ok") and Path(msh).is_file():
        try:
            rec = {**rec, **N._measure(msh)}
        except Exception as exc:
            rec = {**rec, "ok": False, "stage": "measure", "error": str(exc)}
    seconds = time.monotonic() - t
    r = N.member_result("netgen_occ", rec, seconds)
    return {"status": r.status, "error_kind": r.error_kind, "min_sicn": r.min_sicn, "elements": r.elements,
            "message": r.message[:200], "solids": rec.get("solids"), "seconds": round(seconds, 1)}


if __name__ == "__main__":
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict import netgen_member as N
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    i, out = int(sys.argv[1]), sys.argv[2]
    f = json.load(open("/c/list.json"))[i]
    src = str(Path("/c/in") / f["sha256"] / Path(f["name"]).name)
    work = Path(tempfile.mkdtemp(prefix=f"d20_{i}_"))
    pool = WorkerPool(1)
    rec = {"i": i, "sha256": f["sha256"], "name": f["name"], "split": f["split"], "label": f["netgen_label"]}
    try:
        rd = pool.call("read_step_xcaf", {"path": src, "brep_out": str(work / "in.brep")}, timeout_s=900)
        step = step_for(pool, rd.value["brep"])
        rec["first_root"] = leg(N, step, work)
        brep = N._all_roots_brep(step, work)
        rec["every_root"] = leg(N, brep, work) if brep else {"status": "error", "message": "no all-roots brep"}
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        pool.close()
        shutil.rmtree(work, ignore_errors=True)
    Path(out).write_text(json.dumps(rec))
    print(json.dumps({"i": i, "first": (rec.get("first_root") or {}).get("status"), "every": (rec.get("every_root") or {}).get("status")}))
