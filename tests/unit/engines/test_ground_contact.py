# Responsibility: Pin how a body standing on the ground meets the floor: a body resting on a flat
# face keeps the floor at its lowest point, a body that touches at points or lines (a wheel, a
# sphere, a nose that rolls down onto the floor) gets the floor raised a little into it so the
# contact is a flat patch, and the raise is measured from the geometry and bounded.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.engines import ground_contact as G

# bodies built from triangles, so every number below is measured, not assumed

def _extrude(profile_xz: list[tuple[float, float]], y0: float, y1: float) -> np.ndarray:
    """Close a convex x-z profile swept along y into a watertight triangle soup."""
    pts = [(float(x), float(z)) for x, z in profile_xz]
    cx = sum(p[0] for p in pts) / len(pts)
    cz = sum(p[1] for p in pts) / len(pts)
    tris = []
    n = len(pts)
    for i in range(n):
        (xa, za), (xb, zb) = pts[i], pts[(i + 1) % n]
        a0, b0, a1, b1 = (xa, y0, za), (xb, y0, zb), (xa, y1, za), (xb, y1, zb)
        tris += [(a0, b0, b1), (a0, b1, a1)]                       # the swept side
        tris.append(((cx, y0, cz), b0, a0))                        # the two caps
        tris.append(((cx, y1, cz), a1, b1))
    return np.asarray(tris, dtype=float)


def _box(dx=1.0, dy=0.4, dz=0.3, z0=0.0) -> np.ndarray:
    return _extrude([(0, z0), (dx, z0), (dx, z0 + dz), (0, z0 + dz)], -dy / 2, dy / 2)


def _wheel(radius=0.3, width=0.2, n=96, z0=0.0) -> np.ndarray:
    """A cylinder lying on its side on the ground: it touches along one line."""
    # start half a facet off the bottom so the lowest point is a vertex pair, as CAD tessellates
    ring = [(radius * math.cos(-math.pi / 2 + (k + 0.5) * 2 * math.pi / n),
             z0 + radius + radius * math.sin(-math.pi / 2 + (k + 0.5) * 2 * math.pi / n))
            for k in range(n)]
    return _extrude(ring, -width / 2, width / 2)


def _sphere(radius=0.25, n=48) -> np.ndarray:
    tris = []

    def p(i, j):
        th, ph = math.pi * i / n, 2 * math.pi * j / n
        return (radius * math.sin(th) * math.cos(ph), radius * math.sin(th) * math.sin(ph),
                radius + radius * math.cos(th))
    for i in range(n):
        for j in range(n):
            a, b, c, d = p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)
            if i > 0:
                tris.append((a, b, d))
            if i < n - 1:
                tris.append((b, c, d))
    return np.asarray(tris, dtype=float)


def _rounded_nose(length=1.044, height=0.288, radius=0.1, width=0.389, n=24) -> np.ndarray:
    """The Ahmed variant's side profile: a flat underside on the ground whose front edge rolls
    up in a quarter circle - a large flat seat AND a line where the nose grazes the floor."""
    arc = [(radius - radius * math.sin(t), radius - radius * math.cos(t))
           for t in (math.pi / 2 * (1 - k / n) for k in range(n + 1))]
    profile = arc + [(length, 0.0), (length, height), (0.0, height)]
    return _extrude(profile, -width / 2, width / 2)


def _chamfered(chamfer_deg=45.0, c=0.05) -> np.ndarray:
    run = c / math.tan(math.radians(chamfer_deg))
    return _extrude([(run, 0.0), (1.0, 0.0), (1.0, 0.3), (0.0, 0.3), (0.0, c)], -0.2, 0.2)


# the classification

