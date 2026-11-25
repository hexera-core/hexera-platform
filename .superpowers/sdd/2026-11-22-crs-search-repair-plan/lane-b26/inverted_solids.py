"""How often does a real v4 DELIVERY carry an inside-out solid, and what did that cost the pre-B26 loss number?

    inverted.py <out dir> [<out dir> ...]

Pass 1, every `delivered.brep` under the given wave out-dirs: solids, and how many have a NEGATIVE volume
(no Booleans, so it is cheap). Pass 2, for each delivery that carries one and whose input is beside it:
`part_kept` measured BOTH ways - with B26's orientation fix, and the way B22 measured before it - so the
gap says how much any pre-B26 loss number can be trusted.
"""
from __future__ import annotations

import sys
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape


def read(path: str) -> TopoDS_Shape:
    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, path, BRep_Builder()):
        raise ValueError(path)
    return shape


def solids(shape) -> list:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def volume(shape) -> float:
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    return p.Mass()


def kept(ins: list, outs: list) -> float | None:
    """sum(common(in_i, outs)) / sum(vol(in_i)), with the orientations exactly as given."""
    num = den = 0.0
    for body in ins:
        bv = abs(volume(body))
        if bv <= 0:
            continue
        got = 0.0
        for tool in outs:
            common = BRepAlgoAPI_Common(body, tool)
            if not common.IsDone():
                return None
            got += abs(volume(common.Shape()))
        num += min(got, bv)
        den += bv
    return None if den <= 0 else num / den


def oriented(s):
    return s if volume(s) > 0 else s.Reversed()


def main(dirs: list[str]) -> int:
    files = sorted(p for d in dirs for p in Path(d).rglob("delivered.brep"))
    print(f"{len(files)} delivered BREPs under {len(dirs)} wave dir(s)\n")
    rows = []
    for f in files:
        try:
            ss = solids(read(str(f)))
        except Exception as exc:  # noqa: BLE001
            print(f"  unreadable: {f} ({type(exc).__name__})")
            continue
        vols = [volume(s) for s in ss]
        neg = [v for v in vols if v < 0]
        rows.append((f, len(ss), neg))
    with_neg = [r for r in rows if r[2]]
    print(f"PASS 1 - inverted solids in deliveries: {len(with_neg)} of {len(rows)} deliveries carry at least one "
          f"({len(with_neg) / max(len(rows), 1):.1%})")
    by_arm: dict[str, list] = {}
    for f, n, neg in rows:
        arm = next((p for p in f.parts if p.startswith(("search_", "staged", "input_only"))), "?")
        by_arm.setdefault(arm, []).append(bool(neg))
    for arm, flags in sorted(by_arm.items()):
        print(f"   {arm:14} {sum(flags):>3} of {len(flags):>3} deliveries have an inverted solid")
    print()
    for f, n, neg in with_neg[:40]:
        print(f"   {f.parts[-3]}/{f.parts[-2]}: {n} solids, {len(neg)} negative "
              f"({', '.join(f'{v:.4g}' for v in neg[:6])})")

    print("\nPASS 2 - what the orientation cost the measurement (part_kept both ways):")
    done = 0
    for f, _n, _neg in with_neg:
        inp = f.parent / "input.brep"
        if not inp.is_file():
            inp = next((p for p in f.parent.parent.rglob("input.brep")), None)  # type: ignore[assignment]
            if inp is None or not Path(inp).is_file():
                continue
        try:
            ins, outs = solids(read(str(inp))), solids(read(str(f)))
            raw = kept(ins, outs)
            fix = kept([oriented(s) for s in ins], [oriented(s) for s in outs])
        except Exception as exc:  # noqa: BLE001
            print(f"   {f.parts[-3]}: not measurable ({type(exc).__name__})")
            continue
        flag = ""
        if raw is not None and fix is not None:
            flag = "  <- a pre-B26 PASS that B26 REFUSES" if raw >= 0.10 > fix else ""
        print(f"   {f.parts[-3]}/{f.parts[-2]}: pre-B26 {raw if raw is None else round(raw, 4)}  "
              f"B26 {fix if fix is None else round(fix, 4)}{flag}")
        done += 1
        if done >= 12:
            print("   (first 12)")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
