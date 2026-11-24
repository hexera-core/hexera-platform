"""B15 step 1: can a durable verdict cache EVER hit across processes and arms?

The cache keys on the sha256 of the engine-form STEP (`verdict.assemble`: `file_sha256(step)`), which
`stages.step_for` writes per workspace. `stages._deterministic` rewrites the header timestamp for exactly this
reason. This probe asks whether that is ENOUGH: it exports the SAME geometry several ways and compares the bytes.

  python step_bytes_probe.py write <brep>          # build one shape, write it as BREP
  python step_bytes_probe.py export <brep> <tag>   # step_for(brep) -> sha256 + the FILE_NAME header, as JSON

Run `export` twice in SEPARATE processes on one BREP (that is what two arms of one file do), and once on a byte-equal
COPY at another path (that is what two workspaces with the same geometry do). Equal shas mean the cache can hit.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

sys.path.insert(0, "/work/src")


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main() -> int:
    mode, brep = sys.argv[1], sys.argv[2]
    from meshpipeline.cad.repair_v4 import stages, ws_kernel
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    if mode == "write":
        from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        box = BRepPrimAPI_MakeBox(100.0, 60.0, 40.0).Shape()
        ch = BRepFilletAPI_MakeChamfer(box)
        exp = TopExp_Explorer(box, TopAbs_EDGE)
        for _ in range(3):
            ch.Add(2.0, TopoDS.Edge_s(exp.Current()))
            exp.Next()
        ws_kernel.write_brep(ch.Shape(), brep)
        print(json.dumps({"brep": brep, "brep_sha256": _sha(brep)}))
        return 0

    pool = WorkerPool(1)
    try:
        step = stages.step_for(pool, brep)
        with open(step, "rb") as fh:
            head = fh.read(4096).decode("latin-1")
        names = re.findall(r"FILE_NAME\([^)]*\)", head, re.S)
        print(json.dumps({"tag": sys.argv[3], "brep": brep, "brep_sha256": _sha(brep), "step_path": step,
                          "step_sha256": _sha(step), "file_name": " ".join(names[0].split()) if names else None,
                          "bytes": len(open(step, "rb").read())}, sort_keys=True))
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
