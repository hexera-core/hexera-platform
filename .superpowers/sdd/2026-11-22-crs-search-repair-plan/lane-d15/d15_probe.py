"""Lane D15: what the det sweep proposes per expansion, and what binds first, on the dev_v1r files.

Runs the REAL search_det arm path (arms.run_file: engine form -> staged healer -> confirm -> search with the shared
budget, h_mm auto, workers 1, no model). The only change is an instrumented DetGenerator: its propose() asks the
real sweep with an unlimited k, logs every row it made, and returns the first k exactly as the real one does, so
the search runs unchanged. Read-only: writes JSON to --out.

usage: python d15_probe.py --split SPLIT.json --inputs DIR --out DIR --index I
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True)
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--index", type=int, required=True)
    a = ap.parse_args()
    sys.path.insert(0, "/w/devtools/validation/v4_eval")
    import arms  # type: ignore[import-not-found]
    import evalspec  # type: ignore[import-not-found]
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    f = json.load(open(a.split))["files"][a.index]

    class Entry:
        name, sha256, source = f["name"], f["sha256"], f["source"]

    src = str(Path(a.inputs) / f["sha256"] / Path(f["name"]).name)
    log: list[dict] = []
    kind_re = re.compile(r"\(site (\S+), (\w+)\)$")

    class Probe(S.DetGenerator):
        def propose(self, state, k):  # type: ignore[override]
            t = time.monotonic()
            full = super().propose(state, 10**6)
            rows = []
            for rank, c in enumerate(full):
                m = kind_re.search(c.rationale or "")
                part = c.op == "defeature" and (c.params or {}).get("source") == "recognised" and \
                    "on the part" in (c.rationale or "")
                rows.append({"rank": rank, "op": c.op, "mode": (c.params or {}).get("mode"), "site": m.group(1) if m else None,
                             "kind": "part" if part else (m.group(2) if m else None),
                             "source": (c.params or {}).get("source"), "n_targets": len(c.targets)})
            log.append({"state": state.id, "depth": state.depth, "k": k, "n_full": len(full),
                        "propose_s": round(time.monotonic() - t, 3),
                        "sites": [{"id": s.id, "kind": s.kind, "failing": list(s.meshers_failing or []),
                                   "coarse": bool((s.graph or {}).get("coarse")), "n_ent": len(s.entities)}
                                  for s in state.sites],
                        "rows": rows, "skipped": dict(self.skipped), "notes": list(self.notes)[-20:]})
            return full[:k]

    captured: dict = {}
    real_search = S.search

    def search(*args, **kw):
        r = real_search(*args, **kw)
        captured["trace"] = r.trace.to_dict()
        captured["reason"] = r.reason
        return r

    S.arm_generators = lambda arm, recipes=None, **kw: ([Probe(recipes=recipes)], None)  # type: ignore[assignment]
    S.search = search  # type: ignore[assignment]
    arm = evalspec.ARMS["search_det"]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    pool = WorkerPool(1)
    rec: dict = {"index": a.index, "sha256": f["sha256"], "name": f["name"], "label": f.get("family"),
                 "first_failing_layer": f.get("first_failing_layer"), "budget": arm.budget.__dict__}
    try:
        o = arms.run_file("search_det", Entry, src, pool=pool, engine=arms.RealEngine(), ledger=None, arm=arm,
                          budget=arm.budget, h_mm=arms.H_MM_AUTO, route=None, max_turns=0,
                          job_id=f"d15:{f['sha256'][:12]}", workdir=str(out / "work" / f["sha256"][:12]), workers=1)
        rec["excluded"] = o.excluded
        rec["row"] = o.row
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["tb"] = traceback.format_exc()[-3000:]
    finally:
        pool.close()
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    rec["proposes"] = log
    rec["search_reason"] = captured.get("reason")
    rec["trace"] = captured.get("trace")
    (out / f"{a.index:02d}_{f['sha256'][:12]}.json").write_text(json.dumps(rec, default=str))
    print(json.dumps({"i": a.index, "wall_s": rec["wall_s"], "reason": rec["search_reason"],
                      "expansions": len(log), "excluded": rec.get("excluded"), "error": rec.get("error")}))


if __name__ == "__main__":
    main()
