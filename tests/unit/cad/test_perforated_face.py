"""A face with one hole per channel is an opening, and a flange's bore still is not.

Measured on the real parts, 2026-10-24. Before this, `_measure_wires` reported only the LARGEST
hole on a face, so one 0.08 mm2 channel was weighed against a 21.89 mm2 end face, came out at 0.4%
against the 10% bore bar, and the face read as a solid flat end. The wavy 16-channel array was read
as "single_solid / solid-body / external" with ZERO openings proposed; it is now "pipe_wall /
body-surface / internal" with two. The one-channel part was always read correctly, because one hole
is exactly what the old reader assumed.

The pair of cases that matters is a flange and a cold plate, and they are told apart by whether one
hole dominates: a flange's bore is about 93% of its face's hole area, a cold plate's largest channel
about 1%.
"""
import math

import pytest

from meshpipeline.cad.scout import MAX_DOMINANT_HOLE_SHARE, MIN_RING_BORE_FRACTION


def _share(holes: list[float]) -> float:
    """The largest hole's share of all the hole area on a face."""
    total = sum(holes)
    return max(holes) / total if total else 0.0


def _is_perforated(holes: list[float]) -> bool:
    return len(holes) > 1 and _share(holes) <= MAX_DOMINANT_HOLE_SHARE


# #
# the two cases the rule exists to separate
# #
def test_a_flange_bore_is_the_opening_and_its_bolt_holes_are_not():
    # A 100 mm bore and eight 10 mm bolt holes, in mm2. The bore takes 93% of the hole area, so the
    # face is a bore with incidental holes and the old reading is the right one.
    bore = math.pi * 50.0 ** 2
    bolts = [math.pi * 5.0 ** 2] * 8
    assert _share([bore, *bolts]) > MAX_DOMINANT_HOLE_SHARE
    assert not _is_perforated([bore, *bolts])


def test_a_cold_plate_end_face_is_perforated():
    # The Corintis baseline's section: 100 channels of 0.1 x 0.8 mm. No bore, every hole the same,
    # so the largest is 1% of the hole area and the face's open area is the sum.
    channels = [0.1 * 0.8] * 100
    assert _share(channels) == pytest.approx(0.01)
    assert _is_perforated(channels)


def test_summing_the_holes_is_what_clears_the_bore_bar():
    # The arithmetic the fix turns on, on the real part. One channel against the end face is 0.4%,
    # under the 10% bar, which is why the face read as solid. All hundred together are 36%.
    face = 19.9 * 1.1
    one, every = 0.1 * 0.8, 100 * 0.1 * 0.8
    assert one / face < MIN_RING_BORE_FRACTION
    assert every / face > MIN_RING_BORE_FRACTION
    assert every / face == pytest.approx(0.3656, abs=1e-3)


def test_one_hole_is_never_perforated():
    # The single-channel part, which the old reader handled correctly and must keep handling.
    assert not _is_perforated([0.1 * 0.8])


def test_two_equal_holes_are_perforated_and_two_unequal_ones_are_not():
    # The boundary, stated both ways round so the share is not read backwards.
    assert _is_perforated([1.0, 1.0])
    assert not _is_perforated([9.0, 1.0])


# #
# and the measurement itself, on a plate built here
# #
@pytest.fixture(scope="module")
def channelled_plate(tmp_path_factory):
    """A 2 x 10 x 1 mm plate with four roofed channels running through it, as a STEP.

    Built here rather than read from the corpus so the test is hermetic, and ROOFED on purpose:
    a channel open at the top is a notch in the face's outline, not a hole in it, and this is a
    test about holes.
    """
    OCP = pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    block = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 2.0, 10.0, 1.0).Shape()
    solid = block
    for i in range(4):
        x0 = 0.3 + i * 0.4
        channel = BRepPrimAPI_MakeBox(gp_Pnt(x0, 0.0, 0.25), 0.1, 10.0, 0.5).Shape()
        solid = BRepAlgoAPI_Cut(solid, channel).Shape()

    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    dest = tmp_path_factory.mktemp("plate") / "channelled_plate.step"
    writer = STEPControl_Writer()
    writer.Transfer(solid, STEPControl_AsIs)
    assert writer.Write(str(dest)) == IFSelect_ReturnStatus.IFSelect_RetDone
    return dest


def test_a_channelled_plate_reads_as_an_internal_passage(channelled_plate):
    # Four channels through a plate are a passage, and before the holes on a face were summed this
    # read as a solid body in external flow with no openings at all.
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
        scale_to_metres,
    )

    interp = GeometryInterpretation(
        interpretation_id="test", owner_id="test", geometry_source_id="test",
        unit=LengthUnit.millimetre, scale_to_metres=scale_to_metres(LengthUnit.millimetre),
        basis=ResolutionBasis.file_declared, evidence="test")
    prepared = from_occ_transfer(interp, parser_applied_unit(channelled_plate))

    r = scout_cad(channelled_plate, prepared=prepared)
    assert r.flow == "internal", (
        "four channels through a plate are an internal passage, not a solid body in a flow")
    assert r.input_kind == "body-surface"
    assert len(r.openings) >= 2, "a channel array open at both ends has two ends"
    # The open area is the four channels together, not the biggest one.
    widest = max(o.area for o in r.openings)
    four_channels_m2 = 4 * (0.1e-3 * 0.5e-3)
    assert widest == pytest.approx(four_channels_m2, rel=0.05), (
        "the opening's area should be every channel on that face, not one of them")
