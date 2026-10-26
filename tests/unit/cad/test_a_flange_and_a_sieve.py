"""A flange's bore is its opening, and a sieve is not a cold plate.

Two things the perforated-face reading got wrong, both found by a review agent and both measured
here on parts built to real dimensions.

A FLANGED PIPE SPOOL'S INLET READ 3.1x TOO BIG. The rule for "this face is perforated rather than
bored" was the share of the hole area the largest hole takes, at 50%, and that was calibrated on
the wrong example: a flange's bore takes 93% of it on a low-pressure class but only 38% on ASME
B16.5 Class 1500, where eight 22.2 mm bolts outweigh a 49.25 mm bore. At 38% the face read as
perforated and the inlet came out as the SUM of the bore and all eight bolt holes - 5932 mm2
against the bore's 1905, with the whole 165 mm flange face as its extent instead of the 49 mm
bore. The port binder refuses a job whose opening's size and position disagree, so this broke live
jobs on the path that already has customers.

What actually separates them is that A CHANNEL ARRAY'S HOLES ARE ALL THE SAME SIZE. Measured
largest-over-median: the corpus plates are 1.0, that flange 4.9.

A SIEVE READ AS A COOLED SOLID. A 300 x 300 x 3 mm screen with a hundred holes has one hole per
passage at each end and the same count at both, so the hole-count pairing could not tell it from a
channel array: it read "pipe_wall / body-surface / internal" at confidence 0.9, and that let the
stage offer it as a solid with coolant passages - a route into the conjugate mesher for a sieve.

What separates those is WHICH WAY THE HOLES RUN. A coolant channel runs the length of a plate; a
sieve's holes pierce its thickness. The mouth's own normal says which.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshpipeline.cad.scout import (
    MAX_DOMINANT_HOLE_SHARE,
    MAX_HOLE_SIZE_SPREAD,
    _runs_along_the_part,
)

#: ASME B16.5 Class 1500 NPS 2, in mm: the case that broke
BORE_D, FLANGE_D, FLANGE_T, BOLT_D, CIRCLE_D, N_BOLTS = 49.25, 165.1, 30.2, 22.2, 127.0, 8
PIPE_LEN, PIPE_OD = 300.0, 60.3
BORE_MM2 = math.pi * (BORE_D / 2.0) ** 2


def _scouted(path: Path):
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
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=scale_to_metres(LengthUnit.millimetre),
        basis=ResolutionBasis.file_declared, evidence="t")
    return scout_cad(path, prepared=from_occ_transfer(interp, parser_applied_unit(path)))


def _write(shape, dest: Path) -> Path:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    assert w.Write(str(dest)) == IFSelect_ReturnStatus.IFSelect_RetDone
    return dest


# #
# the arithmetic the flange case turns on
# #
def test_a_high_pressure_flange_is_not_separated_by_the_share_of_hole_area():
    """Why the share bar alone was not enough, on the real numbers."""
    bolts_mm2 = N_BOLTS * math.pi * (BOLT_D / 2.0) ** 2
    share = BORE_MM2 / (BORE_MM2 + bolts_mm2)
    assert share == pytest.approx(0.38, abs=0.01)
    assert share <= MAX_DOMINANT_HOLE_SHARE, "which is why the face read as perforated"


def test_but_the_sizes_separate_them_cleanly():
    # largest over median: a bore beside eight equal bolts has the bore as the largest and a bolt
    # as the median; a channel array has them equal.
    bolt_mm2 = math.pi * (BOLT_D / 2.0) ** 2
    assert BORE_MM2 / bolt_mm2 == pytest.approx(4.9, abs=0.1)
    assert BORE_MM2 / bolt_mm2 > MAX_HOLE_SIZE_SPREAD
    assert 1.0 <= MAX_HOLE_SIZE_SPREAD, "identical channels must stay perforated"


# #
# ...and the flange itself
# #
@pytest.fixture(scope="module")
def spool(tmp_path_factory) -> Path:
    """A length of pipe with an ASME Class 1500 flange at each end, bored through, bolt holes and
    all. Built at the real dimensions, because the dimensions are the bug."""
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    def z_cyl(x, y, z, r, h):
        return BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0.0, 0.0, 1.0)), r, h).Shape()

    body = z_cyl(0, 0, 0, PIPE_OD / 2.0, PIPE_LEN)
    for z in (0.0, PIPE_LEN - FLANGE_T):
        body = BRepAlgoAPI_Fuse(body, z_cyl(0, 0, z, FLANGE_D / 2.0, FLANGE_T)).Shape()
    body = BRepAlgoAPI_Cut(body, z_cyl(0, 0, -1.0, BORE_D / 2.0, PIPE_LEN + 2.0)).Shape()
    for k in range(N_BOLTS):
        a = 2.0 * math.pi * k / N_BOLTS
        x, y = (CIRCLE_D / 2.0) * math.cos(a), (CIRCLE_D / 2.0) * math.sin(a)
        for z in (-1.0, PIPE_LEN - FLANGE_T - 1.0):
            body = BRepAlgoAPI_Cut(body, z_cyl(x, y, z, BOLT_D / 2.0, FLANGE_T + 2.0)).Shape()
    return _write(body, tmp_path_factory.mktemp("spool") / "class1500_nps2.step")


def test_the_inlet_is_the_bore_and_not_the_bolt_holes_as_well(spool):
    r = _scouted(spool)
    assert r.flow == "internal" and len(r.openings) == 2
    for o in r.openings:
        mm2 = 1e6 * o.area
        assert mm2 == pytest.approx(BORE_MM2, rel=0.02), (
            f"the inlet should be the {BORE_MM2:.0f} mm2 bore; it read {mm2:.0f}")
        assert not o.perforated, "a bore beside bolt holes is a bore"


def test_the_inlets_extent_is_the_bore_and_not_the_flange_face(spool):
    # The binder refuses a job whose opening's size and position disagree, and the extent was the
    # whole 165 mm flange face.
    for o in _scouted(spool).openings:
        across = 1000.0 * max(o.wh)
        assert across == pytest.approx(BORE_D, rel=0.05), (
            f"the extent should be the {BORE_D} mm bore; it read {across:.0f} mm")


# #
# a sieve
# #
@pytest.fixture(scope="module")
def screen(tmp_path_factory) -> Path:
    """A 300 x 300 x 3 mm screen with a 10 x 10 grid of 10 mm square through-holes."""
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    body = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 300.0, 300.0, 3.0).Shape()
    for iy in range(10):
        for ix in range(10):
            body = BRepAlgoAPI_Cut(body, BRepPrimAPI_MakeBox(
                gp_Pnt(20.0 + ix * 28.0, 20.0 + iy * 28.0, -1.0), 10.0, 10.0, 5.0).Shape()).Shape()
    return _write(body, tmp_path_factory.mktemp("screen") / "sieve_300x300x3.step")


def test_a_sieve_is_a_body_in_a_flow_not_a_passage(screen):
    r = _scouted(screen)
    assert r.flow == "external", f"a screen is not an internal passage, got {r.flow}"
    assert r.body_kind == "single_solid"


def test_and_the_stage_may_not_offer_a_sieve_as_a_cooled_solid(screen):
    from meshpipeline.application.geometry_check import _kinds_on_offer
    r = _scouted(screen)
    facts = {"flow": r.flow, "input_kind": r.input_kind, "body_kind": r.body_kind}
    proposal = {"openings": [{"id": i} for i in range(len(r.openings))]}
    assert "solid-assembly" not in _kinds_on_offer(facts, proposal)


# #
# the rule that separates them, on its own
# #
class _Mouth:
    def __init__(self, normal):
        self.normal = normal


@pytest.mark.parametrize(("sides", "normal", "along", "what"), [
    # the single-channel plate: 0.2 x 32.5 x 1.3 mm, its channel down the 32.5
    ((0.0002, 0.0325, 0.0013), (0.0, 1.0, 0.0), True, "a cold plate's channel"),
    # the same plate, a mouth facing its thinnest side
    ((0.0002, 0.0325, 0.0013), (1.0, 0.0, 0.0), False, "through the plate's thickness"),
    # a 300 x 300 x 3 mm screen: its holes pierce the 3 mm
    ((0.300, 0.300, 0.003), (0.0, 0.0, 1.0), False, "a sieve's holes"),
    ((0.300, 0.300, 0.003), (1.0, 0.0, 0.0), True, "along a screen's face"),
])
def test_which_way_the_holes_run(sides, normal, along, what):
    assert _runs_along_the_part(_Mouth(normal), sides) is along, what


def test_a_nearly_axial_normal_is_read_by_its_largest_part():
    # a mouth tilted a few degrees still faces the axis it mostly faces
    sides = (0.0002, 0.0325, 0.0013)
    assert _runs_along_the_part(_Mouth((0.08, 0.99, 0.05)), sides) is True
    assert _runs_along_the_part(_Mouth((0.99, 0.08, 0.05)), sides) is False
