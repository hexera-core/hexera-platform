"""A coolant whose two ports are on pieces that do not touch is refused before it is meshed.

WHY THIS EXISTS. `conjugate.verify` counts the coolant's pieces and compares them against
`expect_fluid_pieces` - and that expectation is a count of THE SAME SURFACE's pieces. A coolant
already severed therefore meets its own expectation and the check agrees with the defect by
construction. The question that cannot be satisfied that way is whether the inlet and the outlet
are on the same piece, and it is answerable in milliseconds on the surface, before any mesher
runs.

WHAT IT COST ON THE PART THIS WAS WRITTEN FOR. ColdPlateAssembly_CLEAN.step, job b6762d5c,
2026-11-13: 326,631,400 cells over 2 h 29 m on a 64-core machine, a conformal interface at
21,618,626 faces on both sides and a coolant volume within 0.92% of the CAD - a faithful mesh of
a part with no path from its inlet to its outlet. The plate's channels and the outlet tube's bore
are 0.500 mm of metal apart (0.5797 mm vertex to vertex, the 63 nearest tube vertices all on
y = 33.946 mm against the plate's surface at y = 33.446), so the gap is the assembly's and not
this pipeline's: `fluid.stl` is watertight and within 0.046% of the volume the CAD declares.

The geometry below is a two-box stand-in for that shape at a size a test can hold, with the same
question in it.
"""
import numpy as np
import pytest

from meshpipeline.cad.fluid_passages import ports_on_one_passage

#: How finely each box face is divided. Not decoration: both the piece a port is assigned to and
#: the gap the refusal quotes are read off VERTICES, so a 12-triangle box - whose only vertices
#: are its eight corners - answers both questions with corner-to-corner distances that have
#: nothing to do with the surfaces. The real part carries 52,334 triangles; this carries enough
#: that the stand-in behaves the same way.
GRID = 10


def box(lo, hi, n: int = GRID):
    """An axis-aligned box as a closed, outward-facing triangle grid, n x n to a face."""
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    out = []
    for axis in range(3):
        u, v = [k for k in range(3) if k != axis]
        for side, sign in ((lo[axis], -1.0), (hi[axis], 1.0)):
            us = np.linspace(lo[u], hi[u], n + 1)
            vs = np.linspace(lo[v], hi[v], n + 1)
            for i in range(n):
                for j in range(n):
                    corners = [(us[i], vs[j]), (us[i + 1], vs[j]),
                               (us[i + 1], vs[j + 1]), (us[i], vs[j + 1])]
                    pts = []
                    for a, b in corners:
                        q = np.empty(3)
                        q[axis], q[u], q[v] = side, a, b
                        pts.append(q)
                    for tri in ((0, 1, 2), (0, 2, 3)):
                        T = np.array([pts[k] for k in tri])
                        nrm = np.cross(T[1] - T[0], T[2] - T[0])
                        if nrm[axis] * sign < 0:
                            T = T[::-1]
                        out.append(T)
    return np.asarray(out)


#: Two boxes a tenth of a millimetre apart in y, stacked: the shape of a plate channel under a
#: tube bore. They share their x and z bounds EXACTLY, so no bounding-box test can tell which one
#: a port is on - only the distance to the surface can, which is the rule under test. Stacking
#: also puts the two nearest corners directly above each other, so the vertex-to-vertex distance
#: IS the gap; on the real part, which is tessellated far more finely, the two agree to 16%.
PLATE = box((0.0, 0.0, 0.0), (0.050, 0.010, 0.020))
TUBE = box((0.0, 0.0101, 0.0), (0.050, 0.0131, 0.020))
APART = np.concatenate([PLATE, TUBE])
#: The same two boxes touching, so their shared face welds them into one piece.
JOINED = np.concatenate([PLATE, box((0.0, 0.010, 0.0), (0.050, 0.013, 0.020))])

ON_PLATE = [0.0, 0.005, 0.010]          # the middle of the plate box's x = 0 face
ON_TUBE = [0.0, 0.0116, 0.010]          # the middle of the tube box's x = 0 face


