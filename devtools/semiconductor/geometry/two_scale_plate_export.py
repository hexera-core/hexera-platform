"""Build, verify and export the two-scale plate, in both its variants.

    python geometry/two_scale_plate_export.py

Its own exporter rather than another entry in advanced_export's BUILDERS because what has to be
verified here is different from a volume and a body count. The point of this part is that its
width histogram SPLITS - that `cad/scales.scales_of_field` finds two modes with an empty trough
between them - and no other part in the corpus does. A part that built cleanly and read as one
scale would be a part that does not do its job, and nothing else checks that.

MEASURED on the first build, 2026-10-29, at a 1.0 mm subdivision:

    narrowest passage   98.0 um          built as 98 um
    split at            244.1 um         trough share 0.002
    fine                 98.0 um over  430.4 mm2
    broad               398.3 um over  709.8 mm2
    fine share          0.377            the customer plate is 0.359
    prismatic axis      y at 0.0829      the customer plate is y at 0.0694

The broad side reads 398 um rather than the 2.1 mm the manifold is built at, because the broad
population's p05 is dragged down by the junctions where each slot opens into a feeder. That makes
the separation 2.0 octaves against the splitter's floor of 2 - this part sits ON the threshold it
is meant to exercise, which is worth knowing and is not a defect.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import two_scale_plate                                           # noqa: E402
import verify                                                    # noqa: E402
from advanced_export import _volume, write_step, write_stl       # noqa: E402

OUT = Path(__file__).parent.parent / "cad"
#: A face counts as reached when the coolant comes within this of it, in mm.
TOUCH_TOL_MM = 1.0e-4
#: Chord error for the exported STL, as a fraction of the narrowest feature. A tenth of 98 um is
#: well under the 0.0797 mm the customer's own coolant was tessellated at, which is roughly one
#: chord across the feature and is a plausible reason its scale split was ever marginal.
CHORD_FRACTION = 0.1


#: Longest edge in the exported surface, in millimetres.
#:
#: WHY A PLANAR PART HAS TO BE SUBDIVIDED AT ALL. A chord tolerance does nothing to a flat face:
#: this plate tessellates to 944 triangles however fine the deflection, because every face is
#: planar and two triangles represent it exactly. That is a correct surface and a useless one for
#: the thing this part exists to exercise - the width field is read at the surface's own
#: VERTICES, and with 944 triangles every vertex sits on a corner, where the inscribed radius
#: reads whatever the corner opens into rather than the channel it belongs to. Measured: at 944
#: triangles `scales_of_field` found ONE scale on this part; subdivided to 1 mm it found two,
#: splitting at 244.1 um with a trough share of 0.002.
#:
#: A real CAD export carries this density anyway - the customer's own coolant is 52,334 triangles
#: over a part of similar complexity - so this is making the test article resemble the thing it
#: stands in for, not padding it.
SUBDIVIDE_MM = 1.0


def _write_dense(shape, path: Path, chord_mm: float) -> int:
    """Tessellate, then subdivide so the surface carries vertices in its faces' interiors."""
    mesh = verify.tessellate(shape, deflection_mm=chord_mm, angular_rad=0.1)
    mesh = mesh.subdivide_to_size(max_edge=SUBDIVIDE_MM)
    mesh.export(str(path))
    return len(mesh.faces)


def bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    bb = Bnd_Box()
    BRepBndLib.Add_s(shape, bb)
    lo, hi = bb.CornerMin(), bb.CornerMax()
    return (lo.X(), lo.Y(), lo.Z()), (hi.X(), hi.Y(), hi.Z())


