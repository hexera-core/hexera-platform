"""On REAL dev parts: is sha256(STEP export) stable, and does it survive a STEP round trip that breaks the
BinTools digest? Each argument is a BREP path. Prints one row per part."""
import sys
import tempfile
from pathlib import Path

from meshpipeline.cad.repair_v4 import stages
from meshpipeline.cad.repair_v4.verdict.cache import file_sha256
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool


def main(paths: list[str]) -> None:
    work = Path(tempfile.mkdtemp())
    pool = WorkerPool(1)
    from meshpipeline.cad.repair_v4.verdict.shape_digest import digest
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep
    print(f"{'part':14} {'export twice':13} {'step survives rt':17} {'bintools survives rt':20} faces")
    try:
        for i, brep in enumerate(paths):
            name = Path(brep).stem[:12]
            try:
                stages._STEP_FOR.clear()
                s1 = stages.step_for(pool, brep)
                stages._STEP_FOR.clear()
                s2 = stages.step_for(pool, brep)
                twice = file_sha256(s1) == file_sha256(s2)

                r = pool.call("read_step_xcaf", {"path": s1, "brep_out": str(work / f"rt{i}.brep")}, timeout_s=600)
                if not r.ok:
                    print(f"{name:14} {'SAME' if twice else 'DIFFERENT':13} round-trip read failed: {r.status}")
                    continue
                rt = r.value["brep"]
                stages._STEP_FOR.clear()
                s3 = stages.step_for(pool, rt)
                step_rt = file_sha256(s1) == file_sha256(s3)
                shape = read_brep(brep)
                bin_rt = digest(shape) == digest(read_brep(rt))
                from OCP.TopAbs import TopAbs_FACE
                from OCP.TopExp import TopExp
                from OCP.TopTools import TopTools_IndexedMapOfShape
                m = TopTools_IndexedMapOfShape()
                TopExp.MapShapes_s(shape, TopAbs_FACE, m)
                print(f"{name:14} {'SAME' if twice else 'DIFFERENT':13} "
                      f"{'SAME' if step_rt else 'DIFFERENT':17} {'SAME' if bin_rt else 'DIFFERENT':20} {m.Extent()}")
            except Exception as exc:  # noqa: BLE001
                print(f"{name:14} error: {type(exc).__name__}: {exc}")
    finally:
        pool.close()


if __name__ == "__main__":
    main(sys.argv[1:])
