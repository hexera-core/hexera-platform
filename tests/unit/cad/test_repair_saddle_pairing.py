# Responsibility: Pin the non-planar rim pairing rule on synthetic point loops, with no kernel: which saddle pairs are one through-hole, which are two holes, and which cannot be judged.
from __future__ import annotations

import math

import pytest

import meshpipeline.cad.repair.conservative as conservative


@pytest.fixture(autouse=True)
def _points_are_the_loop(monkeypatch):
    # A "wire" here IS its sampled points, so the rule is exercised without OpenCASCADE.
    monkeypatch.setattr(conservative, "_loop_points", lambda wire, per_edge=24: list(wire))


def _saddle(z=10.0, radius=4.0, sag=0.4, x=0.0, y=0.0, n=48):
    """A hole's rim on a curved face: a ring whose height dips with cos^2 around it."""
    return [(x + radius * math.cos(a), y + radius * math.sin(a), z - sag * math.cos(a) ** 2)
            for a in (2 * math.pi * k / n for k in range(n))]


def _ring(z=0.0, radius=4.0, x=0.0, n=48):
    return [(x + radius * math.cos(a), radius * math.sin(a), z)
            for a in (2 * math.pi * k / n for k in range(n))]


def _seam(radius=4.0, n=48):
    """A loop as deep as it is wide (a tennis-ball seam): it has no normal to judge it by."""
    return [(radius * math.cos(a), radius * math.sin(a), radius * 1.5 * math.cos(2 * a))
            for a in (2 * math.pi * k / n for k in range(n))]


def _coaxial(left, right):
    fits = [conservative._rim_fit(left), conservative._rim_fit(right)]
    return conservative._saddle_coaxial(*fits)


# THE COAXIAL RULE - the brief's numbers, each erring toward pairing


def test_two_saddles_on_one_axis_pair():
    assert conservative._paired_nonplanar_rims([_saddle(z=-10.0), _saddle(z=10.0)]) == {0, 1}
    assert _coaxial(_saddle(z=-10.0), _saddle(z=10.0))


def test_saddles_offset_off_the_axis_by_one_radius_are_not_coaxial():
    assert not _coaxial(_saddle(z=-10.0), _saddle(z=10.0, x=4.0))
    assert _coaxial(_saddle(z=-10.0), _saddle(z=10.0, x=1.9))


def test_the_radius_ratio_limit_sits_at_two_and_a_half():
    assert _coaxial(_saddle(z=-10.0, radius=4.0), _saddle(z=10.0, radius=9.6))
    assert not _coaxial(_saddle(z=-10.0, radius=4.0), _saddle(z=10.0, radius=10.4))


def test_a_planar_rim_and_a_saddle_on_one_axis_pair():
    assert conservative._paired_nonplanar_rims([_ring(z=0.0), _saddle(z=10.0)]) == {0, 1}
    assert _coaxial(_ring(z=0.0), _saddle(z=10.0))


def test_the_gap_along_the_axis_has_a_floor_and_a_ceiling_of_forty_radii():
    assert not _coaxial(_saddle(z=0.0), _saddle(z=0.0))
    assert _coaxial(_saddle(z=0.0), _saddle(z=150.0))
    assert not _coaxial(_saddle(z=0.0), _saddle(z=170.0))


def test_tilted_axes_are_not_coaxial():
    tilted = [(px, py * math.cos(0.3) - pz * math.sin(0.3), py * math.sin(0.3) + pz * math.cos(0.3))
              for px, py, pz in _saddle(z=0.0)]
    assert not _coaxial(_saddle(z=-10.0), tilted)


# THE PASSAGE RULE STILL PAIRS WHAT THE COAXIAL RULE DOES NOT - the safe direction


def test_a_pair_the_coaxial_rule_rejects_still_pairs_when_a_passage_could_join_them():
    # An offset, flared or angled passage through a round bar: the landed planar rule pairs rims
    # whose centroid line leaves each no steeper than 70 deg from its normal, with no size-ratio
    # cap (a spot-face is three times its bore). A curved rim is held to the same rule, so the
    # coaxial limits can only ADD pairs, never release one.
    assert conservative._paired_nonplanar_rims(
        [_saddle(z=-10.0), _saddle(z=10.0, x=4.0)]) == {0, 1}
    assert conservative._paired_nonplanar_rims(
        [_saddle(z=-10.0, radius=4.0), _saddle(z=10.0, radius=10.4)]) == {0, 1}


def test_two_windows_side_by_side_on_one_skin_do_not_pair():
    # The line between them runs along the skin, 90 deg from both normals: two holes.
    assert conservative._paired_nonplanar_rims(
        [_saddle(z=19.8, x=30.0), _saddle(z=19.8, x=70.0)]) == set()


def test_one_loop_alone_has_no_partner():
    assert conservative._paired_nonplanar_rims([_saddle()]) == set()
    assert conservative._paired_nonplanar_rims([_seam()]) == set()


# WHAT CANNOT BE JUDGED STILL FAILS CLOSED


def test_a_loop_with_no_normal_beside_another_is_undecidable():
    with pytest.raises(conservative.RimsUndecidable):
        conservative._paired_nonplanar_rims([_seam(), _saddle(z=10.0, x=30.0)])
    assert conservative._rim_fit(_seam()) is None
    assert conservative._rim_fit([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]) is None