def reached_faces(fluid, solid) -> list[str]:
    """Which of the block's six faces the coolant gets to - the same test the mesher makes before
    it plans a port, run here so a variant that claims to be plumbed really is."""
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
    manifest, failures = [], 0

    for name, spec in two_scale_plate.VARIANTS:
        print("=" * 92)
        print(name)
        print(spec.describe())

        t0 = time.time()
        solid, fluid, spec = two_scale_plate.build(spec)
        built = time.time() - t0

        fv, sv = _volume(fluid), _volume(solid)
        block = spec.plate_x_mm * spec.plate_y_mm * spec.plate_z_mm
        # THE PORTS STICK OUT ON PURPOSE, so the fluid is legitimately bigger than the block it
        # was cut from. A port is fused to the coolant BEFORE the block is cut, and the overhang
        # is what makes the coolant surface cross the face cleanly rather than land exactly on it,
        # where a float32 STL coordinate falls either side and the port is sometimes there and
        # sometimes not. The first version of this check did not know that and refused the ported
        # variant for being 0.1440 mm3 over - which is exactly 2 bores x 1.2 x 1.2 x 0.05.
        overhang = (len(spec.bores()) * spec.bore_mm ** 2 * spec.bore_overhang_mm
                    if spec.ported else 0.0)
        closed = abs((fv + sv) - block - overhang)
        reached = reached_faces(fluid, solid)

        print(f"  built in         {built:.1f} s")
        print(f"  fluid            {fv:10.4f} mm3")
        print(f"  solid            {sv:10.4f} mm3")
        if overhang:
            print(f"  port overhang    {overhang:10.4f} mm3 outside the block, by design")
        print(f"  closes on block  {closed:.3e} mm3")
        print(f"  faces reached    {reached or 'none - the coolant is sealed'}")

        # THE TWO THINGS THAT MAKE THIS PART WORTH HAVING, checked rather than assumed.
        bad = []
        if closed > 1e-6 * block:
            bad.append(f"fluid + solid is {closed:.3e} mm3 off the block: the boolean is not clean")
        if spec.ported and not reached:
            bad.append("the ported variant reaches no block face, so it cannot be flowed")
        if not spec.ported and reached:
            bad.append(f"the sealed variant reaches {reached}, so it is not sealed")
        if bad:
            failures += 1
            for b in bad:
                print(f"  REFUSED: {b}")
            continue

        chord = spec.narrowest_mm * CHORD_FRACTION
        n_fluid = _write_dense(fluid, OUT / f"{name}_fluid.stl", chord)
        n_solid = _write_dense(solid, OUT / f"{name}_solid.stl", chord)
        write_step(solid, OUT / f"{name}_solid.step")
        print(f"  wrote            {n_fluid:,} + {n_solid:,} triangles at a {chord*1000:.1f} um "
              f"chord, subdivided to {SUBDIVIDE_MM} mm")

        manifest.append({
            "name": name, "kind": "two_scale",
            # THE KEY THE INDEXER READS, and it refuses rather than guessing when it is absent:
            # "the cell size is derived from it". The first version of this called it
            # narrowest_um and both parts were refused, which is the tool working.
            "smallest_feature_um": spec.narrowest_mm * 1000.0,
            "fluid_volume_mm3": round(fv, 6), "solid_volume_mm3": round(sv, 6),
            "fluid_bodies": 1,
            "slots": len(spec.slot_x_positions()), "pitch_mm": spec.pitch_mm,
            "manifold_mm": spec.manifold_mm, "scale_ratio": spec.manifold_mm / spec.slot_mm,
            "channel_height_mm": spec.channel_height_mm,
            "reached_faces": reached, "ported": spec.ported,
            "source": {"note": ("built for this corpus: the only part in it with two scales of "
                                "passage, at the first customer's own 21:1 ratio and 0.545 mm "
                                "slot pitch")},
        })

    # WRITTEN TO BOTH, because the two directories mean different things and the indexer reads
    # the second. Staging is millimetres and is where this exporter belongs; the corpus is metres
    # and is where `declared_features` looks for a part's smallest feature.
    body = json.dumps(manifest, indent=2) + "\n"
    (OUT / "manifest_two_scale.json").write_text(body)
    corpus = Path(__file__).parent.parent.parent.parent / "cad" / "semiconductor"
    if corpus.is_dir():
        (corpus / "manifest_two_scale.json").write_text(body)
    print("=" * 92)
    print(f"{len(manifest)} written, {failures} refused -> {OUT}")
    print("next: devtools/semiconductor/run.sh import --from devtools/semiconductor/cad "
          "--scale 1e-3, then python devtools/semiconductor/index_parts.py")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