def ports(inlet, outlet, extra=()):
    rows = [{"name": "inlet", "type": "inlet", "centre_m": list(inlet)},
            {"name": "outlet", "type": "outlet", "centre_m": list(outlet)}]
    return rows + [dict(e) for e in extra]


def test_two_ports_on_pieces_that_do_not_touch_are_refused():
    said = ports_on_one_passage(APART, ports(ON_PLATE, ON_TUBE))
    assert said
    assert "different pieces" in said
    assert "Nothing can flow from one to the other" in said


def test_the_refusal_carries_the_distance_so_a_wall_can_be_told_from_a_second_circuit():
    said = ports_on_one_passage(APART, ports(ON_PLATE, ON_TUBE))
    assert "0.100 mm apart at their closest" in said, said


def test_the_refusal_carries_both_volumes():
    said = ports_on_one_passage(APART, ports(ON_PLATE, ON_TUBE))
    assert "10,000.0 mm3" in said, said       # the plate box: 50 x 10 x 20
    assert "3,000.0 mm3" in said, said        # the tube box:  50 x  3 x 20


def test_the_same_geometry_joined_says_nothing():
    assert ports_on_one_passage(JOINED, ports(ON_PLATE, ON_TUBE)) == ""


def test_two_ports_on_the_same_piece_of_a_severed_surface_say_nothing():
    """Severed is not itself the complaint - a part can have a dead pocket and still flow."""
    other_end = [0.050, 0.005, 0.010]
    assert ports_on_one_passage(APART, ports(ON_PLATE, other_end)) == ""


def test_a_mouth_that_is_not_a_port_does_not_decide_anything():
    """A wall-typed mouth on the far piece is a sealed opening, not an outlet."""
    wall = ({"name": "mouth_3", "type": "wall", "centre_m": ON_TUBE},)
    other_end = [0.050, 0.005, 0.010]
    assert ports_on_one_passage(APART, ports(ON_PLATE, other_end, wall)) == ""


@pytest.mark.parametrize("rows", [
    [],
    [{"name": "inlet", "type": "inlet", "centre_m": ON_PLATE}],
    [{"name": "a", "type": "wall", "centre_m": ON_PLATE},
     {"name": "b", "type": "wall", "centre_m": ON_TUBE}],
])
def test_without_both_roles_there_is_no_question_to_answer(rows):
    """A sealed part is a legal plan, so half a port plan is silence and not a refusal."""
    assert ports_on_one_passage(APART, rows) == ""


def test_one_piece_is_always_fine():
    assert ports_on_one_passage(PLATE, ports(ON_PLATE, [0.050, 0.005, 0.010])) == ""


