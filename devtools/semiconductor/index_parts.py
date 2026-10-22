# Responsibility: Measure every test part in cad/semiconductor and write the registry.
# Boundaries: measures and records; it meshes nothing and chooses no resolution.
"""BUILD THE PART REGISTRY.

    python devtools/semiconductor/index_parts.py [--cad cad/semiconductor]

Run it once after new CAD lands. It pairs every `*_fluid.stl` with its `*_solid.stl`, measures the
coolant's volume, its bounding box and HOW MANY SEPARATE PASSAGES IT HAS, pulls the narrowest
feature out of whatever `manifest*.json` sits beside the STLs, and writes `parts.json`.

The passage count is the reason this is a measurement and not a table someone maintains. It is what
catches a mesh too coarse to hold a channel together - the failure `checkMesh` calls "Mesh OK" and
no solver will touch - so it has to describe the STL as it is now, not as it was when a note was
written. 100 channels that never touch must come back as 100.

A part with no declared narrowest feature is REFUSED rather than guessed at. The cell size is
derived from that number, and a guess there is a guess about the whole run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import parts as P

#: Where the registry and the STLs live, relative to the repository root.
DEFAULT_CAD = Path("cad") / "semiconductor"


def declared_features(cad: Path) -> dict[str, dict]:
    """What the CAD manifests say, keyed by part name.

    Every `manifest*.json` beside the STLs is read, because the parts arrived in two batches with
    a manifest each and a third batch will arrive the same way. Later files win on a clash, and a
    clash is reported rather than resolved quietly.
    """
    out: dict[str, dict] = {}
    for path in sorted(cad.glob("manifest*.json")):
        try:
            entries = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            print(f"  {path.name}: not readable as JSON ({exc}); skipped", file=sys.stderr)
            continue
        for e in entries if isinstance(entries, list) else [entries]:
            name = e.get("name")
            if not name:
                continue
            if name in out and out[name].get("_from") != path.name:
                print(f"  note: {name} appears in both {out[name]['_from']} and {path.name}; "
                      f"{path.name} wins", file=sys.stderr)
            out[name] = {**e, "_from": path.name}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure the test parts and write parts.json.")
    ap.add_argument("--cad", type=Path, default=DEFAULT_CAD,
                    help=f"directory holding the STLs, in METRES (default {DEFAULT_CAD})")
    a = ap.parse_args(argv)
    cad: Path = a.cad
    if not cad.is_dir():
        print(f"no such directory: {cad}", file=sys.stderr)
        return 2

    manifests = declared_features(cad)
    measured: list[P.Part] = []
    refused: list[str] = []

    for fluid in sorted(cad.glob("*_fluid.stl")):
        name = fluid.name[: -len("_fluid.stl")]
        solid = cad / f"{name}_solid.stl"
        if not solid.is_file():
            refused.append(f"{name}: no {solid.name} beside the fluid STL")
            continue
        entry = manifests.get(name, {})
        um = entry.get("smallest_feature_um")
        if not um:
            refused.append(
                f"{name}: no smallest_feature_um in any manifest, and the cell size is derived "
                "from it. Add it to the manifest rather than letting this tool guess.")
            continue
        try:
            part = P.measure(name, solid, fluid, narrowest_m=float(um) * 1e-6,
                             note=(entry.get("source") or {}).get("note", ""),
                             declared={k: v for k, v in entry.items()
                                       if k in ("kind", "family", "fluid_volume_mm3",
                                                "exact_fluid_volume_mm3", "fluid_bodies",
                                                "checks_passed")})
        except P.PartError as exc:
            refused.append(f"{name}: {exc}")
            continue
        if (why := P.unit_complaint(part.solid_lo, part.solid_hi)):
            refused.append(f"{name}: {why}")
            continue
        measured.append(part)

    if not measured:
        print("nothing measured", file=sys.stderr)
        for r in refused:
            print(f"  REFUSED {r}", file=sys.stderr)
        return 1

    print(f"{'part':<34}{'narrow':>8}{'fluid mm3':>12}{'solid mm3':>12}"
          f"{'passages':>10}   extent mm")
    for p in measured:
        e = p.extent_mm
        declared = p.declared.get("exact_fluid_volume_mm3") or p.declared.get("fluid_volume_mm3")
        flag = ""
        if declared:
            err = abs(p.fluid_volume_m3 * 1e9 - declared) / declared * 100
            flag = f"  (manifest {declared:g}, {err:+.3f}%)" if err > 0.01 else ""
        bodies = p.declared.get("fluid_bodies")
        if bodies is not None and bodies != p.passages:
            flag += f"  (manifest says {bodies} bodies)"
        print(f"  {p.name:<32}{p.narrowest_m * 1e6:>6.0f}um{p.fluid_volume_m3 * 1e9:>12.5f}"
              f"{p.solid_volume_m3 * 1e9:>12.4f}{p.passages:>10}   "
              f"{e[0]:.3f} x {e[1]:.3f} x {e[2]:.3f}{flag}")

    path = P.save(cad, measured)
    print(f"{chr(10)}  {len(measured)} part(s) written to {path}")
    for r in refused:
        print(f"  REFUSED {r}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
