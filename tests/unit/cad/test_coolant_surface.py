"""The coolant comes out of the extractor as one closed STL, with nothing declared.

The extraction already held the coolant's whole boundary with every face oriented out of the
fluid, but it never left the child process: as_dict carries centroids and areas and no geometry,
and `stage` - the only writer - refuses without declared openings ({name, area_m2, near_m}), which
a plain uploaded cold plate does not have. So a conjugate case could not be built from an upload.

MEASURED, 2026-10-25, on the 11-part corpus. Five parts write a watertight, correctly-wound
surface whose enclosed volume matches what the CAD was built to:

    part                        known mm3   enclosed mm3   err        tris      s
    unit_cell_100um                2.6000         2.6000   +0.000%      12    1.1
    hotspot_array                 10.0800        10.0800   +0.000%    1056    1.6
    manifold_two_level             4.5600         4.5600   +0.000%    1212    3.4
    wavy_250um_16ch               43.6755        43.6755   +0.000%   49344   32.4
    embedded_silicon_D9_10um       0.7500         0.7425   -1.000%    2376    8.7

Every one is 0 open edges, and every bounding box matches the fluid STL exported from the separate
fluid STEP to within 1.2e-6 mm (2.5e-2 mm for embedded_silicon, which is its two missing edge
channels). murray_ported extracts cleanly but is REFUSED here, correctly: its source STEP has 52
faces OpenCASCADE cannot triangulate, so the surface comes out with open edges, and an open edge
would let surfaceToCell flood the whole block instead of the channels.

THE ORIENTATION IS PROVEN BY MEASUREMENT, not argument. write_stl_binary zeroes every stored facet
normal, and the mesher reads this surface with `surfaceToCell ... useSurfaceOrientation true`, so a
surface written inside out would tag the metal as coolant with nothing failing. An A/B on
unit_cell at 5 cells across, one mesh from this writer's zeroed-normal STL and one from the
shipped OCC-written STL that carries real normals, came out identical in every figure:

    fluid     383,736 cells   2.94474 mm3   non-ortho 2.5008   skew 0.0546658
    solid     801,748 cells   5.50526 mm3   non-ortho 30.948   skew 0.545723
    interface 152,844 faces both sides, conformal
    ports     ymin 236, ymax 236

That pair alone does NOT settle which of the two the mesher reads, because the shipped STLs'
normals were derived from their own winding, so the two readings agree on every facet by
construction. A third run settles it: the same part meshed from a DELIBERATELY INVERTED surface,
zeroed normals and every triangle's winding reversed.

    run                        normals   winding    fluid cells   fluid mm3   ports
    shipped STL                real      correct        383,736     2.94474   236 / 236
    this writer                ZEROED    correct        383,736     2.94474   236 / 236
    this writer, inverted      zeroed    REVERSED        16,220     0.12578       4 / 4

So the normal does not matter and the winding does: 95.16% off the declared coolant volume, the
fluid in two disconnected pieces, and the solid swallowing 8.32 mm3 of a 5.85 mm3 block. The port
check still said "the coolant can flow", on four faces.

THAT IS WHY THE GUARD IS NOT DECORATION. An inverted surface produces a mesh that passes
checkMesh - "Mesh OK", non-ortho 1.33, skew 0.039 - and is wrong by a factor of twenty.
write_surface refuses three things rather than trusting anything: an open edge, which lets
surfaceToCell flood the block; a non-manifold edge, which leaves "inside" undefined along it; and
an edge its two faces cross the same way, which is a flipped face. The third matters because the
signed-volume check is one global number and a few flipped facets on a large surface hide inside
it. The part indexer would not catch an inversion either: it reported the flipped surface's volume
as 2.60000 mm3, the same as the correct one.
"""
from __future__ import annotations

import pytest

from meshpipeline.cad.fluid_volume import surface_volume

MM3 = 1e9        # m3 -> mm3


def _cube(side: float = 1.0, *, inside_out: bool = False) -> list:
    """A closed cube as 12 triangles, wound so its normals point out (or in)."""
    s = side
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tris = []
    for a, b, c, d in quads:
        tris.append((v[a], v[b], v[c]))
        tris.append((v[a], v[c], v[d]))
    if inside_out:
        tris = [(a, c, b) for a, b, c in tris]
    return tris


# #
# the orientation check, which is the whole safety net
# #
def test_a_surface_wound_outwards_encloses_a_positive_volume():
    assert surface_volume(_cube(1.0)) == pytest.approx(1.0)
    assert surface_volume(_cube(2.0)) == pytest.approx(8.0)


def test_a_surface_wound_inwards_encloses_a_negative_one():
    # This is the failure that would otherwise be silent: surfaceToCell would tag the metal as
    # coolant and the coolant as metal, and every check would still pass.
    assert surface_volume(_cube(1.0, inside_out=True)) == pytest.approx(-1.0)


# #
# the writer, on a plate built here
# #
@pytest.fixture(scope="module")
def channelled_plate(tmp_path_factory):
    """A 2 x 10 x 1 mm plate with four roofed channels through it, as a STEP, in millimetres.

    Roofed on purpose: a channel open at the top is a notch in the part's outline rather than a
    hole in a face, and only a hole can be capped. The four channels are separate cavities, which
    is what makes this a test of writing EVERY passage.
    """
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    solid = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 2.0, 10.0, 1.0).Shape()
    for i in range(4):
        channel = BRepPrimAPI_MakeBox(gp_Pnt(0.3 + i * 0.4, 0.0, 0.25), 0.1, 10.0, 0.5).Shape()
        solid = BRepAlgoAPI_Cut(solid, channel).Shape()

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    dest = tmp_path_factory.mktemp("plate") / "channelled_plate.step"
    writer = STEPControl_Writer()
    writer.Transfer(solid, STEPControl_AsIs)
    assert writer.Write(str(dest)) == IFSelect_ReturnStatus.IFSelect_RetDone
    return dest


