"""Files that arrive already over an absolute cap, so no operation on them can ever be kept.

`caps.cumulative_refusal` refuses any result whose max tolerance exceeds
`CAD_REPAIR_MAX_TOLERANCE_MM`, measured on the RESULT, not as a change from the upload. A part that
arrives at 0.18 mm (as1_motor.step) therefore rolls back every operation for a reason that says
nothing about the operation. Counting those as negatives would teach the memory that
`fix_small_edges` fails on small edges, when it was never allowed to succeed.

Usage:
    PYTHONPATH=src:. .venv/bin/python -m labs.defect_cad_rag.precap /tmp/dcr-sweep/records.jsonl \
        ROOT... --json /tmp/dcr-sweep/precapped.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", type=Path)
    ap.add_argument("roots", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args(argv)

    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import measure

    names = {json.loads(line)["file"] for line in args.records.read_text().splitlines()
             if '"kind": "defect"' in line}
    paths = {p.name: p for root in args.roots for p in root.expanduser().rglob("*")
             if p.name in names}
    out = {}
    for name in sorted(names):
        try:
            m = measure(_read_shape(paths[name]))
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        reasons = []
        if m.max_tolerance_mm > rcfg.CAD_REPAIR_MAX_TOLERANCE_MM:
            reasons.append(f"tolerance {m.max_tolerance_mm:.4g} mm > "
                           f"{rcfg.CAD_REPAIR_MAX_TOLERANCE_MM} mm cap")
        if not math.isfinite(m.diagonal_mm) or m.diagonal_mm > 1e9:
            reasons.append(f"diagonal {m.diagonal_mm:.3g} mm (unbounded geometry)")
        if reasons:
            out[name] = {"reasons": reasons, **m.to_dict()}
    args.json.write_text(json.dumps(out, indent=2))
    print(f"{len(out)} of {len(names)} files precapped -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
