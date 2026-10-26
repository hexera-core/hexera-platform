"""A pin's top caps nothing, and 504 of them hid a whole chamber's coolant.

`candidate_caps` lays a flat face across every hole a flat face of a part rims, which is right for a
bore, a pipe's open end and a bolt hole. On a pin-fin plate every pin meets the chamber's ceiling,
so each pin's cross-section is an inner wire of that face and a disc gets laid across it. Measured
on the corpus part: 526 candidates, of which 504 are pin tops, and those 504 cut its one chamber
into pieces that then reach the outside past them, so no coolant was ever found in it.

WHAT SEPARATES THEM, measured on the ten corpus parts that build as CAD by stepping a quarter of
each cap's own radius along both normals and asking the solid classifier:

    part                               caps   on the box   metal both   free both
    pin_fin_300um_staggered             526           22          504          22
    embedded_silicon_D9_10um            396          396            0         396
    hotspot_array                        48           48            0          48
    manifold_two_level                   42           42            0          42
    murray_ported_4gen                   17           17            0          17
    unit_cell_100um                       2            2            0           2

504 of 526 on the one part that failed and 0 of 505 on the five that worked, and what survives on the
pin-fin plate is exactly its 22 real ports. After the fix it offers 22.

BOTH SIDES, AND NOT EITHER SIDE. A real port has air on one side and fluid on the other, so either
side would drop every port there is. Two flanges bolted face to face rim the same bore and have
fluid on both sides, and that cap has to stay. Only a cap with metal on both sides is a cap laid
across solid material.

AND NOT A PART-RELATIVE TOLERANCE. 3% of the pin-fin plate's 15.87 mm diagonal is 0.48 mm, which is
wider than the 0.3 mm of metal above its ceiling, so no bar set against the part's own size can see
this at all. The step is a quarter of the CAP's radius.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshpipeline.cad.fluid_volume import (
    CAP_SIDE_STEP,
    _Cap,
    candidate_caps,
    metal_on_both_sides,
)

#: The mini chamber, in metres. A 3 x 3 x 1 mm block with a 2.4 x 3.0 x 0.4 mm chamber cut through
#: it in y, and four pins fused back in from floor to ceiling.
BLOCK = (3e-3, 3e-3, 1e-3)
WALL, BASE, DEPTH = 0.3e-3, 0.3e-3, 0.4e-3
PIN_R = 0.2e-3
PINS = ((1.0e-3, 0.8e-3), (2.0e-3, 0.8e-3), (1.0e-3, 2.2e-3), (2.0e-3, 2.2e-3))


@pytest.fixture(scope="module")
def chamber():
    """(the solid, its diagonal): a chamber with pins standing floor to ceiling, open at both ends."""
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    x, y, z = BLOCK
    solid = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), x, y, z).Shape()
    solid = BRepAlgoAPI_Cut(solid, BRepPrimAPI_MakeBox(
        gp_Pnt(WALL, 0.0, BASE), x - 2 * WALL, y, DEPTH).Shape()).Shape()
    for px, py in PINS:
        axis = gp_Ax2(gp_Pnt(px, py, BASE), gp_Dir(0.0, 0.0, 1.0))
        solid = BRepAlgoAPI_Fuse(
            solid, BRepPrimAPI_MakeCylinder(axis, PIN_R, DEPTH).Shape()).Shape()
    return solid, math.dist((0.0, 0.0, 0.0), BLOCK)


def _solids(shape) -> list:
    from meshpipeline.cad.fluid_volume import _solids as real
    return real(shape)


def test_the_chamber_offers_only_its_two_ends(chamber):
    """Each pin meets both the floor and the ceiling, so before the fix this part offered its two
    real ends plus two caps per pin."""
    shape, diag = chamber
    caps = candidate_caps(_solids(shape), [], diag)
    assert len(caps) == 2, [
        (round(c.area * 1e6, 4), [round(v * 1e3, 3) for v in c.centroid]) for c in caps]
    # the chamber's own cross-section, 2.4 x 0.4 mm, minus nothing: the pins do not reach the ends
    for c in caps:
        assert c.area == pytest.approx((BLOCK[0] - 2 * WALL) * DEPTH, rel=1e-6), c.area
        assert abs(c.normal[1]) > 0.99, "the ends face y"


def test_a_pin_top_has_metal_on_both_sides(chamber):
    """The disc across a pin at the ceiling: the lid above it, the pin below it."""
    shape, _diag = chamber
    solids = _solids(shape)
    px, py = PINS[0]
    top = _Cap(face=None, area=math.pi * PIN_R ** 2, centroid=(px, py, BASE + DEPTH),
               normal=(0.0, 0.0, 1.0))
    assert metal_on_both_sides(solids, top) is True


def test_a_real_end_does_not(chamber):
    shape, _diag = chamber
    solids = _solids(shape)
    mid_x = BLOCK[0] / 2.0
    end = _Cap(face=None, area=(BLOCK[0] - 2 * WALL) * DEPTH,
               centroid=(mid_x, 0.0, BASE + DEPTH / 2.0), normal=(0.0, 1.0, 0.0))
    assert metal_on_both_sides(solids, end) is False


def test_a_cap_in_free_space_on_both_sides_stays(chamber):
    """The flange-bolted-face-to-face case in miniature: a cap in the middle of the chamber has
    fluid on both sides and must survive, because the split keeps the fluid on both sides of it as
    one passage."""
    shape, _diag = chamber
    solids = _solids(shape)
    inside = _Cap(face=None, area=(BLOCK[0] - 2 * WALL) * DEPTH,
                  centroid=(BLOCK[0] / 2.0, 1.5e-3, BASE + DEPTH / 2.0), normal=(0.0, 1.0, 0.0))
    assert metal_on_both_sides(solids, inside) is False


def test_a_degenerate_cap_is_never_dropped(chamber):
    shape, _diag = chamber
    zero = _Cap(face=None, area=0.0, centroid=(1e-3, 1e-3, 1e-3), normal=(0.0, 0.0, 1.0))
    assert metal_on_both_sides(_solids(shape), zero) is False


def test_the_step_is_the_caps_own_size_not_the_parts():
    """3% of the pin-fin plate's 15.87 mm diagonal is 0.48 mm, wider than the 0.3 mm of metal above
    its ceiling, so a part-relative bar cannot see this. A quarter of a 300.7 um pin top's radius
    is 37.6 um, which is inside both."""
    assert 0 < CAP_SIDE_STEP < 1.0
    pin_radius = 300.7e-6 / 2.0
    assert CAP_SIDE_STEP * pin_radius < 0.3e-3, "the step has to fit inside the metal it probes"
    assert 0.03 * 15.87e-3 > 0.3e-3, "...which the part-relative bar does not"


def test_the_chambers_coolant_is_found(chamber, tmp_path: Path):
    """The point of all of it: with the pin tops gone the chamber extracts as one passage."""
    from meshpipeline.cad.fluid_volume import extract, write_surface
    shape, _diag = chamber
    x = extract(shape)
    assert x.found, x.reason
    assert len(x.passages) == 1, [p.volume for p in x.passages]
    want = ((BLOCK[0] - 2 * WALL) * BLOCK[1] * DEPTH
            - len(PINS) * math.pi * PIN_R ** 2 * DEPTH)
    got = sum(p.volume for p in x.passages)
    assert got == pytest.approx(want, rel=0.02), (got, want)
    rec = write_surface(x, tmp_path / "fluid.stl")
    assert rec is not None and rec["open_edges"] == 0