#: 4 channels of 0.1 x 0.5 mm running the plate's 10 mm length
FOUR_CHANNELS_MM3 = 4 * 0.1 * 0.5 * 10.0


@pytest.fixture(scope="module")
def written(channelled_plate, tmp_path_factory):
    from meshpipeline.cad.fluid_volume import surface_bounded
    out = tmp_path_factory.mktemp("coolant") / "plate_fluid.stl"
    return surface_bounded(channelled_plate, out, scale=0.001, kind="cad", budget_s=300.0)


def test_the_coolant_is_written_with_nothing_declared(written):
    # The point of the whole thing: a file arrives, and the coolant inside it comes out. No
    # declared openings, no seed, no unit metadata beyond the scale.
    assert written is not None, "the coolant of a four-channel plate should be written"
    from pathlib import Path
    assert Path(written["path"]).is_file()
    assert Path(written["path"]).stat().st_size > 84


def test_the_written_surface_is_closed(written):
    # An open edge would let surfaceToCell flood the block instead of the channels, so the writer
    # refuses rather than writing one.
    assert written["open_edges"] == 0
    assert written["nonmanifold_edges"] == 0
    assert written["inconsistent_edges"] == 0


def test_every_passage_is_written_not_just_the_largest(written):
    # Four channels, four separate cavities, all of them coolant. An embedded-silicon array
    # extracts as 198 and there is nothing in the geometry that makes one of them something else.
    assert written["passages"] == 4
    assert written["volume_mm3"] == pytest.approx(FOUR_CHANNELS_MM3, rel=1e-3)


def test_what_the_surface_encloses_is_what_the_cad_says(written):
    # The two volumes a caller compares: what the B-rep says the coolant is, and what the surface
    # actually written encloses.
    assert written["surface_volume_mm3"] == pytest.approx(FOUR_CHANNELS_MM3, rel=1e-3)
    assert written["surface_volume_mm3"] == pytest.approx(written["volume_mm3"], rel=1e-3)


def test_the_surface_came_out_the_right_way_round(written):
    assert written["surface_volume_m3"] > 0.0
    assert written["volume_m3"] > 0.0


def test_it_says_where_inside_the_coolant_is(written):
    # A conjugate case needs a point inside the fluid, and the part's faces cannot give one.
    seed = written["seed_m"]
    assert seed is not None and len(seed) == 3
    # inside the plate's own 2 x 10 x 1 mm extent, in metres
    assert 0.0 <= seed[0] <= 0.002 and 0.0 <= seed[1] <= 0.010 and 0.0 <= seed[2] <= 0.001


# #
# and the contrast with the writer that was already there
# #
def test_the_staging_writer_still_refuses_without_a_declaration(channelled_plate, tmp_path):
    """`stage_bounded` is for a case whose openings the user has already named, and it returns
    None without them. That is why a plain cold plate needed its own way out: it arrives with
    nothing named at all."""
    from meshpipeline.cad.fluid_volume import stage_bounded
    assert stage_bounded(channelled_plate, tmp_path / "staged", declared=[], scale=0.001) is None
    assert stage_bounded(channelled_plate, tmp_path / "staged",
                         declared=[{"name": "inlet", "area_m2": 5e-8}], scale=0.001) is None


def test_an_inverted_surface_is_refused_rather_than_written(channelled_plate, tmp_path):
    """The guard, end to end, on a real extraction turned inside out.

    Measured consequence of not having it: the same part meshed from an inverted surface gave
    16,220 fluid cells instead of 383,736 and 0.12578 mm3 instead of 2.94474, with checkMesh
    calling the result "Mesh OK". Nothing downstream would have noticed.
    """
    from meshpipeline.cad.fluid_volume import extract, surface_volume, write_surface
    from meshpipeline.cad.scout import read_cad

    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    from OCP.TopoDS import TopoDS

    shape = read_cad(channelled_plate)
    t = gp_Trsf()
    t.SetScaleFactor(0.001)                       # the plate is authored in millimetres
    x = extract(BRepBuilderAPI_Transform(shape, t, True).Shape())
    assert x.found, "the plate's coolant should extract"

    # the right way round, as a control
    good = write_surface(x, tmp_path / "good.stl")
    assert good is not None and good["surface_volume_m3"] > 0.0

    # every boundary face reversed, which is what flips the winding _face_triangles emits
    for p in x.passages:
        p.boundary = [TopoDS.Face_s(f.Reversed()) for f in p.boundary]
    assert write_surface(x, tmp_path / "inverted.stl") is None
    assert not (tmp_path / "inverted.stl").exists(), "an inverted surface must not be written"


def test_the_sign_alone_would_not_catch_a_few_flipped_faces():
    """Why the winding count is gated as well as the sign. Flip one facet of a cube and the total
    signed volume is still positive, so the sign says nothing is wrong."""
    tris = _cube(1.0)
    tris[0] = (tris[0][0], tris[0][2], tris[0][1])
    assert surface_volume(tris) > 0.0, "the global sign survives one flipped facet"
