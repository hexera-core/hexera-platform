"""B8: per state, each of the four panel members run SEPARATELY and timed (status, min SICN, seconds), plus valid and
faithful from the full verdict assembled from those same results. States: the input, the staged output (cheap-tier
transactions only, no full confirmation - what v1's search started from), and children the v1 search_ai evaluated
on these files (split_angle 90 on all faces, unify_same_domain all, tighten_tolerances 1e-5, run_stage condition)."""
import json, sys, time, traceback
from pathlib import Path


def main():
    from meshpipeline.cad.repair_v4 import ops
    from meshpipeline.cad.repair_v4.stages import run_stage, run_staged, step_for
    from meshpipeline.cad.repair_v4.verdict.assemble import MEMBER_TIMEOUT_S, default_members, verdict
    from meshpipeline.cad.repair_v4.verdict.panel import run_panel
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace

    src, out, with_children = sys.argv[1], Path(sys.argv[2]), sys.argv[3] == "1"
    pool = WorkerPool(2)
    rec = {"file": Path(src).name, "states": []}

    def measure(label, ws, step):
        rows, results = {}, []
        for m in default_members("full"):
            t = time.time()
            [r] = run_panel(pool, step, [m], timeout_s=MEMBER_TIMEOUT_S)
            rows[m.name] = {"status": r.status, "error_kind": r.error_kind, "min_sicn": r.min_sicn,
                            "seconds": round(time.time() - t, 2)}
            results.append(r)
        v = verdict(pool, step, src, run_panel=lambda *a, **k: results)
        st = {"state": label, "members": rows, "valid": bool(v.valid.ok),
              "faithful": None if v.faithful is None else bool(v.faithful.ok), "full_clean": bool(v.clean)}
        rec["states"].append(st)
        print(json.dumps(st, default=str), flush=True)

    try:
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(out / (Path(src).stem + ".in.brep"))},
                         timeout_s=300)
        ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
        measure("input", ws, src)
        if with_children:
            staged = run_staged(pool, ws).ws
            if staged.brep_path != ws.brep_path:
                measure("staged(cheap-only)", staged, step_for(pool, staged.brep_path))
            faces = sorted((s for s in staged.ids if s[0] == "F"), key=lambda s: int(s[1:]))
            for label, op, targets, params in (
                    ("split_angle 90", "split_angle", faces, {"max_deg": 90}),
                    ("unify_same_domain all", "unify_same_domain", [], {"scope": "all", "keep": []}),
                    ("tighten_tolerances 1e-5", "tighten_tolerances", [], {"target_rel": 1e-5})):
                try:
                    new, diff = ops.REGISTRY[op].apply(pool, staged, targets, params, timeout_s=600)
                    if new.brep_path != staged.brep_path and not diff.params.get("declined"):
                        measure(label, new, step_for(pool, new.brep_path))
                except Exception as exc:  # noqa: BLE001
                    rec["states"].append({"state": label, "error": f"{type(exc).__name__}: {exc}"[:300]})
            cond = run_stage(pool, staged, "condition")
            if cond.ws.brep_path != staged.brep_path:
                measure("run_stage condition", cond.ws, step_for(pool, cond.ws.brep_path))
    except Exception:
        rec["error"] = traceback.format_exc()[-1500:]
    finally:
        (out / (Path(src).stem + ".b8.json")).write_text(json.dumps(rec, indent=1, default=str))
        pool.close()


if __name__ == "__main__":
    main()
