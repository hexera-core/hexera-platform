#!/usr/bin/env python3
# Responsibility: Census of the mesh-failure track's multi-solid PLC-failure files: for each, do
#                 the bodies merely TOUCH (fragmentable) or INTERPENETRATE (must be declined)?
# Owns: the pair classifier (bbox prefilter, then boolean-common volume vs a threshold), the
#       corpus walk and the JSON report.
# Boundaries: OCP only to read a STEP and measure pairs; it repairs nothing. The kernel
#             measurements are injectable so the classifier is tested without OCP.
"""A pair of solids whose boolean common has volume above `eps_mm3` interpenetrates; one whose
bounding boxes meet but whose common is empty (or dust) touches. Pairs whose boxes are disjoint
are neither. A pair the kernel could not measure is counted in `failed`, never as touching. A file
is `complete` only if every box-meeting pair was measured successfully inside the pair cap and the
deadline; otherwise the counts are a lower bound.

The deadline is checked between pairs only: one runaway `BRepAlgoAPI_Common` cannot be
interrupted and can overrun it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

EPS_MM3 = 1e-6
PAIR_CAP = 40
DEADLINE_S = 120.0


def classify_pairs(
    solids: list,
    *,
    eps_mm3: float = EPS_MM3,
    pair_cap: int = PAIR_CAP,
    deadline_s: float = DEADLINE_S,
    boxes_meet: Callable | None = None,
    common_volume: Callable | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    boxes_meet = boxes_meet or _boxes_meet
    common_volume = common_volume or _common_volume
    start = clock()
    overlapping = touching = failed = checked = 0
    complete = True
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            a, b = solids[i], solids[j]
            if not boxes_meet(a, b):
                continue
            if checked >= pair_cap or clock() - start > deadline_s:
                complete = False
                break
            checked += 1
            volume = common_volume(a, b)
            if volume is None:
                failed += 1
            elif volume > eps_mm3:
                overlapping += 1
            else:
                touching += 1
        if not complete:
            break
    return {
        "overlapping": overlapping,
        "touching": touching,
        "failed": failed,
        "pairs_checked": checked,
        "complete": complete and failed == 0,
    }


_BOX_CACHE: dict[int, object] = {}


def _box(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    key = id(shape)
    if key not in _BOX_CACHE:
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        _BOX_CACHE[key] = box
    return _BOX_CACHE[key]


def _boxes_meet(a, b) -> bool:
    return not _box(a).IsOut(_box(b))


def _common_volume(a, b) -> float | None:
    """Volume of the boolean common, or None when the kernel could not compute it."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    try:
        op = BRepAlgoAPI_Common(a, b)
        if not op.IsDone():
            return None
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(op.Shape(), props)
        return abs(props.Mass())
    except Exception:  # noqa: BLE001 - a kernel failure is a failed pair, never a touch
        return None


def read_solids(path: Path) -> list:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def targets(manifest: dict) -> list[dict]:
    return [f for f in manifest["files"] if (f.get("solids") or 0) > 1 and "PLC" in (f.get("gmsh_error") or "")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--roots", type=Path, nargs="+", required=True)
    ap.add_argument("--json", type=Path, required=True, dest="out")
    ap.add_argument("--eps-mm3", type=float, default=EPS_MM3)
    ap.add_argument("--pair-cap", type=int, default=PAIR_CAP)
    ap.add_argument("--deadline-s", type=float, default=DEADLINE_S)
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mesh_probe import load_manifest, resolve_inputs

    manifest = load_manifest(args.manifest)
    found, missing = resolve_inputs(manifest, args.roots)
    rows = []
    for entry in targets(manifest):
        row = {"name": entry["name"], "solids": entry["solids"]}
        path = found.get(entry["name"])
        if path is None:
            row.update(error="not found under roots", complete=False)
        else:
            _BOX_CACHE.clear()
            try:
                solids = read_solids(path)
                row["solids_read"] = len(solids)
                row.update(
                    classify_pairs(
                        solids,
                        eps_mm3=args.eps_mm3,
                        pair_cap=args.pair_cap,
                        deadline_s=args.deadline_s,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not end the census
                row.update(error=f"{type(exc).__name__}: {exc}", complete=False)
        rows.append(row)
        print(json.dumps(row), flush=True)
    done = [r for r in rows if r["complete"]]
    summary = {
        "files": len(rows),
        "with_overlap": sum(1 for r in rows if r.get("overlapping", 0) > 0),
        "touch_only": sum(1 for r in done if r["overlapping"] == 0),
        "failed_pairs": sum(r.get("failed", 0) for r in rows),
        "incomplete": sum(1 for r in rows if not r["complete"]),
        "missing": missing,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "files": rows}, indent=2) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
