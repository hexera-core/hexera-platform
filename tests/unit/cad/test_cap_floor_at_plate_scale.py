"""A channel mouth is not discarded for being small on a large plate.

The smallest hole that may be a coolant opening was a share of the part's DIAGONAL alone, and that
does not scale to a cold plate: the part gets larger and the channels do not. On the eleven test
parts the diagonal is 5 to 38 mm, so the bar is 10 to 76 um and every channel clears it. On a
realistic 100 x 100 x 5 mm server cold plate the diagonal is 141 mm and the bar is 283 um, which
discards a 100 x 500 um channel mouth - 252 um across - and since every mouth goes the same way,
the whole extraction fails rather than one channel.

    part                                   diagonal   relative bar   bar now   a 252 um mouth
    unit_cell_100um                          32.5 mm        65.1 um   10.0 um   kept, as before
    manifold_two_level                        5.3 mm        10.6 um   10.0 um   kept, as before
    corintis baseline                        38.1 mm        76.2 um   10.0 um   kept, as before
    a server cold plate (100 x 100 x 5)     141.5 mm       283.0 um   10.0 um   KEPT, was discarded
    a big one (300 x 300 x 10)              424.4 mm       848.8 um   10.0 um   KEPT, was discarded
    a one-metre part                       1415.1 mm      2830.2 um   10.0 um   KEPT, was discarded

MEASURED on the eleven-part corpus after the change: every one identical - the same five writing a
surface, the same volumes to four decimals, the same open-edge counts and the same bounding boxes.
So this is a gain at plate scale and a no-op at ours.
"""
from __future__ import annotations

import math

import pytest

from meshpipeline.cad.fluid_volume import MAX_CAP_FLOOR_M, MIN_CAP, cap_floor_m

#: 100 x 500 um, the channel the old rule discarded on a server-scale plate
MOUTH_M = math.sqrt(4.0 * 100e-6 * 500e-6 / math.pi)


def _diag(*side_m: float) -> float:
    return math.dist((0.0, 0.0, 0.0), side_m)


# #
# the bar itself
# #
def test_a_small_part_keeps_the_bar_it_had_or_a_lower_one():
    # The relative rule still applies where it is the tighter of the two, so nothing on a part of
    # corpus size becomes harder to find.
    for side in ((0.0002, 0.0325, 0.0013), (0.0033, 0.0040, 0.00105), (0.0199, 0.0325, 0.0011)):
        d = _diag(*side)
        assert cap_floor_m(d) <= MIN_CAP * d


def test_the_bar_never_rises_above_ten_microns_however_large_the_part():
    for side in ((0.100, 0.100, 0.005), (0.300, 0.300, 0.010), (1.0, 1.0, 0.05), (10.0, 10.0, 1.0)):
        assert cap_floor_m(_diag(*side)) == MAX_CAP_FLOOR_M
    assert MAX_CAP_FLOOR_M == pytest.approx(10e-6)


def test_the_channel_the_old_rule_threw_away_is_kept_now():
    plate = _diag(0.100, 0.100, 0.005)
    assert MOUTH_M < MIN_CAP * plate, "252 um is under the old 283 um bar: that was the defect"
    assert MOUTH_M > cap_floor_m(plate), "...and over the bar now"


def test_the_finest_passage_in_the_corpus_still_clears_it():
    # embedded_silicon_D9_10um has 10 um features, so the bar has to sit below that at any size.
    ten_micron_square = math.sqrt(4.0 * 10e-6 * 10e-6 / math.pi)
    assert ten_micron_square > MAX_CAP_FLOOR_M


# #
# and on a plate built at that scale
# #
@pytest.fixture(scope="module")
def server_plate():
    """A 100 x 100 x 5 mm plate with twelve 100 x 500 um channels through it, in METRES.

    Built at the scale the rule failed at rather than scaled down, because the whole defect is
    that the bar moved with the part's size and the channels did not.
    """
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    body = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 0.100, 0.100, 0.005).Shape()
    for i in range(12):
        body = BRepAlgoAPI_Cut(body, BRepPrimAPI_MakeBox(
            gp_Pnt(0.004 + i * 0.0075, 0.0, 0.00225), 0.0001, 0.100, 0.0005).Shape()).Shape()
    return body


def test_every_channel_end_is_a_cap_candidate(server_plate):
    from meshpipeline.cad.fluid_volume import _real_box, _solids, candidate_caps
    lo, hi = _real_box(server_plate)
    caps = candidate_caps(_solids(server_plate), [], math.dist(lo, hi))
    assert len(caps) == 24, "both ends of all twelve channels"


def test_the_coolant_of_a_server_scale_plate_comes_out_exactly(server_plate):
    from meshpipeline.cad.fluid_volume import extract
    x = extract(server_plate)
    assert x.found, x.reason
    assert len(x.passages) == 12
    built_mm3 = 12 * 0.1 * 0.5 * 100.0
    assert sum(p.volume for p in x.passages) * 1e9 == pytest.approx(built_mm3, rel=1e-6)


def test_it_writes_a_closed_surface_at_that_scale(server_plate, tmp_path):
    from meshpipeline.cad.fluid_volume import extract, write_surface
    rec = write_surface(extract(server_plate), tmp_path / "plate_fluid.stl")
    assert rec is not None
    assert rec["open_edges"] == 0 and rec["inconsistent_edges"] == 0
    assert rec["passages"] == 12
    assert rec["surface_volume_mm3"] == pytest.approx(12 * 0.1 * 0.5 * 100.0, rel=1e-3)
