"""Lane C11 on the TIP (A11.2's contact sites): plan steps, then what the det sweep proposes at the resulting state.
After the last step: every site with its measured clashes (touching=True is A11.2's contact), the det proposals in
the sweep's own order, and the first N proposals applied as branches and judged with the full verdict.
Usage: python chain2.py <step> <out.json> <plan key in plans2.json>"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(__file__))
from chain import _solids  # noqa: E402
from diag import T, _members  # noqa: E402

from meshpipeline.cad.repair_v4 import search as S  # noqa: E402
from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.sites import find_sites  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.assemble import score, verdict  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

N_BRANCHES = 6


def judge(pool, ws, step, h, parent=None):
    t0 = time.time()
    v = S._judged(verdict, pool, ws, step)
    sites = find_sites(pool, ws, v, h_mm=h)
    summary = {
        "seconds": round(time.time() - t0, 1), "valid": v.valid.ok, "clean": v.clean,
        "hard": [{"code": f.code, "entities": f.entities[:8]} for f in v.valid.hard],
        "panel_pass": sum(m.status == "pass" for m in v.panel), "panel": _members(v, ws, parent),
        "faithful": None if v.faithful is None else {"ok": v.faithful.ok, "max_dev_mm": v.faithful.max_dev_mm},
        "sites": [{"id": s.id, "kind": s.kind, "entities": s.entities[:12], "n": len(s.entities),
                   "meshers": s.meshers_failing,
                   "clashes": [{k: c.get(k) for k in ("a", "b", "dist", "touching", "area_a", "area_b")}
                               for c in (s.graph or {}).get("clashes") or []][:8]} for s in sites],
    }
    return summary, v, sites


def main(step: str, out: str, plan: list) -> None:
    rec: dict = {"step": step, "plan": plan, "steps": []}
    pool = WorkerPool(2)
    try:
        work = tempfile.mkdtemp(prefix="c11t-")
        read = pool.call("read_step_xcaf", {"path": step, "brep_out": os.path.join(work, "in.brep")}, timeout_s=T)
        ws = Workspace.from_brep(pool, read.value["brep"], read.value["labels"])
        h = S.h_mm_for(pool, ws)
        summ, v, sites = judge(pool, ws, step, h)
        rec["input"] = summ
        for op, params, targets in plan:
            bodies = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))
            tg = bodies if targets == "all" else targets
            row: dict = {"op": op, "params": params, "targets": tg}
            new, diff = REGISTRY[op].apply(pool, ws, tg, params, timeout_s=T)
            row.update(declined=diff.params.get("declined"), detail=str(diff.params.get("detail") or "")[:300])
            rec["steps"].append(row)
            if new is ws or row["declined"]:
                json.dump(rec, open(out, "w"), indent=1, default=str)
                return
            row["solids"] = _solids(new.brep_path)
            row["after"], v, sites = judge(pool, new, step, h, parent=ws)
            ws = new
            json.dump(rec, open(out, "w"), indent=1, default=str)
        gen = S.DetGenerator()
        gen.bind_pool(pool)
        state = S.SearchState(id="X", ws=ws, verdict=v, score=score(v), sites=sites, depth=len(plan), parent=None,
                              via=None)
        props = gen.propose(state, 64)
        rec["proposals"] = [{"op": c.op, "targets": c.targets[:10], "params": c.params, "why": c.rationale[:160]}
                            for c in props]
        rec["sweep_notes"] = list(gen.notes)[:20]
        rec["sweep_skipped"] = dict(gen.skipped)
        rec["branches"] = []
        for c in props[:N_BRANCHES]:
            b: dict = {"op": c.op, "targets": c.targets[:10], "params": c.params}
            try:
                nb, d = REGISTRY[c.op].apply(pool, ws, c.targets, c.params, timeout_s=T)
                b["declined"] = d.params.get("declined")
                b["detail"] = str(d.params.get("detail") or "")[:200]
                if nb is not ws and not b["declined"]:
                    b["after"], _, _ = judge(pool, nb, step, h, parent=ws)
            except Exception as exc:  # noqa: BLE001
                b["error"] = f"{type(exc).__name__}: {exc}"[:400]
                b["tb"] = traceback.format_exc()[-800:]
            rec["branches"].append(b)
            json.dump(rec, open(out, "w"), indent=1, default=str)
    finally:
        pool.close()
    json.dump(rec, open(out, "w"), indent=1, default=str)


if __name__ == "__main__":
    plans = json.load(open(os.path.join(os.path.dirname(__file__), "plans2.json")))
    main(sys.argv[1], sys.argv[2], plans[sys.argv[3]])
