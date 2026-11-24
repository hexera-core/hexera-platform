"""B10: the staged healer on one file under three cheap tiers, each followed by the FULL-panel confirmation.
  pre_b8       layer 1 + gmsh_default                      (the healer before B8)
  b8_static    layer 1 + gmsh_default + gmsh_prod          (0fa39fd5, default_members("cheap"))
  b8b_adaptive b8_static + every member the INPUT fails    (assemble.adaptive_cheap_members)
Per tier: kept / rolled_back / skipped / inconclusive per stage, the confirmation's fell_back_to / dropped, the final
full verdict, wall seconds. Read-only: nothing is written but this JSON."""
import json, sys, time, traceback
from pathlib import Path


def main():
    from meshpipeline.cad.repair_v4.search import default_judge_for
    from meshpipeline.cad.repair_v4.stages import confirm_staged, run_staged
    from meshpipeline.cad.repair_v4.verdict import gmsh_members as GM
    from meshpipeline.cad.repair_v4.verdict.assemble import adaptive_cheap_members
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace

    src, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    rec = {"file": src, "tiers": {}}
    pool = WorkerPool(2)
    try:
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(out / "in.brep")}, timeout_s=300)
        if not read.ok:
            raise RuntimeError(f"read failed: {read.error}")
        ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
        judge = default_judge_for(pool, ws)
        t = time.time()
        v_in = judge.full(pool, ws, src)
        rec["input"] = {"panel": {m.name: m.status for m in v_in.panel}, "valid": bool(v_in.valid.ok),
                        "clean": bool(v_in.clean), "inconclusive": bool(getattr(v_in, "inconclusive", False)),
                        "seconds": round(time.time() - t, 1)}
        tiers = {"pre_b8": [GM.GmshDefault()], "b8_static": None, "b8b_adaptive": adaptive_cheap_members(v_in)}
        for name, members in tiers.items():
            t = time.time()
            try:
                res = run_staged(pool, ws, cheap_members=members)
                t_staged = time.time() - t
                conf = confirm_staged(pool, ws, res, full=lambda w: judge.full(pool, w, src), input_verdict=v_in)
                v = conf.verdict if conf.verdict is not None else v_in
                rec["tiers"][name] = {
                    "members": None if members is None else [m.name for m in members],
                    "kept": res.kept, "rolled_back": res.rolled_back, "skipped": sorted(res.skipped),
                    "inconclusive": res.inconclusive, "standing": [n for n, _ in res.standing],
                    "fell_back_to": conf.fell_back_to, "dropped": conf.dropped, "checked": conf.checked,
                    "final_panel": {m.name: m.status for m in v.panel}, "final_clean": bool(v.clean),
                    "changed": res.ws.brep_path != ws.brep_path,
                    "staged_s": round(t_staged, 1), "total_s": round(time.time() - t, 1)}
            except Exception as exc:  # noqa: BLE001
                rec["tiers"][name] = {"error": f"{type(exc).__name__}: {exc}"[:400]}
            print(json.dumps({"file": Path(src).name, "tier": name, **{k: rec["tiers"][name].get(k) for k in
                              ("kept", "rolled_back", "fell_back_to", "dropped", "final_clean", "error")}}),
                  flush=True)
    except Exception:
        rec["error"] = traceback.format_exc()[-1500:]
    finally:
        (out / "b10.json").write_text(json.dumps(rec, indent=1, default=str))
        pool.close()


if __name__ == "__main__":
    main()
