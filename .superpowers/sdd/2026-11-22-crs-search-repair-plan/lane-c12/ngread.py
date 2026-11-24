import subprocess, sys
NG = "/opt/netgen-env/bin/python"
PROBE = r'''
import sys
from netgen.occ import OCCGeometry
g = OCCGeometry(sys.argv[1]); s = g.shape
print("type", s.type, "solids", len(list(s.solids)), "shells", len(list(s.shells)), "faces", len(list(s.faces)))
try:
    print("bbox", s.bounding_box)
except Exception as e:
    print("bbox ERR", type(e).__name__, e)
'''
def main():
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.verdict import netgen_member as N
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    step = sys.argv[1]
    pool = WorkerPool(1)
    ef = S.engine_form(pool, step)
    for label, path in (("original", step), ("engine_form", ef)):
        print("==", label)
        print(subprocess.run([NG, "-c", PROBE, path], capture_output=True, text=True, timeout=600).stdout[-600:])
        r = N.NetgenOCC().run(pool, path, timeout_s=600)
        print("member:", r.status, r.error_kind, (r.message or "")[:300])
    pool.close()
if __name__ == "__main__":
    main()
