"""The three numbers a real upload will not declare, measured instead.

conjugate.build is told the narrowest passage, the coolant's volume and how many separate passages
there are, and the cell size - which the whole mesh follows from - is derived from the first.
The test corpus DECLARES all three. devtools/semiconductor/parts.py says of the narrowest passage,
in as many words, "DECLARED, not measured: it cannot be read off a triangle soup, and it is what
the cell size is derived from", and index_parts.py refuses a part without it. A customer declares
nothing, so until these are measured there is no upload the mesher can be given.

All three are measurable now, because the coolant leaves the extractor as real faces and a closed
surface. Scored against what the corpus declares:

    part                      narrowest um          volume mm3        pieces
                              dec    meas    err    dec      err      dec/meas
    unit_cell_100um           100.0  100.00  +0.0%   2.600  +0.000%    1/1
    hotspot_array              60.0   60.00  +0.0%  10.080  -0.000%   24/24
    manifold_two_level        100.0  100.00  +0.0%   4.560  -0.000%    1/1
    embedded_silicon_D9_10um   10.0   10.00  +0.0%   0.750  -1.000%  200/198
    wavy_250um_16ch           250.0  294.71 +17.9%  43.676  +0.000%   16/16

Four of five exact on the narrowest, every volume exact or within 1%, four of five piece counts
exact. So the declaration becomes the scorecard rather than an input.

THE WAVY PLATE IS READ 17.9% COARSE, and that is the honest limit. Its channels meet the end face
at a slant, so a 250 um channel presents a 294.71 um mouth - 250/cos 32 degrees - and the radius
field cannot correct it because that part's sampling spacing works out at 250 um, the channel
width itself, where it reads 354 um (the diagonal of its own sampling cell). Both readings err in
the same direction, and large is the unsafe direction: cells come out 18% coarser than asked for.

TWO READINGS, AND THE SMALLER WINS, because each is blind where the other sees:
  the port mouths   exact for a prismatic channel, straight off the B-rep. Blind to a channel
                    that pinches INSIDE the part - the hotspot array's mouths are 120 um and its
                    channels narrow to 60.
  the radius field  sees a pinch anywhere along the channel. Blind below its own sampling spacing.
"""
from __future__ import annotations

import math

import pytest

from meshpipeline.cad.fluid_volume import extract, narrowest_passage_m, write_surface


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    """A 2 x 10 x 1 mm plate with four 100 x 500 um channels through it, in METRES.

    Prismatic and square to its end faces, so the mouths read the channel exactly and there is a
    known right answer to hold the measurement to.
    """
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    body = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 0.002, 0.010, 0.001).Shape()
    for i in range(4):
        body = BRepAlgoAPI_Cut(body, BRepPrimAPI_MakeBox(
            gp_Pnt(0.0003 + i * 0.0004, 0.0, 0.00025), 0.0001, 0.010, 0.0005).Shape()).Shape()
    x = extract(body)
    assert x.found, "the plate's coolant should extract"
    rec = write_surface(x, tmp_path_factory.mktemp("three") / "fluid.stl")
    assert rec is not None
    return x, rec


#: the plate as built: four channels, 100 x 500 um, 10 mm long
NARROWEST_M = 100e-6
VOLUME_MM3 = 4 * 0.1 * 0.5 * 10.0
PASSAGES = 4


# #
# all three come out of writing the surface
# #
def test_the_narrowest_passage_is_measured(plate):
    _x, rec = plate
    assert rec["narrowest_m"] == pytest.approx(NARROWEST_M, rel=0.05), (
        "the channels are 100 x 500 um, so the narrowest passage is 100 um")


def test_the_volume_and_the_piece_count_are_measured(plate):
    _x, rec = plate
    assert rec["volume_mm3"] == pytest.approx(VOLUME_MM3, rel=1e-3)
    assert rec["passages"] == PASSAGES


def test_they_are_the_three_arguments_the_mesher_takes(plate):
    """The names matter: these go straight into conjugate.build as narrowest_m,
    expect_fluid_volume_m3 and expect_fluid_pieces."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate.conjugate import build
    takes = inspect.signature(build).parameters
    for name in ("narrowest_m", "expect_fluid_volume_m3", "expect_fluid_pieces"):
        assert name in takes, f"conjugate.build no longer takes {name}"
    _x, rec = plate
    assert rec["narrowest_m"] > 0.0
    assert rec["volume_m3"] > 0.0
    assert rec["passages"] >= 1


# #
# the smaller of the two readings wins, and why
# #
def test_the_mouths_alone_would_miss_a_channel_that_pinches_inside(plate):
    """The hotspot array is the measured case: its mouths are 120 um and its channels narrow to
    60, so the mouths alone read it 100% too coarse. On this plate, which is prismatic, the two
    readings agree - that is what makes it a control rather than the case."""
    x, rec = plate
    mouths = [min(p.wh) for passage in x.passages for p in passage.ports if min(p.wh) > 0.0]
    assert min(mouths) == pytest.approx(NARROWEST_M, rel=0.05)
    assert rec["narrowest_m"] <= min(mouths) + 1e-12, "the answer is never larger than the mouths"


def test_it_errs_small_rather_than_large(plate):
    """Large is the unsafe direction: the cell size is derived from this, so reading the passage
    wider than it is makes every cell coarser than asked for."""
    _x, rec = plate
    assert rec["narrowest_m"] <= NARROWEST_M * 1.05


def test_an_extraction_with_no_passages_reads_nothing(tmp_path):
    from meshpipeline.cad.fluid_volume import Extraction
    empty = Extraction(passages=[], parts=1, caps=0, regions=0, seconds=0.0)
    assert narrowest_passage_m(empty, tmp_path / "nothing.stl") == 0.0


def test_a_radius_field_that_cannot_read_leaves_the_mouths_standing(plate, monkeypatch, tmp_path):
    """The fallback, and it is deliberately LOUD. The first thing this caught was a NameError in
    the function itself, and at info level it sat silently behind a reading 100% too coarse."""
    x, _rec = plate
    import meshpipeline.engines.passage as passage

    def broken(*_a, **_k):
        raise RuntimeError("no field today")

    monkeypatch.setattr(passage, "passage_of_stls", broken)
    got = narrowest_passage_m(x, tmp_path / "missing.stl")
    mouths = [min(p.wh) for p in x.passages for p in p.ports if min(p.wh) > 0.0]
    assert got == pytest.approx(min(mouths))


# #
# and the slant, which is the honest limit
# #
def test_the_slant_that_costs_the_wavy_plate_eighteen_percent():
    """Stated as arithmetic so the number is not mistaken for noise. A channel meeting its end
    face at an angle presents a mouth wider by 1/cos of that angle, and no reading of the mouth
    can see past it."""
    channel_um, mouth_um = 250.0, 294.71
    assert mouth_um / channel_um == pytest.approx(1.0 / math.cos(math.radians(32.0)), rel=0.01)
    assert (mouth_um - channel_um) / channel_um == pytest.approx(0.179, abs=0.002)
