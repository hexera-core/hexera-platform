"""Lane B6: are the four body-overlap declines right? Per file: every overlapping body pair's volume, mean thickness
2V/A, axis-aligned and ORIENTED bbox thinnest extents, containment, the victim's volume share and the fidelity bound;
then cut_by FORCED past its guards (in-process, the real op and history path) and judged by the FULL verdict, so the
deviation layer 3 reports can be set against the geometry."""
import json, sys, time, traceback
from pathlib import Path


def main():
    from OCP.Bnd import Bnd_Box, Bnd_OBB
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.repair_v4 import body_kernel as BK
    from meshpipeline.cad.repair_v4 import kernel_calls
    from meshpipeline.cad.repair_v4.ops.bodies import Conformalise
    from meshpipeline.cad.repair_v4.search import default_judge_for
    from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace
    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, read_brep

    src, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    rec = {"file": Path(src).name}

    class InProc:  # the op's kernel call, run here so its guards can be lifted for the experiment
        def call(self, fn, payload, *, timeout_s):
            t = time.time()
            try:
                return CallResult(True, kernel_calls.REGISTRY[fn](payload), None, "ok", time.time() - t)
            except Exception as exc:  # noqa: BLE001
                return CallResult(False, None, f"{type(exc).__name__}: {exc}", "error", time.time() - t)

    pool = WorkerPool(2)
    try:
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(out / "input.brep")}, timeout_s=300)
        ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
        shape = read_brep(ws.brep_path)
        diag = diagonal_of(shape)
        bound = BK.fidelity_bound(diag)
        solids, ex = [], TopExp_Explorer(shape, TopAbs_SOLID)
        while ex.More():
            solids.append(ex.Current()); ex.Next()
        sids = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))
        rec.update(diagonal_mm=diag, bound_mm=bound, solids=len(solids), solid_ids=sids,
                   volumes=[BK._volume(s) for s in solids])
        pairs = []
        for i in range(len(solids)):
            for j in range(i + 1, len(solids)):
                op = BRepAlgoAPI_Common(); op.SetArguments(BK._list([solids[i]])); op.SetTools(BK._list([solids[j]]))
                op.Build()
                c = op.Shape()
                if not op.IsDone() or not BK._solid_count(c):
                    continue
                v, a = BK._volume(c), BK._area(c)
                if v <= 1e-9 * max(diag, 1.0) ** 3:
                    continue
                box = Bnd_Box(); BRepBndLib.Add_s(c, box)
                x0, y0, z0, x1, y1, z1 = box.Get()
                obb = Bnd_OBB(); BRepBndLib.AddOBB_s(c, obb, False, True, False)
                obb_min = 2 * min(obb.XHSize(), obb.YHSize(), obb.ZHSize())
                vi, vj = BK._volume(solids[i]), BK._volume(solids[j])
                pairs.append({"pair": [sids[i] if i < len(sids) else i, sids[j] if j < len(sids) else j],
                              "overlap_volume": v, "overlap_area": a, "mean_thickness_2V_over_A": 2 * v / a,
                              "aabb_min_extent": min(x1 - x0, y1 - y0, z1 - z0), "obb_min_extent": obb_min,
                              "kernel_depth": max(2 * v / a, min(x1 - x0, y1 - y0, z1 - z0)),
                              "share_of_body_i": v / vi, "share_of_body_j": v / vj,
                              "contained": max(v / vi, v / vj) > 0.98})
        rec["overlapping_pairs"] = pairs
        print(json.dumps(rec, default=str), flush=True)

        # the experiment: cut_by FORCED (guards lifted in THIS process only), then the real full verdict
        BK.fidelity_bound = lambda d: 1e9
        BK.MAX_VOLUME_CHANGE = 1.0
        judge = default_judge_for(pool, ws)
        v_in = judge.full(pool, ws, src)
        rec["input_panel"] = {m.name: m.status for m in v_in.panel}
        forced = []
        for p in pairs[:3]:
            for keeper in p["pair"]:
                if not isinstance(keeper, str):
                    continue
                entry = {"pair": p["pair"], "keeper": keeper}
                try:
                    new, diff = Conformalise().apply(InProc(), ws, list(p["pair"]), {"mode": f"cut_by:{keeper}"},
                                                     timeout_s=600)
                    entry["declined"] = diff.params.get("declined")
                    entry["declared_removed"] = len(diff.declared_removed)
                    entry["modified"] = len(diff.modified)
                    if new is not ws:
                        v = judge.full(pool, new, src)
                        f = v.faithful
                        entry.update(panel={m.name: m.status for m in v.panel}, clean=v.clean, valid=v.valid.ok,
                                     valid_hard=[h.code for h in v.valid.hard],
                                     faithful=None if f is None else {
                                         "ok": f.ok, "reasons": list(f.reasons), "max_dev_mm": f.max_dev_mm,
                                         "bound_mm": f.bound_mm, "area_ratio": f.area_ratio,
                                         "volume_ratio": f.volume_ratio,
                                         "declared_removed": len(f.declared_removed)})
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"{type(exc).__name__}: {exc}"[:400]
                forced.append(entry)
                print(json.dumps(entry, default=str), flush=True)
        rec["forced_cut_by"] = forced
    except Exception:
        rec["error"] = traceback.format_exc()[-2000:]
    finally:
        (out / f"{Path(src).stem}.json").write_text(json.dumps(rec, indent=1, default=str))
        pool.close()


if __name__ == "__main__":
    main()
