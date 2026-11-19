# Responsibility: Measure how much one part's quantities of interest move with the mesh alone - several sizes and several realizations per size - so a simplification's error can be read against a stated mesh noise rather than against one mesh.
# Boundaries: a developer tool run on the bench VM, beside harness.py; nothing in the product imports it.
# Collaborates with: devtools/simplify/harness.py (mesh_and_solve, the load case).
"""HOW MUCH DOES THE MESH ALONE MOVE THE ANSWER?

    python devtools/simplify/converge.py part.step --out results/part/converge [--sizes 20,30,45,60]

For each size (diagonal / SIZE), the part is meshed at five nearby sizes (x0.96 ... x1.04), which gmsh
turns into five different meshes of the same geometry. Reported per size: the median and spread of
compliance, max displacement and the strain energy. A defeaturing error smaller than the spread at
the size it was measured at is not a measurement.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402  (a sibling script, not a package)

JITTER = (0.96, 0.98, 1.0, 1.02, 1.04)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sizes", default="20,30,45,60")
    ap.add_argument("--jobs", type=int, default=16)
    a = ap.parse_args(argv)
    out = Path(a.out)
    (out / "geom").mkdir(parents=True, exist_ok=True)
    shape, diag, lc = H.prepare(a.step)
    brep = out / "geom" / "original.brep"
    H.write_brep(shape, brep)
    jobs = []
    for size in (float(s) for s in a.sizes.split(",")):
        for j in JITTER:
            name = f"s{size:g}_j{j:g}"
            jobs.append((name, str(brep), str(out / "work" / name), asdict(lc), diag / size * j, {"none": []}, False))
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = dict(ex.map(H._job, jobs))
    table = {}
    for size in (float(s) for s in a.sizes.split(",")):
        rows = [res[f"s{size:g}_j{j:g}"] for j in JITTER]
        ok = [r for r in rows if r["ok"]]
        if not ok:
            table[size] = {"ok": 0}
            continue
        c = [r["qoi"]["compliance"] for r in ok]
        med = statistics.median(c)
        table[size] = {"ok": len(ok), "elements": statistics.median(r["elements"] for r in ok),
                       "compliance_median": med, "compliance_spread": (max(c) - min(c)) / med,
                       "compliance": c, "solve_s": statistics.median(r["solve_s"] for r in ok)}
        print(f"size {size:4g}: {len(ok)}/5 ok, {table[size]['elements']:>8.0f} el, compliance {med:.6g}, "
              f"spread {table[size]['compliance_spread']:.3%}, each " + " ".join(f"{(x - med) / med:+.3%}" for x in c))
    (out / "converge.json").write_text(json.dumps({"diag": diag, "table": table, "runs": res}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
