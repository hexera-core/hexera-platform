"""Build every test geometry, verify it twice, and write STEP and STL only if both checks pass.

A geometry that fails verification is NOT written. A broken solid on disk gets picked up later by
somebody who assumes it was checked, and then the mesher's behaviour on it is attributed to the mesher.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import murray                                                  # noqa: E402
import published as P                                          # noqa: E402
import verify                                                  # noqa: E402

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
    """Write a binary STL at a deflection tied to the SMALLEST FEATURE, not the part diagonal.

    The usual default of diagonal/1000 is 25 um on a 25 mm part, which erases a 10 um channel outright.
    Returns the triangle count so the manifest records how fine the file actually is.
    """
    mesh = verify.tessellate(shape, deflection_mm=deflection_mm, angular_rad=0.1)
    mesh.export(str(path))
    return len(mesh.faces)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    failures = 0

    jobs: list[tuple] = []
    for spec in P.STRAIGHT_CATALOGUE:
        jobs.append((spec.name, spec, "straight"))
    jobs.append((P.PIN_FIN.name, P.PIN_FIN, "pinfin"))
    jobs.append(("murray_branching_4gen", murray.MurraySpec(), "murray"))

    for name, spec, kind in jobs:
        print("=" * 96)
        print(name)
        t0 = time.time()
        # WHAT SHAPE IS EXPECTED, stated per geometry rather than assumed.
        #
        # The first export run rejected five of six geometries and every rejection was the checker
        # being wrong, not the CAD. A straight-channel array's FLUID is N parallel prisms that never
        # touch, so it is legitimately N disconnected bodies; the checker demanded one. A monolithic
        # block with a channel network cut into it has TWO shells, the outer surface and the cavity
        # surface, and ONE body; the checker demanded one shell. Both assumptions are false for exactly
        # the geometry class this project exists for, which is enclosed channels.
        if kind == "straight":
            solid, fluid = P.build_straight(spec)
            exact, feature, bbox = spec.fluid_volume_mm3, spec.narrowest_mm, spec.extent_mm
            src = spec.source
            # ONE SHELL, ROOF OR NO ROOF, and the reason is worth writing down because I predicted
            # 1 + N and was wrong. A roof only encloses a channel if the channel is also closed at its
            # ENDS. These are through-channels: they run the full length of the slab, so both ends are
            # flush with its faces and the channel surface joins the outer surface. One connected
            # surface, one shell. A void needs closing on every side, not just the top.
            solid_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=1)
            fluid_shape = dict(expect_solids=spec.n_channels, expect_bodies=spec.n_channels,
                               expect_shells=spec.n_channels)
        elif kind == "pinfin":
            solid, fluid = P.build_pinfin(spec)
            exact, feature, bbox = spec.fluid_volume_mm3, spec.narrowest_mm, None
            src = spec.source
            solid_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=None)
            fluid_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=None)
        else:
            solid, fluid, spec = murray.build(spec)
            exact, feature, bbox = None, spec.narrowest_mm, None
            src = None
            # A GENUINELY ENCLOSED VOID, unlike the through-channels above: the network sits inside a
            # wall margin on every side, so nothing reaches a face. OpenCASCADE calls that one solid
            # with two shells, outer and cavity. trimesh, working from triangles alone, sees two closed
            # surfaces and so reports two BODIES. Both are right about the same part; they are counting
            # different things, which is why the expected numbers differ here and nowhere else.
            solid_shape = dict(expect_solids=1, expect_bodies=2, expect_shells=2)
            fluid_shape = dict(expect_solids=1, expect_bodies=1, expect_shells=1)
        build_s = time.time() - t0

        rs = verify.verify(solid, f"{name} solid", expect_bbox_mm=bbox,
                           expect_min_feature_mm=feature, **solid_shape)
        rf = verify.verify(fluid, f"{name} fluid", expect_volume_mm3=exact,
                           expect_min_feature_mm=feature, **fluid_shape)
        vf = _volume(fluid)
        vol_err = abs(vf - exact) / exact if exact else None

        ok = rs.ok and rf.ok and (vol_err is None or vol_err < 1e-6)
        print(rs.text())
        print(rf.text())
        if vol_err is not None:
            print(f"  kernel fluid volume {vf:.6f} mm^3, {vol_err*100:.6f}% from exact")

        if not ok:
            failures += 1
            print(f"  NOT WRITTEN: verification failed, so no file is produced")
            continue

        defl = feature / 20.0
        n_solid = write_stl(solid, OUT / f"{name}_solid.stl", defl)
        n_fluid = write_stl(fluid, OUT / f"{name}_fluid.stl", defl)
        write_step(solid, OUT / f"{name}_solid.step")
        write_step(fluid, OUT / f"{name}_fluid.step")
        print(f"  written: {name}_solid/.fluid .step and .stl "
              f"({n_solid:,} and {n_fluid:,} triangles at {defl*1e3:.2f} um deflection)")

        manifest.append({
            "name": name,
            "kind": kind,
            "smallest_feature_um": round(feature * 1e3, 3),
            "fluid_volume_mm3": round(vf, 9),
            "exact_fluid_volume_mm3": round(exact, 9) if exact else None,
            "solid_volume_mm3": round(_volume(solid), 6),
            "stl_deflection_um": round(defl * 1e3, 3),
            "stl_triangles_solid": n_solid,
            "stl_triangles_fluid": n_fluid,
            "build_seconds": round(build_s, 1),
            "checks_passed": len(rs.checks) + len(rf.checks),
            "source": None if src is None else {"paper": src.paper, "url": src.url, "note": src.note},
        })

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("=" * 96)
    print(f"{len(manifest)} geometries written to {OUT}")
    for g in manifest:
        print(f"  {g['name']:34s} {g['smallest_feature_um']:7.1f} um   "
              f"fluid {g['fluid_volume_mm3']:10.4f} mm^3   {g['checks_passed']} checks")
    if failures:
        print(f"\n{failures} geometries FAILED verification and were not written")
    for k, why in P.NOT_BUILT.items():
        print(f"\n  NOT BUILT  {k}: {why}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