def test_a_surface_too_large_to_read_quickly_is_no_evidence():
    """The reading has to stay milliseconds: it sits in front of a mesher, not instead of one."""
    from meshpipeline.cad import fluid_passages as FP
    big = np.repeat(APART, (FP.VET_MAX_TRIANGLES // len(APART)) + 1, axis=0)
    assert len(big) > FP.VET_MAX_TRIANGLES
    assert ports_on_one_passage(big, ports(ON_PLATE, ON_TUBE)) == ""


def test_the_piece_a_port_opens_into_is_the_nearest_one_and_not_the_one_it_is_inside():
    """The two pieces have the SAME x and z bounds, so nothing about a bounding box separates
    them: a port is assigned by how far its centre is from each piece's surface, and by nothing
    else. Both ports here sit exactly on their own piece."""
    d = lambda T, p: float(np.linalg.norm(  # noqa: E731
        T.reshape(-1, 3) - np.asarray(p), axis=1).min())
    assert PLATE.reshape(-1, 3)[:, [0, 2]].min() == TUBE.reshape(-1, 3)[:, [0, 2]].min()
    assert PLATE.reshape(-1, 3)[:, [0, 2]].max() == TUBE.reshape(-1, 3)[:, [0, 2]].max()
    assert d(PLATE, ON_PLATE) < 0.1 * d(TUBE, ON_PLATE)
    assert d(TUBE, ON_TUBE) < 0.1 * d(PLATE, ON_TUBE)
    assert ports_on_one_passage(APART, ports(ON_PLATE, ON_TUBE))


# #
# A MOUTH IS A RIM, NOT A POINT
# #
#: The three numbers that forced this rule, from job b6762d5c's own surface, in mm: opening 1 is
#: 5.900 from the plate - exactly its own radius, because it IS a face of the plate - and 3.979
#: from the tube that runs over it. Nearest alone puts it on the tube, which would have refused a
#: port pair that is geometrically sound; the rule that matches its own radius puts it on the
#: plate, where the scout independently put it ("space 1: openings 1, 2; space 2: opening 3").
OPENING_1_RADIUS_M = 0.0059
NEAR_PIECE_M = 0.003979
FAR_PIECE_M = 0.0059

CENTRE = [0.100, 0.100, 0.100]
#: Its near face is FAR_PIECE_M from CENTRE, on a vertex: 0.100 is on this face's grid.
FAR = box((0.100 + FAR_PIECE_M, 0.0, 0.0), (0.200 + FAR_PIECE_M, 0.200, 0.200))
#: Its near face is NEAR_PIECE_M from CENTRE, on the other side, also on a vertex.
NEAR = box((0.0, 0.040, 0.040), (0.100 - NEAR_PIECE_M, 0.160, 0.160))
TWO = np.concatenate([FAR, NEAR])


def test_the_distances_this_rule_turns_on_are_the_ones_measured():
    d = lambda T: float(np.linalg.norm(  # noqa: E731
        T.reshape(-1, 3) - np.asarray(CENTRE), axis=1).min())
    assert d(FAR) == pytest.approx(FAR_PIECE_M, abs=1e-9)
    assert d(NEAR) == pytest.approx(NEAR_PIECE_M, abs=1e-9)
    assert NEAR_PIECE_M < FAR_PIECE_M, "the nearer piece must be the wrong answer"


def test_a_mouth_opens_into_the_piece_it_is_its_own_radius_from_not_the_nearest_one():
    far_end = [0.0, 0.100, 0.100]            # on the NEAR box's far side, 96 mm away
    rows = [{"name": "inlet", "type": "inlet", "centre_m": CENTRE,
             "diameter_m": 2 * OPENING_1_RADIUS_M},
            {"name": "outlet", "type": "outlet", "centre_m": far_end, "diameter_m": 0.0}]
    said = ports_on_one_passage(TWO, rows)
    # the inlet is on FAR by its radius, the outlet is on NEAR: two pieces, so it refuses
    assert said and "different pieces" in said, said


def test_without_a_diameter_it_falls_back_to_the_nearest_piece():
    """Which is the only thing it can do, and is why the plan carries the diameter."""
    far_end = [0.0, 0.100, 0.100]
    rows = [{"name": "inlet", "type": "inlet", "centre_m": CENTRE},
            {"name": "outlet", "type": "outlet", "centre_m": far_end}]
    # with no radius the inlet lands on NEAR, the same piece as the outlet, and nothing is said
    assert ports_on_one_passage(TWO, rows) == ""


#: The stacked boxes again, with the ports placed as real ones are: one radius off the face they
#: open through, rather than on it.
RIM_D = 0.004
ON_PLATE_RIM = [-0.5 * RIM_D, 0.005, 0.010]
ON_TUBE_RIM = [-0.5 * RIM_D, 0.0116, 0.010]


def test_ports_placed_at_a_rim_land_on_their_own_piece():
    rows = [{"name": "inlet", "type": "inlet", "centre_m": ON_PLATE_RIM, "diameter_m": RIM_D},
            {"name": "outlet", "type": "outlet", "centre_m": ON_TUBE_RIM, "diameter_m": RIM_D}]
    said = ports_on_one_passage(APART, rows)
    assert said and "0.100 mm apart at their closest" in said, said
    assert ports_on_one_passage(JOINED, rows) == ""
