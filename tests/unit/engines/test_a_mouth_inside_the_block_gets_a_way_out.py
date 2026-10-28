# Responsibility: Verify a coolant mouth that stops inside the block is given a way out to a block
# face, that a mouth which opens into another part is not, and that the stub encloses what it says.
# Boundaries: geometry only. No mesher, no CAD kernel, no file.
#
# WHY THIS FILE EXISTS. A conjugate case takes its inlet and outlet from the block's six named
# faces, which works while the coolant's mouths lie ON those faces - eight of the nine parts in
# this package, because a channel milled across a plate ends flush with the side it was cut on. The
# first real customer assembly's mouths are fitting BORES and its bounding box is set by the
# fittings' own bodies, so the coolant stopped 2 mm inside the block on every side and the mesher
# refused with "it is sealed inside the metal".
import math

import pytest

from meshpipeline.engines.cfmesh_conjugate import mouths as M

#: The Corintis plate's block, as the plan derived it (metres).
BLO = (-0.06157654, 0.02011018, -0.008590401)
BHI = (0.1159235, 0.06194618, 0.0924096)

#: Its three mouths, as the extraction measured them and the surface record now carries them.
#: Two open into air and one opens into another part - which the extraction itself flagged.
CORINTIS = [
    {"passage": 0, "centroid_m": [0.06592, 0.03345, 0.07541], "normal": [0.0, 1.0, 0.0],
     "area_m2": 109.36e-6, "diameter_m": 0.0118, "wh_m": [0.0117, 0.0117], "clear_ahead": False},
    {"passage": 0, "centroid_m": [0.09988, 0.05468, 0.04331],
     "normal": [0.998233779, 0.0, 0.059408106],
     "area_m2": 50.27e-6, "diameter_m": 0.008, "wh_m": [0.00793, 0.00793], "clear_ahead": True},
    {"passage": 1, "centroid_m": [0.08934, 0.05468, 0.07739],
     "normal": [0.99643, 0.0, 0.08438],
     "area_m2": 50.27e-6, "diameter_m": 0.008, "wh_m": [0.00793, 0.00793], "clear_ahead": True},
]


def _stubs():
    return M.stubs_for(CORINTIS, BLO, BHI)


# which mouths get one

def test_a_mouth_that_opens_into_air_gets_a_stub():
    stubs, _said = _stubs()
    assert {s.port for s in stubs} == {1, 2}, "the two clear mouths are the ones that may have one"


def test_a_mouth_that_opens_into_another_part_does_not():
    """A stub there would be a passage bored through metal the customer did not draw."""
    stubs, said = _stubs()
    assert 0 not in {s.port for s in stubs}
    assert any("another part" in line for line in said), (
        f"nothing was said about the mouth that was refused: {said}")


def test_a_mouth_already_flush_with_a_face_is_left_alone():
    """open_faces will have found it; a stub would be a tube of nothing."""
    flush = dict(CORINTIS[1])
    flush["centroid_m"] = [BHI[0] - 1e-5, 0.05468, 0.04331]
    flush["normal"] = [1.0, 0.0, 0.0]
    stubs, _said = M.stubs_for([flush], BLO, BHI)
    assert stubs == []


def test_a_mouth_with_no_flow_area_is_not_a_port():
    dead = dict(CORINTIS[1])
    dead["area_m2"] = 0.0
    stubs, said = M.stubs_for([dead], BLO, BHI)
    assert stubs == [] and any("flow area" in line for line in said)


# where it goes

def test_each_stub_crosses_the_face_it_heads_for():
    """It has to CROSS the boundary, not land on it: a surface ending exactly on the face leaves
    surfaceToCell deciding a coincident-face question whose answer depends on rounding."""
    stubs, _said = _stubs()
    for s in stubs:
        axis, sign = M._AXIS[s.face]
        wall = BHI[axis] if sign > 0 else BLO[axis]
        past = (s.reach[axis] - wall) * sign
        assert past > 0.0, f"the stub for mouth {s.port} stops short of {s.face}"
        assert past <= M.OVERSHOOT * 2.0 * s.radius * 1.001, "it overshoots further than declared"


def test_the_face_a_mouth_heads_for_is_the_first_one_its_ray_reaches():
    face, distance = M.face_ahead((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (-1.0, -1.0, -1.0),
                                  (2.0, 5.0, 5.0))
    assert face == "xmax" and distance == pytest.approx(2.0)


def test_a_mouth_pointing_at_no_face_is_refused_rather_than_assumed():
    face, _d = M.face_ahead((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), BLO, BHI)
    assert face == ""


# what it encloses

def test_a_stub_encloses_the_cylinder_it_claims():
    """THE WINDING WAS WRONG FIRST TIME AND ONLY THIS CAUGHT IT.

    The tube's two triangles were wound inwards, so the customer's two stubs enclosed -840.735 mm3
    where they claim 2551.247. A closed surface that encloses the wrong volume raises no open,
    non-manifold or inconsistent edge, so nothing else downstream would have noticed.
    """
    stubs, _said = _stubs()
    tris = []
    for s in stubs:
        tris.extend(M.stub_triangles(s))
    got = M.signed_volume(tris)
    want = sum(s.volume for s in stubs)
    assert got > 0.0, "the stubs are wound inside out"
    # a 24-gon falls about 1% short of its circle, and that is the whole discrepancy
    assert got == pytest.approx(want, rel=0.02)
    assert M.added_volume_agrees(0.0, got, stubs)


def test_the_volume_check_refuses_a_stub_that_added_nothing():
    """What it is for: caps that cancelled each other instead of cancelling the mouth's."""
    stubs, _said = _stubs()
    assert not M.added_volume_agrees(0.0, 0.0, stubs)
    assert not M.added_volume_agrees(0.0, -sum(s.volume for s in stubs), stubs)


def test_a_stub_is_a_closed_shell():
    """Every edge used once each way, or the joined surface is not closed either."""
    stubs, _said = _stubs()
    for s in stubs:
        edges = {}
        for tri in M.stub_triangles(s):
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                key = (tuple(round(v, 12) for v in a), tuple(round(v, 12) for v in b))
                edges[key] = edges.get(key, 0) + 1
        for (a, b), n in edges.items():
            assert n == 1, f"edge used {n} times in one direction"
            assert edges.get((b, a)) == 1, "an edge has no opposite: the shell is open"


def test_the_stub_is_as_wide_as_the_mouths_flow_area():
    """The cell size and the flow rate both follow from it, so it is the area and not the hole."""
    stubs, _said = _stubs()
    for s in stubs:
        assert math.pi * s.radius ** 2 == pytest.approx(50.27e-6, rel=1e-6)