class TestSeatedBodiesKeepTheFloorAtTheirLowestPoint:
    def test_a_box_resting_on_its_underside_is_seated(self):
        c = G.measure_contact(_box())
        assert c.kind == G.SEATED
        assert c.floor_z == c.lowest_z == 0.0 and c.penetration_m == 0.0
        assert c.seat_area_m2 == pytest.approx(0.4, rel=1e-6)
        assert c.contact_angle_deg == pytest.approx(90.0, abs=1e-6)

    def test_a_box_off_the_origin_is_measured_from_its_own_lowest_point(self):
        c = G.measure_contact(_box(z0=-1.25))
        assert c.kind == G.SEATED and c.floor_z == pytest.approx(-1.25)

    def test_a_steep_chamfer_is_still_seated(self):
        # a 45-degree bevel meets the floor at 45 degrees: a real corner, not a sliver
        c = G.measure_contact(_chamfered(45.0))
        assert c.kind == G.SEATED and c.penetration_m == 0.0

    def test_a_fillet_finer_than_any_wall_cell_is_still_seated(self):
        # a 1 mm round on the bottom edge of a 300 mm box: it grazes the floor, but below a
        # height no wall cell resolves - the box rests on its face and keeps its floor
        r, n = 0.001, 8
        arc = [(r - r * math.sin(t), r - r * math.cos(t))
               for t in (math.pi / 2 * (1 - k / n) for k in range(n + 1))]
        c = G.measure_contact(_extrude(arc + [(1.0, 0.0), (1.0, 0.3), (0.0, 0.3)], -0.2, 0.2))
        assert c.kind == G.SEATED and c.penetration_m == 0.0

    def test_the_winding_of_the_file_does_not_matter(self):
        box = _box()
        flipped = box[:, ::-1, :]
        assert G.measure_contact(flipped) == G.measure_contact(box)


class TestGrazingBodiesGetAFlatContactPatch:
    def test_a_wheel_on_its_side_touches_along_a_line(self):
        c = G.measure_contact(_wheel(radius=0.3))
        assert c.kind == G.GRAZING
        # the only flat area on the floor is the one facet the tessellation laid under the axle
        assert c.seat_area_m2 < 0.05 * (2 * 0.3 * 0.2)
        assert 0.0 < c.penetration_m <= G.MAX_PENETRATION_SHARE * c.height

    def test_the_cut_goes_as_deep_as_the_angle_needs(self):
        # given room (a bare wheel is only 2r tall, so the default cap stops it first), the floor
        # rises into the tyre until its sides meet the floor at the required angle: for a circle
        # of radius r that is r(1 - cos a), within one facet
        c = G.measure_contact(_wheel(radius=0.3), max_share=0.1)
        want = 0.3 * (1 - math.cos(math.radians(G.MIN_CONTACT_ANGLE_DEG)))
        assert c.penetration_m == pytest.approx(want, rel=0.35)
        assert c.contact_angle_deg >= G.MIN_CONTACT_ANGLE_DEG and not c.capped

    def test_a_sphere_touches_at_a_point(self):
        c = G.measure_contact(_sphere())
        assert c.kind == G.GRAZING
        assert 0.0 < c.penetration_m <= G.MAX_PENETRATION_SHARE * c.height

    def test_a_flat_underside_with_a_nose_that_rolls_onto_the_floor_is_grazing(self):
        # the Ahmed variant: most of its underside lies flat, but its rounded nose meets the floor
        # tangentially - the sliver of fluid under the nose is what snappy could not snap
        c = G.measure_contact(_rounded_nose())
        assert c.kind == G.GRAZING
        assert c.seat_area_m2 > 0.3                     # the flat underside is still measured
        assert c.penetration_m > 0.0

    def test_a_shallow_chamfer_is_grazing(self):
        c = G.measure_contact(_chamfered(10.0, c=0.02))
        assert c.kind == G.GRAZING

    def test_a_small_wheel_beside_a_large_seated_box_still_grazes(self):
        # the wheel's shallow stretch is a sliver of the scene's whole contact line, but it is a
        # wedge the mesh will meet all the same - a share of the line would have hidden it
        box = _box(dx=4.0, dy=2.0, dz=1.5)
        wheel = _wheel(radius=0.1, width=0.05) + np.array([5.0, 0.0, 0.0])
        c = G.measure_contact(np.concatenate([box, wheel]))
        assert c.kind == G.GRAZING and c.penetration_m > 0.0

    @pytest.mark.parametrize("flipped", ["wheel", "box"])
    def test_pieces_wound_opposite_ways_are_each_read_outward(self, flipped):
        # a scene whose pieces disagree on winding: one sign for the lot would read the wheel
        # inside out and call its wedge an obtuse corner
        box = _box(dx=1.0, dy=0.4, dz=0.8)
        wheel = _wheel(radius=0.3, width=0.2) + np.array([2.0, 0.0, 0.0])
        if flipped == "wheel":
            wheel = wheel[:, ::-1, :]
        else:
            box = box[:, ::-1, :]
        c = G.measure_contact(np.concatenate([box, wheel]))
        assert c.kind == G.GRAZING
        assert c == G.measure_contact(np.concatenate([_box(dx=1.0, dy=0.4, dz=0.8),
                                                      _wheel(radius=0.3, width=0.2)
                                                      + np.array([2.0, 0.0, 0.0])]))

    def test_it_is_the_angle_that_decides_not_the_area(self):
        # a keel standing on its edge touches along a line, but its flanks leave the floor at
        # 31 degrees - a real corner the cells fill, so no cut; a flatter keel (15 degrees) is cut
        steep = G.measure_contact(_extrude([(0.0, 0.3), (0.5, 0.0), (1.0, 0.3)], -0.2, 0.2))
        flat = G.measure_contact(_extrude([(0.0, 0.134), (0.5, 0.0), (1.0, 0.134)], -0.2, 0.2))
        assert steep.kind == G.SEATED and steep.seat_area_m2 == 0.0
        assert steep.contact_angle_deg == pytest.approx(30.96, abs=0.05)
        assert flat.kind == G.GRAZING and flat.capped


