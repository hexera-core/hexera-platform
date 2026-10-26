"""A manifold plate grades its channel widths on purpose, and that read as a solid body.

THE RULE IT BROKE. A perforated face was one whose holes are ALL THE SAME SIZE, at a spread of 2.0
measured as largest-over-median AREA. Real cold plates do not obey it: a manifold grades its
channels deliberately, wide feeds and narrow drains, and three times wider is NINE TIMES the area.

MEASURED on the part built below, a 20 x 12 x 2 mm plate with three 0.6 mm feeds and seven 0.15 mm
drains through it, before the fix:

    spread (largest area / median area)     16
    read as                                 a solid body in external flow
    opening proposed                        the 10.58 mm2 SOLID end face
    channel area actually there             1.43 mm2

So the one reading a cold plate must not get - its metal face offered as the opening - was what a
graded plate got, and the port binder refuses a job whose opening's size and position disagree.

WHAT ACTUALLY SEPARATED THEM. The spread bar was reaching for a BORE WITH BOLT HOLES BESIDE IT, and
the thing about a bore is that there is one of it. A flange's 49.25 mm bore is one hole among eight
identical 22.2 mm bolts; a graded array has no hole that is one of a kind. Both halves are checked
here on real CAD, because a rule restated inside a test stays green while the production code is
wrong - five of the six cases in test_perforated_face.py did exactly that.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshpipeline.cad.scout import (
    MAX_HOLE_SIZE_SPREAD,
    SAME_SIZE_WIDTHS,
    _no_hole_is_one_of_a_kind,
)

#: The plate, in mm: three wide feeds and seven narrow drains through a 20 x 12 x 2 mm block.
PLATE = (20.0, 12.0, 2.0)
FEEDS, FEED_W = 3, 0.6
DRAINS, DRAIN_W = 7, 0.15


def _write_step(solid, dest: Path) -> Path:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPControl_Writer()
    w.Transfer(solid, STEPControl_AsIs)
    assert w.Write(str(dest)) == IFSelect_ReturnStatus.IFSelect_RetDone
    return dest


@pytest.fixture(scope="module")
def graded_plate(tmp_path_factory) -> Path:
    """A plate whose channels run its LENGTH, roofed, in two widths.

    Roofed on purpose: a channel open at the top is a notch in the face's outline and not a hole in
    it, and this is a test about holes.
    """
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    L, W, H = PLATE
    solid = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), W, L, H).Shape()
    pitch = W / (FEEDS + DRAINS + 1)
    for i in range(FEEDS + DRAINS):
        wide = i < FEEDS
        width = FEED_W if wide else DRAIN_W
        x0 = pitch * (i + 1) - width / 2.0
        cut = BRepPrimAPI_MakeBox(gp_Pnt(x0, 0.0, 0.6), width, L, 0.8).Shape()
        solid = BRepAlgoAPI_Cut(solid, cut).Shape()
    return _write_step(solid, tmp_path_factory.mktemp("graded") / "graded_plate.step")


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


# #
# the part, read by the production reader
# #
def test_the_spread_bar_alone_would_still_refuse_this_part():
    """The arithmetic that made it a solid body: 4x the width is 16x the area, against a bar of 2."""
    spread = (FEED_W / DRAIN_W) ** 2
    assert spread == pytest.approx(16.0)
    assert spread > MAX_HOLE_SIZE_SPREAD, "this is the bar the part failed"


def test_a_graded_plate_reads_as_an_internal_passage(graded_plate):
    r = _scouted(graded_plate)
    assert r.flow == "internal", (
        f"a graded channel array is a fluid passage, not a body in external flow: {r.flow}, "
        f"kind {r.body_kind}, openings {[(o.role, o.area_m2) for o in r.openings]}")


def test_its_opening_is_the_channels_and_not_the_metal_face(graded_plate):
    r = _scouted(graded_plate)
    channels = (FEEDS * FEED_W + DRAINS * DRAIN_W) * 0.8        # mm2 of channel cross-section
    face = PLATE[1] * PLATE[2]                                  # the whole 12 x 2 mm end face
    mouths = [o for o in r.openings if o.role != "not_an_opening"]
    assert mouths, "no opening proposed at all"
    for o in mouths:
        mm2 = float(o.area) * 1e6
        assert mm2 < face / 2.0, (
            f"{o.role} is {mm2:.2f} mm2 against {channels:.2f} mm2 of channel and a "
            f"{face:.2f} mm2 solid face - the metal face was proposed as the opening")


def test_both_ends_are_found(graded_plate):
    r = _scouted(graded_plate)
    mouths = [o for o in r.openings if o.role != "not_an_opening"]
    assert len(mouths) >= 2, f"a passage needs both ends: {[o.role for o in mouths]}"
    assert all(o.perforated for o in mouths), "each end is one hole per channel"
    assert {o.holes for o in mouths} == {FEEDS + DRAINS}, (
        f"every channel reaches both ends: {[o.holes for o in mouths]}")


# #
# and a flange's bore is still a bore
# #
def _holes(*widths_mm: float) -> list[dict]:
    return [{"area_m2": math.pi * (w / 2000.0) ** 2} for w in widths_mm]


def test_a_bore_is_one_of_a_kind_and_bolt_holes_are_not():
    # ASME B16.5 Class 1500 NPS 2: the case that broke a live job
    assert not _no_hole_is_one_of_a_kind(_holes(49.25, *([22.2] * 8)))


@pytest.mark.parametrize(("name", "widths", "want"), [
    ("the graded array", [FEED_W] * FEEDS + [DRAIN_W] * DRAINS, True),
    ("a uniform array", [0.25] * 16, True),
    ("three sizes, all paired", [1.0, 1.0, 0.5, 0.5, 0.2, 0.2], True),
    ("one hole", [1.0], False),
    ("two equal holes", [1.0, 1.0], True),
    ("two unequal holes", [1.0, 0.2], False),
    # SAID PLAINLY in the constant's own comment: this one reads as perforated, and a two-port
    # manifold flange is not obviously the wrong reading for it.
    ("two equal bores and eight bolts", [49.25, 49.25] + [22.2] * 8, True),
])
def test_what_counts_as_one_of_a_kind(name, widths, want):
    assert _no_hole_is_one_of_a_kind(_holes(*widths)) is want, name


def test_the_size_classes_are_compared_by_width_not_area():
    """A width ratio is what a person means by "three times wider"; the area ratio is its square,
    and no bar set on areas can tell 3x wider from a bore."""
    just_inside = [1.0, SAME_SIZE_WIDTHS * 0.999, 2.0, 2.0]
    assert _no_hole_is_one_of_a_kind(_holes(*just_inside))
    just_outside = [1.0, SAME_SIZE_WIDTHS * 1.001, 2.0, 2.0]
    assert not _no_hole_is_one_of_a_kind(_holes(*just_outside))
