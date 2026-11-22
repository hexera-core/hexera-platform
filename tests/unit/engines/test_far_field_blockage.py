# Responsibility: Verify PLAN-cfd-6 - an external far field's cross-section is widened until the body blocks at most 3% of it, the planned margins kept as minimums, a floor or a symmetry plane never moved, a body already within it unchanged, and the extent gate never calls the wider box a defect.
# Boundaries: pure over plain boxes and triangles; no mesher is run.
from __future__ import annotations

import pytest

from meshpipeline.engines import far_field as FF
from meshpipeline.engines.domain_extent_gate import check_domain_extents
from meshpipeline.engines.snappy import snappy_runner as R


def _disc(d: float, t: float) -> list:
    """A square plate d x d facing +x, t thick (two big faces, four thin ones): frontal area d^2 along x."""
    x0, x1, h = 0.0, t, d / 2
    q = [((x0, -h, -h), (x0, h, -h), (x0, h, h)), ((x0, -h, -h), (x0, h, h), (x0, -h, h)),
         ((x1, -h, -h), (x1, h, h), (x1, h, -h)), ((x1, -h, -h), (x1, -h, h), (x1, h, h))]
    return q


def _analysis(d: float, t: float) -> dict:
    return {"bbox_min": [0.0, -d / 2, -d / 2], "bbox_max": [t, d / 2, d / 2], "L": d}


def test_projected_areas_are_half_the_summed_projection():
    a = FF.projected_areas(_disc(0.3, 0.06))
    assert a[0] == pytest.approx(0.09) and a[1] == pytest.approx(0.0) and a[2] == pytest.approx(0.0)


def test_a_wide_flat_body_gets_a_wider_far_field():
    # the CFD board's abc 00050662: 60 mm along the flow, 307 x 307 across; 5 / 10 / 5 lengths gave 9.8% blockage
    an = _analysis(0.307, 0.06)
    plain = R.domain_from_strategy(an, {"domain_margin": {"up": 5, "down": 10, "side": 5, "vert": 5}},
                                   flow_axis="+x", ruler_m=0.06)
    sec0 = (plain[1][1] - plain[0][1]) * (plain[1][2] - plain[0][2])
    assert 0.307 ** 2 / sec0 > 0.09
    rec: dict = {}
    dmin, dmax = R.domain_from_strategy(an, {"domain_margin": {"up": 5, "down": 10, "side": 5, "vert": 5}},
                                        flow_axis="+x", ruler_m=0.06, frontal_m2=[0.307 ** 2, 0.0, 0.0],
                                        blockage=rec)
    sec = (dmax[1] - dmin[1]) * (dmax[2] - dmin[2])
    assert 0.307 ** 2 / sec <= FF.BLOCKAGE_MAX
    assert rec["blockage_before"] > 0.09 and rec["blockage"] <= FF.BLOCKAGE_MAX and rec["grown_rulers"] > 0
    # the margins asked are kept as the minimum; the streamwise ones are untouched
    assert dmin[0] == pytest.approx(plain[0][0]) and dmax[0] == pytest.approx(plain[1][0])
    for j in (1, 2):
        assert dmin[j] < plain[0][j] and dmax[j] > plain[1][j]
    assert "widened" in FF.blockage_words(rec) and "at most 3%" in FF.blockage_words(rec)


def test_a_slender_body_keeps_its_box():
    an = {"bbox_min": [0.0, -0.05, -0.05], "bbox_max": [1.0, 0.05, 0.05], "L": 1.0}
    rec: dict = {}
    got = R.domain_from_strategy(an, {"domain_margin": {"up": 5, "down": 10, "side": 5, "vert": 5}}, flow_axis="+x",
                                 frontal_m2=[0.01, 0.1, 0.1], blockage=rec)
    plain = R.domain_from_strategy(an, {"domain_margin": {"up": 5, "down": 10, "side": 5, "vert": 5}}, flow_axis="+x")
    assert got == plain and rec == {}


def test_the_floor_and_a_symmetry_plane_never_move():
    dmin, dmax = [-1.0, -0.5, 0.0], [3.0, 0.5, 1.0]          # a grounded body: the floor is z-min
    rec = FF.grow_for_blockage(dmin, dmax, 0, 0.2, fixed={(2, "min")})
    assert rec is not None and dmin[2] == 0.0 and dmax[2] > 1.0 and dmin[1] < -0.5 and dmax[1] > 0.5
    assert 0.2 / ((dmax[1] - dmin[1]) * (dmax[2] - dmin[2])) <= FF.BLOCKAGE_MAX
    # nothing free: nothing to grow
    assert FF.grow_for_blockage([0, 0, 0], [1, 1, 1], 0, 0.5,
                                fixed={(1, "min"), (1, "max"), (2, "min"), (2, "max")}) is None


def test_the_prose_extent_gate_takes_more_room_to_the_side():
    geom = {"geometry": {"domain_box": {"xmin": -5, "xmax": 11, "ymin": -9, "ymax": 10, "zmin": -9, "zmax": 10},
                         "body_box": {"xmin": 0, "xmax": 1, "ymin": 0, "ymax": 1, "zmin": 0, "zmax": 1},
                         "reference_length": 1.0}}
    ok, why = check_domain_extents({"upstream": 5, "downstream": 10, "lateral": 5}, geom)
    assert ok, why
    geom["geometry"]["domain_box"]["ymin"] = -3                # less room than asked is still a miss
    assert not check_domain_extents({"upstream": 5, "downstream": 10, "lateral": 5}, geom)[0]
