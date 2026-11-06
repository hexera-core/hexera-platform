"""Metamorphic stability: the same part, moved or round-tripped, must describe the same defects.

Three variants of each file, each compared with the original:

  rigid      rotated about an oblique axis and translated (an exact copy, no re-approximation);
  step       written to STEP and read back (what every upload actually goes through);
  rigid+step both.

For each variant it reports whether the LOCALISER finds the same defects (counts per code), whether
the box-based fingerprints survive, and - for defects matched by entity index and intrinsic size -
how far the descriptor moved and whether the moved defect still retrieves itself among the file's
same-code defects. A descriptor that fails this has no business in a retrieval store.

Usage:
    PYTHONPATH=src:. .venv/bin/python -m labs.defect_cad_rag.stability FILE... --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from collections import Counter
from pathlib import Path


def _rigid(shape):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    rot = gp_Trsf()
    rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 2, 3)), 0.7)
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(13.0, -7.0, 29.0))
    return BRepBuilderAPI_Transform(shape, move.Multiplied(rot), True).Shape()


def _step(shape):
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.conservative import _write_step

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rt.step"
        _write_step(shape, path)
        return _read_shape(path)


def _described(shape) -> list[dict]:
    from labs.defect_cad_rag.patch import _Topology, describe, isolate_patch
    from meshpipeline.cad.repair.localize import localize_scan

    found, _ = localize_scan(shape)
    topo = _Topology(shape)
    out = []
    for d in found:
        try:
            patch = isolate_patch(shape, d, topology=topo)
            vec = describe(patch, topology=topo)
        except Exception:  # noqa: BLE001
            patch, vec = None, None
        out.append({"code": d.code.value, "key": (d.code.value, d.entity_type, d.entity_index),
                    "fingerprint": d.fingerprint, "scale": patch.scale_mm if patch else None,
                    "vector": vec})
    return out


def compare(original: list[dict], variant: list[dict]) -> dict:
    by_key = {d["key"]: d for d in variant}
    matched = []
    for d in original:
        v = by_key.get(d["key"])
        if v is None or d["scale"] is None or v["scale"] is None:
            continue
        if abs(v["scale"] - d["scale"]) > 1e-6 * max(d["scale"], 1e-9) + 1e-9:
            continue  # same index, different entity: the map was renumbered
        matched.append((d, v))
    drift = [math.dist(d["vector"], v["vector"]) for d, v in matched if d["vector"] and v["vector"]]
    self_hits = 0
    for d, v in matched:
        if not (d["vector"] and v["vector"]):
            continue
        same = [o for o in original if o["code"] == d["code"] and o["vector"]]
        best = min(math.dist(v["vector"], o["vector"]) for o in same)
        self_hits += math.dist(v["vector"], d["vector"]) <= best + 1e-9
    fp_orig = {d["fingerprint"] for d in original if d["fingerprint"]}
    fp_var = {d["fingerprint"] for d in variant if d["fingerprint"]}
    return {
        "counts_original": dict(Counter(d["code"] for d in original)),
        "counts_variant": dict(Counter(d["code"] for d in variant)),
        "matched": len(matched), "original": len(original),
        "fingerprints_surviving": len(fp_orig & fp_var), "fingerprints": len(fp_orig),
        "drift_max": round(max(drift), 6) if drift else None,
        "drift_mean": round(sum(drift) / len(drift), 6) if drift else None,
        "self_retrieval": round(self_hits / len(drift), 4) if drift else None,
    }


def run(path: Path) -> dict:
    from meshpipeline.cad.repair.brep import _read_shape

    shape = _read_shape(path)
    original = _described(shape)
    moved = _rigid(shape)
    out = {"file": path.name}
    for name, variant in (("rigid", moved), ("step", _step(shape)), ("rigid+step", _step(moved))):
        try:
            out[name] = compare(original, _described(variant))
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    results = []
    for f in args.files:
        try:
            results.append(run(f))
        except Exception as exc:  # noqa: BLE001
            results.append({"file": f.name, "error": f"{type(exc).__name__}: {exc}"})
        print(json.dumps(results[-1]), flush=True)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
