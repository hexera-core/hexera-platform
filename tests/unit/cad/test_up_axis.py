# Responsibility: Verify the code's reading of which way a part stands up (cad/up_axis) on parts
# built on paper: a car body on struts drawn upside down, a car on wheels, an aircraft with and
# without its landing gear, a sting-mounted model, a pipe, a rotor and a block of buildings - each
# turned to all six ways up.
# Boundaries: numpy triangles only; no file, no model, no storage.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.stl_io import _box_triangles
from meshpipeline.cad.up_axis import UP_AXES, opposite, read_ends, read_up, up_from_ends

#: Proper rotations taking +Z to each axis (never a mirror) - the console's own Up turns - so a part
#: built the right way up can be drawn any way up and the answer is known.
TURN = {
    "+z": lambda p: p,
    "-z": lambda p: np.stack([p[..., 0], -p[..., 1], -p[..., 2]], -1),
    "+y": lambda p: np.stack([p[..., 0], p[..., 2], -p[..., 1]], -1),
    "-y": lambda p: np.stack([p[..., 0], -p[..., 2], p[..., 1]], -1),
    "+x": lambda p: np.stack([p[..., 2], p[..., 1], -p[..., 0]], -1),
    "-x": lambda p: np.stack([-p[..., 2], p[..., 1], p[..., 0]], -1),
}


def _box(lo, hi) -> np.ndarray:
    return np.asarray(_box_triangles(lo, hi), dtype=float)


def _cylinder(base, axis: int, radius: float, length: float, n: int = 16) -> np.ndarray:
    """A closed cylinder from `base` along +axis."""
    a, b = [k for k in range(3) if k != axis]
    tris = []
    for i in range(n):
        t0, t1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p = []
        for t in (t0, t1):
            for h in (0.0, length):
                q = list(base)
                q[a] += radius * math.cos(t)
                q[b] += radius * math.sin(t)
                q[axis] += h
                p.append(q)
        (a0, a1), (b0, b1) = (p[0], p[1]), (p[2], p[3])
        tris += [(a0, b0, b1), (a0, b1, a1)]
        lo, hi = list(base), list(base)
        hi[axis] += length
        tris += [(lo, b0, a0), (hi, a1, b1)]
    return np.asarray(tris, dtype=float)


def _car_on_struts() -> np.ndarray:
    """The SAE body's shape, the right way up: a block with four thin mounting struts under it."""
    parts = [_box((-0.42, -0.16, 0.03), (0.42, 0.16, 0.30))]
    for x in (-0.25, 0.25):
        for y in (-0.10, 0.10):
            parts.append(_cylinder((x, y, 0.0), 2, 0.005, 0.03))
    return np.concatenate(parts)


def _car_on_wheels() -> np.ndarray:
    """A car body on four wheels whose tyres reach the ground."""
    parts = [_box((-2.3, -0.9, 0.25), (2.3, 0.9, 1.40))]
    for x in (-1.4, 1.4):
        for y in (-0.8, 0.6):
            parts.append(_cylinder((x, y, 0.32), 1, 0.32, 0.2, n=24))
    return np.concatenate(parts)


def _aircraft(gear: bool) -> np.ndarray:
    """A fuselage along x, a low wing across y, a fin on top at the back - and, if asked, a nose
    gear and two main legs reaching down to the ground."""
    parts = [_cylinder((0.0, 0.0, 1.0), 0, 0.4, 8.0, n=24),
             _box((3.0, -4.5, 0.62), (4.6, 4.5, 0.72)),
             _box((7.0, -0.04, 1.3), (8.0, 0.04, 2.8))]
    if gear:
        parts += [_cylinder((0.8, 0.0, 0.0), 2, 0.06, 0.62),
                  _cylinder((4.2, -1.2, 0.0), 2, 0.08, 0.62),
                  _cylinder((4.2, 1.2, 0.0), 2, 0.08, 0.62)]
    return np.concatenate(parts)


def _sting_model() -> np.ndarray:
    """A wind-tunnel model held from behind by one sting: one thing reaches the far end, not three."""
    return np.concatenate([_box((0.0, -0.1, -0.1), (1.0, 0.1, 0.1)),
                           _cylinder((1.0, 0.0, 0.0), 0, 0.02, 0.6)])


def _pipe() -> np.ndarray:
    """A pipe wall: an outer and an inner tube with open ends."""
    return np.concatenate([_cylinder((0.0, 0.0, 0.0), 0, 0.10, 1.0, n=32),
                           _cylinder((0.0, 0.0, 0.0), 0, 0.09, 1.0, n=32)])


