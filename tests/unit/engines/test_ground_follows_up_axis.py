# Responsibility: Verify the ground follows the up axis the user confirmed (INS-9): the floor face,
# the domain, the contact measurement, the extent gate and the intake's check all use it, a flow
# along the ground is never refused, and a flow into the ground is asked about in plain words.
# Boundaries: pure functions only (no mesher, no store).
"""Areen, shared dev, 2026-10-28: a Y-up file, a ground plane chosen, flow along -z (along the ground
of a Y-up file). The intake refused it: "The ground plane conflicts with flow along -z, because the
ground must be horizontal ... use a horizontal flow direction (+x, -x, +y, or -y), or remove the
ground patch". Every ground site hard-coded +z up; the stage already asks which way is up."""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.agents.intake.validation import _validate_ground_plane
from meshpipeline.engines import ground_plane as gp
from meshpipeline.engines.domain_extent_gate import _axis_margins
from meshpipeline.engines.ground_contact import measure_contact
from meshpipeline.engines.snappy import snappy_runner as R

GROUNDED = [{"name": "car", "type": "wall"}, {"name": "ground", "type": "wall"},
            {"name": "farfield", "type": "farfield"}]


@pytest.mark.parametrize("up,floor", [("+z", (2, "min")), ("-z", (2, "max")), ("+y", (1, "min")),
                                      ("-y", (1, "max")), ("+x", (0, "min")), ("", (2, "min")),
                                      (None, (2, "min")), ("sideways", (2, "min"))])
def test_the_floor_is_under_the_up_axis(up, floor):
    assert gp.floor_of(up) == floor
    if up in ("+z", "-z", "+y", "-y", "+x", "-x"):
        assert gp.up_word(floor) == up


@pytest.mark.parametrize("up", ["+z", "-z", "+y", "-y", "+x", "-x"])
def test_the_up_frame_is_a_rotation_that_comes_back(up):
    rng = np.random.default_rng(1)
    pts = rng.normal(size=(50, 3))
    f = gp.floor_of(up)
    there = gp.to_up_frame(pts, f)
    np.testing.assert_allclose(gp.from_up_frame(there, f), pts)
    # the up direction becomes +z, and it is a rotation (no mirror): det +1
    m = gp.to_up_frame(np.eye(3), f)
    assert np.isclose(np.linalg.det(m), 1.0)
    up_vec = np.zeros(3)
    up_vec["xyz".index(up[1])] = 1.0 if up[0] == "+" else -1.0
    np.testing.assert_allclose(gp.to_up_frame(up_vec, f), [0.0, 0.0, 1.0], atol=1e-12)


# a car 4 x 1.4 (tall) x 1.8, drawn Y-up, standing at y = 0.1
ANALYSIS_Y_UP = {"bbox_min": [0.0, 0.1, -0.9], "bbox_max": [4.0, 1.5, 0.9], "L": 4.0}


def test_flow_along_z_over_a_y_up_floor_is_built_with_the_floor_at_y_min():
    dmin, dmax = R.domain_from_strategy(ANALYSIS_Y_UP, {}, flow_axis="-z", ground=True, floor=(1, "min"))
    assert dmin[1] == pytest.approx(0.1)                   # the floor at the body's lowest y
    assert dmax[1] > 1.5 and dmin[2] < -0.9 and dmax[2] > 0.9


def test_a_minus_y_up_file_has_its_floor_on_the_max_side():
    a = {"bbox_min": [0.0, -1.5, -0.9], "bbox_max": [4.0, -0.1, 0.9], "L": 4.0}
    dmin, dmax = R.domain_from_strategy(a, {}, flow_axis="+x", ground=True, floor=(1, "max"))
    assert dmax[1] == pytest.approx(-0.1) and dmin[1] < -1.5


def test_flow_into_the_ground_is_still_refused_on_its_own_axis():
    with pytest.raises(ValueError, match="across y"):
        R.domain_from_strategy(ANALYSIS_Y_UP, {}, flow_axis="+y", ground=True, floor=(1, "min"))


def test_the_z_up_default_is_unchanged():
    a = {"bbox_min": [0.0, -0.9, 0.05], "bbox_max": [4.0, 0.9, 1.4], "L": 4.0}
    assert R.domain_from_strategy(a, {}, flow_axis="+x", ground=True) == \
        R.domain_from_strategy(a, {}, flow_axis="+x", ground=True, floor=(2, "min"))
    with pytest.raises(ValueError):
        R.domain_from_strategy(a, {}, flow_axis="-z", ground=True)


def _box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def test_the_contact_is_measured_along_the_up_axis():
    # a block standing on y = 0.1 in a Y-up file: seated, its floor at y = 0.1
    tris = _box((0.0, 0.1, -0.5), (2.0, 1.0, 0.5))
    c = measure_contact(gp.to_up_frame(tris, (1, "min")))
    assert c.kind == "seated" and c.floor_z == pytest.approx(0.1)
    dmin = [-5.0, -5.0, -5.0]
    R._lay_floor(dmin, [0.0, 0.1, -0.5], c, dmax=[9.0, 9.0, 9.0], bmax=[2.0, 1.0, 0.5], floor=(1, "min"))
    assert dmin[1] == pytest.approx(0.1)
    # the same block drawn -y up (standing on y = -0.1, body below it): floor on y-max
    tris = _box((0.0, -1.0, -0.5), (2.0, -0.1, 0.5))
    c = measure_contact(gp.to_up_frame(tris, (1, "max")))
    dmax = [9.0, 9.0, 9.0]
    R._lay_floor([-5.0] * 3, [0.0, -1.0, -0.5], c, dmax=dmax, bmax=[2.0, -0.1, 0.5], floor=(1, "max"))
    assert dmax[1] == pytest.approx(-0.1)


def test_the_extent_gate_seats_the_floor_under_the_up_axis():
    box = {"xmin": -8, "xmax": 20, "ymin": 0.1, "ymax": 8, "zmin": -8, "zmax": 8}
    body = {"xmin": 0, "xmax": 4, "ymin": 0.1, "ymax": 1.5, "zmin": -0.9, "zmax": 0.9}
    m = _axis_margins(box, body, 4.0, "+x", grounded=True, floor=(1, "min"))
    # y-min touches the body by design: the vertical room is the room above, not zero
    assert all(v is None or getattr(v, "margin", 1) > 0 for v in m.values())


@pytest.mark.parametrize("up,flow,refused", [
    ("+y", "-z", False),        # Areen's case: along the ground of a Y-up file
    ("+y", "+x", False),
    ("+y", "-y", True),         # into the ground
    ("+z", "-z", True),         # the old rule, still right on a Z-up file
    (None, "+z", True),
    ("-x", "+y", False),
])
def test_the_intake_refuses_only_a_flow_into_the_ground(up, flow, refused):
    errs = _validate_ground_plane(GROUNDED, flow, up)
    assert bool(errs) is refused
    if refused:
        assert "runs into the ground" in errs[0] and "Up control" in errs[0]
        assert "horizontal" not in errs[0]