class TestThePenetrationIsBounded:
    def test_it_never_cuts_more_than_the_share_of_the_height(self):
        # a nose radius as large as the body: the angle needs a deep cut, the cap stops it
        c = G.measure_contact(_rounded_nose(height=0.2, radius=0.2))
        assert c.capped is True
        assert c.penetration_m == pytest.approx(G.MAX_PENETRATION_SHARE * c.height)
        assert 0.0 < c.contact_angle_deg < G.MIN_CONTACT_ANGLE_DEG

    def test_a_raised_floor_is_raised_at_least_the_smallest_share(self):
        c = G.measure_contact(_wheel(0.3))
        assert c.penetration_m >= G.MIN_PENETRATION_SHARE * c.height

    def test_it_scales_with_the_body(self):
        small, large = G.measure_contact(_wheel(0.1)), G.measure_contact(_wheel(1.0))
        assert large.penetration_m == pytest.approx(10 * small.penetration_m, rel=1e-6)

    def test_the_cut_plane_lies_inside_the_body(self):
        c = G.measure_contact(_wheel(0.3))
        assert c.lowest_z < c.floor_z < c.lowest_z + c.height


class TestTheContactLine:
    def test_the_line_is_the_bodys_section_at_the_floor(self):
        segments, angles = G.contact_line(_box(), 0.1)
        length = float(np.linalg.norm(segments[:, 1] - segments[:, 0], axis=1).sum())
        assert length == pytest.approx(2 * (1.0 + 0.4))
        assert np.allclose(angles, 90.0)
        assert np.allclose(segments[..., 2], 0.1)

    def test_the_footprint_is_where_the_body_meets_the_floor(self):
        c = G.measure_contact(_wheel(0.3, width=0.2))
        (x0, y0), (x1, y1) = c.footprint
        assert -0.3 < x0 < 0.0 < x1 < 0.3                 # a strip under the axle, not the tyre
        assert y0 == pytest.approx(-0.1) and y1 == pytest.approx(0.1)

    def test_a_body_with_no_triangles_is_refused(self):
        with pytest.raises(ValueError):
            G.measure_contact(np.zeros((0, 3, 3)))


class TestTheNoteSaysWhereTheFloorIs:
    def test_a_seated_body_is_told_the_floor_is_at_its_lowest_point(self):
        said = G.describe(G.measure_contact(_box()))
        assert "sits at its lowest point" in said and " mm " not in said

    def test_a_grazing_body_is_told_how_far_the_floor_was_raised(self):
        c = G.measure_contact(_wheel(0.3))
        said = G.describe(c)
        assert f"{1000 * c.penetration_m:.3g} mm above its lowest point" in said
        assert "contact patch" in said


class TestTheRunnerMeasuresTheStagedBody:
    def test_it_reads_the_surface_staged_in_the_workspace(self, tmp_path):
        from meshpipeline.cad.stl_io import write_stl_binary
        from meshpipeline.engines.snappy import snappy_runner as R
        nose = _rounded_nose()
        write_stl_binary(tmp_path / "input.stl", [tuple(map(tuple, t)) for t in nose])
        c = R.measure_ground_contact(tmp_path)
        assert c == G.measure_contact(nose.astype(np.float32).astype(float))
        # the Ahmed variant's numbers: a 288 mm body with a 100 mm nose radius, cut until the
        # nose meets the floor at 25 degrees - 100 (1 - cos 25) = 9.4 mm on the true circle,
        # within a facet on a tessellated one (the real file measures 8.05 mm)
        assert c.kind == G.GRAZING and not c.capped
        assert c.penetration_m == pytest.approx(0.0094, rel=0.2)
        assert c.contact_angle_deg >= G.MIN_CONTACT_ANGLE_DEG
