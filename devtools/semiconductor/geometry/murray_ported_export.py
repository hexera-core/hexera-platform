"""Build, verify and export the ported Murray tree.

    python geometry/murray_ported_export.py

Written as its own exporter rather than another entry in advanced_export's BUILDERS because the
thing being verified is different: the point of this part is that the coolant reaches TWO NAMED
BLOCK FACES, and nothing else in the package has that to check. A volume and a body count would
pass on the sealed version too, which is the version this part exists not to be.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import murray_ported                                             # noqa: E402
import verify                                                    # noqa: E402
from advanced_export import _volume, write_step, write_stl       # noqa: E402

OUT = Path(__file__).parent.parent / "cad"
NAME = "murray_ported_4gen"
#: A face counts as reached when the coolant comes within this of it, in mm. The STL is float32,
#: so an exactly flush surface lands a few parts in 10^7 off the plane; the ports overhang by
#: 50 um on purpose so the margin is never in doubt.
TOUCH_TOL_MM = 1.0e-4


def feature_mm(spec) -> float:
    """The narrowest thing in the part: the leaf channel, which the drain holes match."""
    return spec.narrowest_mm


def bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    bb = Bnd_Box()
    BRepBndLib.Add_s(shape, bb)
    lo, hi = bb.CornerMin(), bb.CornerMax()
    return (lo.X(), lo.Y(), lo.Z()), (hi.X(), hi.Y(), hi.Z())


def reached_faces(fluid, solid) -> list[str]:
    """Which of the block's six faces the coolant gets to, from the two bounding boxes.

    The same test the mesher makes before it plans a port, run here so a part that cannot be
    flowed through is never written in the first place.
    """
    flo, fhi = bbox(fluid)
    blo, bhi = bbox(solid)
    out = []
    for axis, name in enumerate("xyz"):
        if flo[axis] - blo[axis] <= TOUCH_TOL_MM:
            out.append(f"{name}min")
        if bhi[axis] - fhi[axis] <= TOUCH_TOL_MM:
            out.append(f"{name}max")
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    spec = murray_ported.PortedSpec()
    print("=" * 92)
    print(NAME)
    print(spec.describe())

    t0 = time.time()
    solid, fluid, spec = murray_ported.build(spec)
    build_s = time.time() - t0
    print(f"  built in {build_s:.1f}s")

    vf, vs = _volume(fluid), _volume(solid)
    upper = murray_ported.analytic_fluid_volume(spec)
    checks: list[str] = []
    problems: list[str] = []

    # The coolant volume against its own upper bound. A union cannot be bigger than the sum of its
    # parts, so a measured volume above the bound means the boolean produced something that is not
    # the union of what went in.
    if vf > upper:
        problems.append(f"coolant is {vf:.5f} mm3, above the {upper:.5f} mm3 upper bound: the "
                        "boolean fuse produced something larger than its own parts")
    elif vf < 0.70 * upper:
        problems.append(f"coolant is {vf:.5f} mm3, under 70% of the {upper:.5f} mm3 upper bound. "
                        "The bound only subtracts nothing for overlaps, so a gap this large means "
                        "segments went missing rather than merely overlapping")
    else:
        checks.append(f"coolant {vf:.5f} mm3, {vf / upper * 100:.1f}% of its upper bound")

    # ONE SHELL EACH, and that is the whole difference from the sealed part. A block with a sealed
    # cavity has two shells - its outside and the cavity's wall - and a block the coolant runs
    # through has one, because the cavity joins the outside at the ports. So the shell count is a
    # topological statement that the part is open, independent of any bounding box.
    rs = verify.verify(solid, f"{NAME} solid", expect_min_feature_mm=feature_mm(spec),
                       expect_solids=1, expect_bodies=1, expect_shells=1)
    rf = verify.verify(fluid, f"{NAME} fluid", expect_min_feature_mm=feature_mm(spec),
                       expect_solids=1, expect_bodies=1, expect_shells=1)
    print(rs.text())
    print(rf.text())
    for r in (rs, rf):
        problems.extend(f"{r.geometry}: {c.name} - {c.detail}" for c in r.checks if not c.passed)
        if r.ok:
            checks.append(f"{r.geometry}: {len(r.checks)} checks")

    # THE CHECK THIS PART EXISTS FOR.
    faces = reached_faces(fluid, solid)
    want = {spec.feed_face, spec.drain_face}
    if not want.issubset(set(faces)):
        problems.append(
            f"the coolant reaches {faces or 'no block face at all'}, and this part is only worth "
            f"having if it reaches {sorted(want)}. Without both there is nothing to fix pressure "
            "and no solver will run it - which is exactly the sealed part this one replaces.")
    else:
        checks.append(f"coolant reaches {', '.join(sorted(faces))}, which includes the feed "
                      f"({spec.feed_face}) and the drain ({spec.drain_face})")

    for c in checks:
        print(f"  ok   {c}")
    for p in problems:
        print(f"  FAIL {p}")
    if problems:
        print("  NOT WRITTEN: verification failed")
        return 1

    defl = feature_mm(spec) / 20.0
    ns = write_stl(solid, OUT / f"{NAME}_solid.stl", defl)
    nf = write_stl(fluid, OUT / f"{NAME}_fluid.stl", defl)
    write_step(solid, OUT / f"{NAME}_solid.step")
    write_step(fluid, OUT / f"{NAME}_fluid.step")
    print(f"  written: {ns:,} and {nf:,} triangles at {defl * 1e3:.1f} um deflection")

    entry = {
        "name": NAME,
        "kind": "branching",
        "smallest_feature_um": round(feature_mm(spec) * 1e3, 3),
        "fluid_volume_mm3": round(vf, 9),
        "solid_volume_mm3": round(vs, 6),
        "upper_bound_mm3": round(upper, 9),
        "stl_triangles_solid": ns,
        "stl_triangles_fluid": nf,
        "build_seconds": round(build_s, 1),
        "reached_faces": faces,
        "feed_face": spec.feed_face,
        "drain_face": spec.drain_face,
        "checks_passed": len(checks) + len(rf.checks) + len(rs.checks),
        "fluid_bodies": 1,
        "source": {
            "paper": "parametric, Murray's law",
            "url": "",
            "note": ("The murray_branching_4gen tree with a feed and a drain: the trunk carried "
                     "out through ymin, and one 119 um hole per leaf tip up through zmax. Fed "
                     "from the side and drained through the lid, which is how a cold plate is "
                     "plumbed. NOT a replica of any part."),
        },
    }
    path = OUT / "manifest_murray_ported.json"
    path.write_text(json.dumps([entry], indent=2) + chr(10), encoding="utf-8")
    print(f"  manifest: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
