"""Lane C13: which op inflates a state's diagonal. Runs the ENGINE (repair_v4, ARM=search_det, the eval's shared budget:
600 s, 60 ops) on one STEP with every registry op's `apply` wrapped: each applied candidate is logged with its op,
params, targets, the parent's and the result's diagonal (the kernel's `ws_diagonal`), and any result over INFLATE x
the input's diagonal has its BREP and its parent's kept for a look.
Usage: python inflate.py <step> <outdir>"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

INFLATE = 2.0


def main(step: str, outdir: str) -> None:
    import meshpipeline.cad.repair_v4 as v4
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    from meshpipeline.cad.repair_v4.search import Budget

    os.makedirs(outdir, exist_ok=True)
    log: list[dict] = []
    base: dict = {}

    def diag(pool, path):
        r = pool.call("ws_diagonal", {"brep_path": path}, timeout_s=120)
        return float(r.value["diag_mm"]) if r.ok and r.value is not None else None

    def wrap(name, op):
        real = op.apply

        def apply(pool, ws, targets, params, *, timeout_s):
            new, d = real(pool, ws, targets, params, timeout_s=timeout_s)
            if "input" not in base:
                base["input"] = diag(pool, ws.original)
            row = {"n": len(log), "op": name, "targets": list(targets)[:12], "n_targets": len(targets),
                   "params": params, "declined": d.params.get("declined"),
                   "depth": len(ws.history), "lineage": [h.op for h in ws.history]}
            if new is not ws and not row["declined"]:
                row["parent_diag"], row["diag"] = diag(pool, ws.brep_path), diag(pool, new.brep_path)
                if row["diag"] and base["input"] and row["diag"] > INFLATE * base["input"]:
                    k = len(log)
                    shutil.copy(new.brep_path, os.path.join(outdir, f"inflated_{k}_{name}.brep"))
                    shutil.copy(ws.brep_path, os.path.join(outdir, f"parent_{k}.brep"))
                    row["kept"] = True
                    row["diff"] = {"modified": {a: b for a, b in list(d.modified.items())[:20]},
                                   "generated": {a: b for a, b in list(d.generated.items())[:20]},
                                   "removed": d.removed[:20], "declared_added": d.declared_added[:20],
                                   "measured": d.params.get("measured")}
            log.append(row)
            json.dump({"input_diag": base.get("input"), "applied": log}, open(os.path.join(outdir, "log.json"), "w"),
                      indent=1, default=str)
            return new, d

        op.apply = apply

    for name, op in REGISTRY.items():
        wrap(name, op)
    v4.ARM = "search_det"
    t0 = time.monotonic()
    res = v4.repair_v4(step, os.path.join(outdir, "out.step"), budget=Budget(600.0, 60, 0, 0.0),
                       deadline_s=time.monotonic() + 1500)
    json.dump({"input_diag": base.get("input"), "applied": log, "result": str(res.verdict_summary)[:2000],
               "seconds": time.monotonic() - t0}, open(os.path.join(outdir, "log.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
