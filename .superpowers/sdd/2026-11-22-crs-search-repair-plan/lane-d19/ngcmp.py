import json, sys, time
from pathlib import Path
if __name__ == "__main__":
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict.netgen_member import NetgenOCC
    from meshpipeline.cad.repair_v4.verdict.cache import VERDICT_CODE_VERSION
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    i, out = int(sys.argv[1]), sys.argv[2]
    f = json.load(open('/c/list.json'))[i]
    src = str(Path('/c/in') / f['sha256'] / Path(f['name']).name)
    pool = WorkerPool(1)
    try:
        rd = pool.call("read_step_xcaf", {"path": src, "brep_out": f"/tmp/ng{i}.brep"}, timeout_s=900)
        step = step_for(pool, rd.value["brep"])
        t = time.monotonic()
        r = NetgenOCC().run(pool, step, timeout_s=600)
        rec = {"i": i, "sha": f['sha256'][:10], "v": VERDICT_CODE_VERSION, "status": r.status, "error_kind": r.error_kind,
               "message": r.message, "min_sicn": r.min_sicn, "elements": r.elements, "cause": r.cause,
               "located": [(x.entity, x.mesher_tag) for x in r.located], "s": round(time.monotonic() - t, 1)}
    finally:
        pool.close()
    Path(out).write_text(json.dumps(rec)); print(json.dumps({k: rec[k] for k in ('i', 'v', 'status', 'error_kind', 's')}))
