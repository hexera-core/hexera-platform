"""D17 condition 1: the full verdict, UNCACHED, on one file, dumped field by field, plus how many of each member's
located faces name a live workspace face. Run once with the code before the fix and once after; compare offline.

usage: python vcmp.py --list LIST.json --inputs DIR --out FILE --index I
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--index", type=int, required=True)
    a = ap.parse_args()
    from meshpipeline.cad.repair_v4.sites import _near_fingerprints
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict.assemble import verdict
    from meshpipeline.cad.repair_v4.verdict.cache import VERDICT_CODE_VERSION
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace

    f = json.load(open(a.list))[a.index]
    src = str(Path(a.inputs) / f["sha256"] / Path(f["name"]).name)
    rec: dict = {"index": a.index, "sha256": f["sha256"], "version": VERDICT_CODE_VERSION}
    pool = WorkerPool(1)
    t0 = time.monotonic()
    try:
        work = Path(a.out).parent / "work" / f"{a.index}"
        work.mkdir(parents=True, exist_ok=True)
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(work / "in.brep")}, timeout_s=600)
        assert read.ok and read.value is not None, read.error
        ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
        step = step_for(pool, read.value["brep"])
        v = verdict(pool, step, src, cache=None)
        live = set(ws.ids.values()) | set(_near_fingerprints(pool, ws))
        rec["clean"] = bool(v.clean)
        rec["valid"] = bool(v.valid.ok)
        rec["members"] = []
        for m in v.panel:
            ents = [x.entity for x in m.located if x.entity]
            rec["members"].append({
                "name": m.name, "status": m.status, "error_kind": m.error_kind, "message": m.message,
                "min_sicn": m.min_sicn, "elements": m.elements, "cause": m.cause,
                "algorithms": list(m.algorithms) if m.algorithms else None,
                "located_faces": len(ents), "located_live": sum(e in live for e in ents),
                "located_points": sum(1 for x in m.located if x.xyz is not None)})
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
        rec["tb"] = traceback.format_exc()[-1500:]
    finally:
        pool.close()
    rec["wall_s"] = round(time.monotonic() - t0, 1)
    Path(a.out).write_text(json.dumps(rec, default=str))
    print(json.dumps({"i": a.index, "v": rec["version"], "wall": rec["wall_s"], "err": rec.get("error")}))


if __name__ == "__main__":
    main()
