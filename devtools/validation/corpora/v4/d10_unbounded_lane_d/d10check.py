"""D10 native check: the re-probe row and the arms' input verdict, per file, with timings."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/w/devtools/validation/v4_eval")
import arms
import reprobe as rp

from meshpipeline.cad.repair_v4.search import default_judge
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool


def main():
    inputs, out = Path(sys.argv[1]), Path(sys.argv[2])
    pool = WorkerPool(1, start_timeout_s=600)
    recs = []
    class E:
        def __init__(self, sha, name): self.sha256, self.name, self.source, self.group = sha, name, "abc_fail", None
    for d in sorted(inputs.iterdir()):
        step = str(next(d.iterdir()))
        t0 = time.monotonic()
        rows = rp.reprobe([E(d.name, Path(step).name)], tier="full", pool=pool, resolve=lambda e, s=step: s)
        t1 = time.monotonic()
        with tempfile.TemporaryDirectory() as wd:
            ws = arms.RealEngine().open_workspace(pool, step, wd)
            v = default_judge(h_mm=1.0).full(pool, ws, step)
        t2 = time.monotonic()
        rec = {"sha": d.name[:8], "reprobe_s": round(t1 - t0, 1), "arm_input_s": round(t2 - t1, 1),
               "reprobe": None if not rows else {"layer": rows[0].first_failing_layer, "family": rows[0].family,
                                                 "summary": rows[0].verdict_summary},
               "arm_input": rp.summarise(v)}
        recs.append(rec)
        print(json.dumps({k: rec[k] for k in ("sha", "reprobe_s", "arm_input_s")}), rec["reprobe"] and rec["reprobe"]["family"], flush=True)
    pool.close()
    out.write_text(json.dumps(recs, indent=1, sort_keys=True))


if __name__ == '__main__':
    main()
