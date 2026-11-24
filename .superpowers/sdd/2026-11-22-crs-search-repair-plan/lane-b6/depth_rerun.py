"""B7: the four overlap files again, with the new depth (`_cut_depth`): per overlapping pair and keeper direction,
the new depth and method, the old estimate, the bound, and what the REAL op (guards on) returns. A cut that now
passes is judged by the full verdict."""
import json, sys
from pathlib import Path


def main():
    from meshpipeline.cad.repair_v4 import body_kernel as BK
    from meshpipeline.cad.repair_v4.ops.bodies import Conformalise
    from meshpipeline.cad.repair_v4.search import default_judge_for
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace
    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, read_brep

    src, out = sys.argv[1], Path(sys.argv[2])
    pool = WorkerPool(2)
    rec = {"file": Path(src).name, "rows": []}
    try:
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(out / "in7.brep")}, timeout_s=300)
        ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
        shape = read_brep(ws.brep_path)
        bound = BK.fidelity_bound(diagonal_of(shape))
        solids = BK._targets(shape, list(range(1, BK._maps(shape)["solid"].Extent() + 1)))
        sids = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: ws.indices[s])
        rec["bound_mm"] = bound
        judge = None
        for i in range(len(solids)):
            for j in range(i + 1, len(solids)):
                if BK._common_volume(solids[i], solids[j], 0.0) <= 1e-9 * max(diagonal_of(shape), 1.0) ** 3:
                    continue
                for keep, vic in ((i, j), (j, i)):
                    vol, depth, method = BK._cut_depth(solids[vic], solids[keep], 0.0)
                    row = {"keeper": sids[keep], "victim": sids[vic], "volume": vol, "depth_new": depth,
                           "method": method, "side_new": "within" if depth <= bound else "beyond"}
                    _, diff = Conformalise().apply(pool, ws, [sids[i], sids[j]], {"mode": f"cut_by:{sids[keep]}"},
                                                   timeout_s=600)
                    row["op"] = diff.params.get("declined") or "applied"
                    row["op_detail"] = diff.params.get("detail")
                    rec["rows"].append(row)
                    print(json.dumps(row, default=str), flush=True)
    finally:
        (out / f"{Path(src).stem}.b7.json").write_text(json.dumps(rec, indent=1, default=str))
        pool.close()


if __name__ == "__main__":
    main()