def _rotor() -> np.ndarray:
    """A hub along z with three flat blades: both faces of the disc look alike."""
    parts = [_cylinder((0.0, 0.0, -0.05), 2, 0.08, 0.10)]
    for i in range(3):
        t = 2 * math.pi * i / 3
        blade = _box((0.08, -0.03, -0.01), (0.6, 0.03, 0.01))
        c, s = math.cos(t), math.sin(t)
        rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        parts.append(blade @ rot.T)
    return np.concatenate(parts)


def _city_block() -> np.ndarray:
    """Five separate buildings of different heights standing on one ground."""
    heights = (20.0, 35.0, 12.0, 50.0, 28.0)
    return np.concatenate([_box((30.0 * i, (i % 2) * 25.0, 0.0), (30.0 * i + 18.0, (i % 2) * 25.0 + 15.0, h))
                           for i, h in enumerate(heights)])


@pytest.mark.parametrize("up", UP_AXES)
def test_a_car_body_on_struts_is_read_the_right_way_up_however_it_was_drawn(up):
    reading = read_up(TURN[up](_car_on_struts()))
    assert reading.axis == up
    assert reading.confidence >= 0.75 and "feet" in reading.reason


def test_the_sae_case_struts_at_plus_z_means_up_is_minus_z():
    """The SAE notchback file: mounting struts at +z, roof at -z - drawn upside down."""
    reading = read_up(TURN["-z"](_car_on_struts()))
    assert reading.axis == "-z"
    assert "+z end" in reading.reason                 # it stands on its +z end


@pytest.mark.parametrize("up", ["+z", "-z", "+y", "-x"])
def test_a_car_on_its_wheels_is_read_the_right_way_up(up):
    assert read_up(TURN[up](_car_on_wheels())).axis == up


@pytest.mark.parametrize("up", ["+z", "-z", "+y"])
def test_an_aircraft_standing_on_its_landing_gear_is_read_the_right_way_up(up):
    assert read_up(TURN[up](_aircraft(gear=True))).axis == up


@pytest.mark.parametrize("up", ["+z", "-z"])
def test_an_aircraft_in_flight_says_nothing_and_leaves_it_to_the_pictures(up):
    reading = read_up(TURN[up](_aircraft(gear=False)))
    assert reading.axis is None and reading.confidence == 0.0


def test_one_sting_is_not_three_feet():
    assert read_up(_sting_model()).axis is None


def test_a_pipe_says_nothing_about_up():
    assert read_up(_pipe()).axis is None


def test_a_rotor_whose_two_faces_look_alike_says_nothing():
    assert read_up(_rotor()).axis is None


@pytest.mark.parametrize("up", UP_AXES)
def test_a_block_of_buildings_stands_on_the_end_all_of_them_reach(up):
    reading = read_up(TURN[up](_city_block()))
    assert reading.axis == up and "pieces" in reading.reason


def test_the_ends_are_keyed_by_the_direction_out_of_them():
    ends = read_ends(_car_on_struts())
    assert set(ends) == set(UP_AXES)
    assert ends["-z"]["contacts"] == 4 and ends["-z"]["cover"] < 0.06
    assert ends["+z"]["contacts"] == 1


def test_two_ends_that_both_look_like_feet_say_nothing():
    feet = {"contacts": 4, "spread": [0.6, 0.6], "cover": 0.01, "pieces": 0.0, "n_pieces": 1}
    flat = {"contacts": 1, "spread": [0.0, 0.0], "cover": 0.5, "pieces": 0.0, "n_pieces": 1}
    ends = {a: dict(flat) for a in UP_AXES}
    ends["-z"] = dict(feet)
    assert up_from_ends(ends).axis == "+z"
    ends["-x"] = dict(feet)                          # a second axis with feet
    assert up_from_ends(ends).axis is None
    ends = {a: dict(flat) for a in UP_AXES}
    ends["-z"] = ends["+z"] = dict(feet)             # both ends of one axis
    assert up_from_ends(ends).axis is None


def test_a_part_past_the_bound_is_not_read_and_leaves_it_to_the_pictures(monkeypatch):
    import meshpipeline.cad.up_axis as ua

    monkeypatch.setattr(ua, "MAX_TRIANGLES", 100)
    reading = read_up(TURN["-z"](_car_on_struts()))
    assert reading.axis is None and "too many triangles" in reading.reason


def test_an_empty_part_is_read_as_saying_nothing():
    assert read_up(np.zeros((0, 3, 3))).axis is None
    assert opposite("+z") == "-z" and opposite("-x") == "+x"
