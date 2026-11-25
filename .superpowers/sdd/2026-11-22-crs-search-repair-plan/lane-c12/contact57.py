"""0057 after cut_by:B14 on the tip: the rows A12's `_contact_rows` WOULD build from the self_overlap site's touching
clashes (it is only called for proximity sites), each applied and fully judged; plus the self_overlap rows aimed at
the failing pair F18/F20, labelled apart."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from chain2 import judge
from diag import T


def main(step, out):
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace
    pool = WorkerPool(2); rec = {"branches": []}
    try:
        w = tempfile.mkdtemp()
        r = pool.call("read_step_xcaf", {"path": step, "brep_out": os.path.join(w, "in.brep")}, timeout_s=T)
        ws0 = Workspace.from_brep(pool, r.value["brep"], r.value["labels"]); h = S.h_mm_for(pool, ws0)
        ws, _ = REGISTRY["conformalise"].apply(pool, ws0, ["B10", "B13", "B14"], {"mode": "cut_by:B14"}, timeout_s=T)
        rec["cut"], v, sites = judge(pool, ws, step, h, parent=ws0)
        contacts = [dict(c) for s in sites for c in (s.graph or {}).get("clashes") or [] if c.get("touching")]
        rows = [("contact", *x) for x in S.DetGenerator._contact_rows(contacts)]
        rows += [("self_overlap", "remove_duplicate_faces", ["F18", "F20"], {"pairs": [["F18", "F20"]]}, "pair"),
                 ("self_overlap", "unify_same_domain", ["F18", "F20"], {}, "pair")]
        for fam, op, tg, params, why in rows:
            b = {"family": fam, "op": op, "targets": tg, "params": params, "why": why}
            try:
                nb, d = REGISTRY[op].apply(pool, ws, tg, params, timeout_s=T)
                b["declined"] = d.params.get("declined"); b["detail"] = str(d.params.get("detail") or "")[:200]
                if nb is not ws and not b["declined"]:
                    b["after"], _, _ = judge(pool, nb, step, h, parent=ws)
            except Exception as exc:  # noqa: BLE001
                b["error"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["branches"].append(b)
            json.dump(rec, open(out, "w"), indent=1, default=str)
    finally:
        pool.close()
        json.dump(rec, open(out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
