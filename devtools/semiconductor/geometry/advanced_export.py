"""Build, verify and export the three advanced geometries.

A geometry that fails verification is NOT written, for the same reason as export_all.py: a broken
solid on disk gets picked up later by somebody who assumes it was checked, and then the mesher's
behaviour on it is blamed on the mesher.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import verify                                                    # noqa: E402
from advanced_build import BUILDERS                              # noqa: E402

OUT = Path(__file__).parent.parent / "cad"


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return g.Mass()


def write_step(shape, path: Path) -> None:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    if w.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEP write failed for {path}")


def write_stl(shape, path: Path, deflection_mm: float) -> int:
    mesh = verify.tessellate(shape, deflection_mm=deflection_mm, angular_rad=0.1)
    mesh.export(str(path))
    return len(mesh.faces)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    failures = 0

    for name, builder in BUILDERS:
        print("=" * 92)
        print(name)
        t0 = time.time()
        solid, fluid, spec = builder()
        build_s = time.time() - t0

        feature = spec.narrowest_mm
        exact = spec.fluid_mm3
        vf = _volume(fluid)
        vs = _volume(solid)
        err = abs(vf - exact) / exact if exact else None

        # SHAPE EXPECTATIONS STATED PER GEOMETRY, never assumed. The lesson from export_all.py:
        # a straight-channel array's fluid is N prisms that never touch, so it is legitimately N
        # bodies, and a block with a cavity has two shells and one body. Guessing one rule for all
        # of them rejected five of six parts on the first run, every time wrongly.
        if name == "unit_cell_100um":
            # One through-channel in one block: the channel is open at both ends, so its surface
            # joins the outer surface and there is ONE shell, not two.
            s_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=1)
            f_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=1)
            f_bodies = 1
        elif name == "hotspot_array":
            n = spec.n_channels
            s_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=1)
            f_shape = dict(expect_solids=n, expect_bodies=n, expect_shells=n)
            f_bodies = n
        else:  # manifold: the slots join every channel, so the fluid is ONE connected network
            s_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=None)
            f_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=None)
            f_bodies = 1

        rs = verify.verify(solid, f"{name} solid", expect_min_feature_mm=feature, **s_shape)
        rf = verify.verify(fluid, f"{name} fluid", expect_min_feature_mm=feature,
                           expect_volume_mm3=exact, **f_shape)
        print(rs.text())
        print(rf.text())
        if err is not None:
            print(f"  kernel fluid volume {vf:.6f} mm^3, {err * 100:.4f}% from the spec's {exact:.6f}")

        ok = rs.ok and rf.ok and (err is None or err < 0.02)
        if not ok:
            failures += 1
            print("  NOT WRITTEN: verification failed")
            continue

        defl = feature / 20.0
        ns = write_stl(solid, OUT / f"{name}_solid.stl", defl)
        nf = write_stl(fluid, OUT / f"{name}_fluid.stl", defl)
        write_step(solid, OUT / f"{name}_solid.step")
        write_step(fluid, OUT / f"{name}_fluid.step")
        print(f"  written: {ns:,} and {nf:,} triangles at {defl * 1e3:.1f} um deflection")

        sized = spec.sized()
        manifest.append({
            "name": name,
            "smallest_feature_um": round(feature * 1e3, 3),
            "fluid_volume_mm3": round(vf, 9),
            "solid_volume_mm3": round(vs, 6),
            "spec_fluid_mm3": round(exact, 9),
            "volume_error_pct": round(err * 100, 4) if err is not None else None,
            "fluid_bodies": f_bodies,
            "stl_triangles_solid": ns,
            "stl_triangles_fluid": nf,
            "build_seconds": round(build_s, 1),
            "cells_at_rule_M": round(sized.cells_at_rule / 1e6, 1),
            "gb_at_rule": round(sized.gb_at_rule, 1),
            "checks_passed": len(rs.checks) + len(rf.checks),
        })

    (OUT / "manifest_advanced.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("=" * 92)
    for g in manifest:
        print(f"  {g['name']:<22}{g['smallest_feature_um']:>7.0f}um  "
              f"fluid {g['fluid_volume_mm3']:>9.4f}mm3  {g['fluid_bodies']:>3} body(s)  "
              f"{g['cells_at_rule_M']:>7.1f}M cells at the rule  {g['checks_passed']} checks")
    if failures:
        print(f"\n{failures} geometry(ies) FAILED verification and were not written")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
