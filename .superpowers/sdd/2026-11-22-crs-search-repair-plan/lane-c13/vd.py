"""Lane C13 (c) verdict discipline: the full verdict on a set of (input STEP, optional state BREP) pairs, under
whatever tree is on PYTHONPATH. Per item: valid, clean, every member's status / error_kind / min_sicn, and layer 3's
ok / reasons / bbox_ratio / diag_after_mm. Run once on the old tree and once on the new; compare.
Usage: python vd.py <out.json> <step>[::<state.brep>] ..."""
import json
import os
import sys
import tempfile


def main(out, items):
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict.assemble import verdict
    from meshpipeline.cad.repair_v4.verdict.faithful import check_faithful
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace
    pool = WorkerPool(2)
    res = {}
    try:
        for item in items:
            step, _, state = item.partition("::")
            w = tempfile.mkdtemp()
            r = pool.call("read_step_xcaf", {"path": step, "brep_out": os.path.join(w, "in.brep")}, timeout_s=900)
            ws = Workspace.from_brep(pool, r.value["brep"], r.value["labels"])
            if state:
                v = verdict(pool, step_for(pool, state), step)
                f = check_faithful(pool, ws.original, state, declared_added=[], declared_removed=[])
            else:
                v = S._judged(verdict, pool, ws, step)
                f = check_faithful(pool, ws.original, ws.brep_path, declared_added=[], declared_removed=[])
            res[item] = {
                "valid": v.valid.ok, "clean": v.clean,
                "panel": [[m.name, m.status, m.error_kind, m.min_sicn] for m in v.panel],
                "faithful": {"ok": f.ok, "reasons": f.reasons, "bbox_ratio": f.bbox_ratio,
                             "diag_after_mm": f.diag_after_mm, "max_dev_mm": f.max_dev_mm, "bound_mm": f.bound_mm},
            }
            print(os.path.basename(step)[:24], bool(state), json.dumps(res[item])[:300], flush=True)
            json.dump(res, open(out, "w"), indent=1, default=str)
    finally:
        pool.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
