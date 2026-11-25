"""Every overlap input's face fingerprints on the workspace grid (what the rows' located entities are on): a located
face whose fingerprint is in its input's set is a face that was ALREADY there (OLD); one that is not was made or
changed by an op (NEW). Usage: python fp.py <inputs dir> <out.json>"""
import glob, json, os, sys, tempfile


def main(root, out):
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace
    pool = WorkerPool(2)
    res = {}
    try:
        for path in sorted(glob.glob(os.path.join(root, "*", "*"))):
            sha = path.split(os.sep)[-2]
            try:
                w = tempfile.mkdtemp()
                r = pool.call("read_step_xcaf", {"path": path, "brep_out": os.path.join(w, "in.brep")}, timeout_s=600)
                ws = Workspace.from_brep(pool, r.value["brep"], r.value["labels"], timeout_s=600)
                res[sha] = {"quantum": ws.quantum, "faces": {fp: sid for sid, fp in ws.ids.items() if sid[0] == "F"},
                            "bodies": sum(1 for s in ws.ids if s[0] == "B")}
            except Exception as exc:  # noqa: BLE001
                res[sha] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            print(sha[:12], res[sha].get("bodies"), res[sha].get("error", ""), flush=True)
            json.dump(res, open(out, "w"))
    finally:
        pool.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
