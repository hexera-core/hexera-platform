# Responsibility: Say how a body standing on the ground meets the floor, and where the floor goes.
# Boundaries: pure measurement over a triangle array in metres. It reads no file, meshes nothing
# and knows no engine; the domain builder asks it for the floor height and the contact line.
# Collaborates with: engines/ground_plane.py (what the ground is), the snappy domain builder and
# case renderer (snappy_runner.domain_from_strategy, render_snappy_case) and the snappy driver.
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from meshpipeline.engines.ground_plane import VERTICAL_AXIS

#: A body whose sides rise steeply from the floor where it stands (a box or a building on its
#: underside, legs with flat feet, even a keel whose flanks leave the floor at a real angle).
#: The floor goes at its lowest point, as PR #84 laid it. It is the ANGLE that decides, not
#: whether the contact is a face, a line or a point: a steep line contact is a corner the cells
#: fill, and a flat face whose edge rolls down onto the floor still leaves a sliver.
SEATED = "seated"
#: A body that meets the floor at points or along lines, or whose surface curls down onto the
#: floor (wheels, a tyre, a sphere, a rounded nose). Between the floor and such a surface there
#: is a wedge of fluid whose angle goes to zero, and no mesher fills a zero-angle wedge with
#: good cells. The floor is raised a little into the body instead, so the body crosses it at a
#: real angle and the contact becomes a small flat patch - the tyre contact patch of
#: automotive CFD.
GRAZING = "grazing"

#: The angle the body should make with the floor where the two meet. A cell squeezed into a
#: wedge of angle a has faces about (90 - a) degrees out of true, and the snappy case refuses
#: faces past 65 degrees (meshQualityControls maxNonOrtho) - so under 25 degrees the wedge
#: cannot be filled with cells that pass the mesher's own check. Measured on the Ahmed variant
#: (OpenFOAM 11, 1.4-2.3 mm cells at the contact, floor cut alone): cut so the nose met the
#: floor at 10-21 degrees, about 250 faces along the contact were past 60 degrees; cut to 25
#: degrees, 6 were.
MIN_CONTACT_ANGLE_DEG = 25.0
#: The contact is read this far above the lowest point, and a raised floor is raised at least
#: this far. A share of the body's height, so it scales with the part: anything lower - a small
#: fillet on a flat underside, the tessellation's own noise - is finer than the wall cells any
#: plan affords (the wall is sized at about 1/150 of the body's diagonal), so the mesh never
#: sees it, and a cut thinner than this would be a sliver of its own.
MIN_PENETRATION_SHARE = 0.005
#: The raised floor never cuts more than this share of the body's height. A round meets the
#: floor at 25 degrees once cut by 9% of its radius (1 - cos 25): about 30 mm into a car's tyre
#: (2% of the car's height) or 8 mm into the Ahmed variant's 100 mm nose (2.8% of its height).
#: Past 4% the cut stops being a contact patch and starts being a shorter body, so it stops
#: there and the note says at what angle the body then meets the floor.
MAX_PENETRATION_SHARE = 0.04
#: A grazing stretch shorter than this share of the whole contact line is a sliver of the
#: tessellation (a fan of tiny triangles at a corner), not a wedge the mesh will see.
_NOISE_SHARE = 0.01
#: Heights tried between the probe and the cap when looking for the shallowest good cut.
_STEPS = 64


@dataclass(frozen=True)
class GroundContact:
    """How the body meets the floor, and where the floor is laid."""

    kind: str                     # SEATED or GRAZING
    lowest_z: float               # the body's lowest point
    floor_z: float                # where the floor goes
    height: float                 # the body's vertical extent
    contact_angle_deg: float      # the shallowest angle between body and floor along the contact
    seat_area_m2: float           # flat area of the body lying on its lowest plane
    footprint: tuple[tuple[float, float], tuple[float, float]]  # contact line's (x, y) box
    capped: bool = False          # the cap stopped the cut before the angle was reached
    #: where the body crosses the raised floor, as a welded polyline: points and the edges that
    #: join them (empty for a seated body). The mesher refines and snaps along it so the cut is
    #: resolved. Derived from the rest, so it takes no part in comparing two contacts.
    line_points: tuple[tuple[float, float, float], ...] = field(default=(), repr=False,
                                                                 compare=False)
    line_edges: tuple[tuple[int, int], ...] = field(default=(), repr=False, compare=False)

    @property
    def penetration_m(self) -> float:
        """How far the floor sits above the body's lowest point (0 for a seated body)."""
        return self.floor_z - self.lowest_z


def _unit_normals(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    keep = norm > 0
    normals = np.zeros_like(cross)
    normals[keep] = cross[keep] / norm[keep, None]
    # outward, whatever winding the file used: a closed body's signed volume is positive when
    # its normals point out of it
    signed = float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum())
    if signed < 0:
        normals = -normals
    return normals, 0.5 * norm


def contact_line(tris, z: float) -> tuple[np.ndarray, np.ndarray]:
    """The body's cross-section at height z: segments (M, 2, 3) where the surface crosses the
    plane, and the fluid-side angle in degrees the surface makes with that plane on each.

    The angle is measured in the fluid between the floor and the body: 90 for a wall standing
    straight up, near 0 for a surface curling down onto the floor, over 90 where the body
    widens towards the floor."""
    t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    normals, _area = _unit_normals(t)
    return _section(t, normals, z)


def _section(t: np.ndarray, normals: np.ndarray, z: float) -> tuple[np.ndarray, np.ndarray]:
    above = t[:, :, VERTICAL_AXIS] > z
    count = above.sum(axis=1)
    cut = (count == 1) | (count == 2)
    if not cut.any():
        return np.zeros((0, 2, 3)), np.zeros(0)
    t, above, normals = t[cut], above[cut], normals[cut]
    a_idx, b_idx = (0, 1, 2), (1, 2, 0)
    pa, pb = t[:, a_idx], t[:, b_idx]                       # (M, 3 edges, 3)
    crosses = above[:, a_idx] != above[:, b_idx]            # exactly two edges per triangle
    za, zb = pa[..., VERTICAL_AXIS], pb[..., VERTICAL_AXIS]
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(crosses, (z - za) / (zb - za), 0.0)
    points = pa + s[..., None] * (pb - pa)
    segments = points[crosses].reshape(-1, 2, 3)
    angles = np.degrees(np.arccos(np.clip(-normals[:, VERTICAL_AXIS], -1.0, 1.0)))
    return segments, angles


def _grazing_share(t: np.ndarray, normals: np.ndarray, z: float,
                   min_angle: float) -> tuple[float, float, np.ndarray]:
    """(share of the contact line meeting the floor under min_angle, the shallowest angle on
    the rest of the line once the noise is set aside, the segments)."""
    segments, angles = _section(t, normals, z)
    if not len(segments):
        return 0.0, 90.0, segments
    lengths = np.linalg.norm(segments[:, 1] - segments[:, 0], axis=1)
    total = float(lengths.sum()) or 1.0
    shallow = float(lengths[angles < min_angle].sum()) / total
    # the shallowest angle the mesh will actually meet: skip the lowest noise share of the line
    order = np.argsort(angles)
    cum = np.cumsum(lengths[order]) / total
    k = int(np.searchsorted(cum, _NOISE_SHARE, side="right"))
    worst = float(angles[order][min(k, len(order) - 1)])
    return shallow, worst, segments


def measure_contact(tris, *, min_angle_deg: float = MIN_CONTACT_ANGLE_DEG,
                    min_share: float = MIN_PENETRATION_SHARE,
                    max_share: float = MAX_PENETRATION_SHARE) -> GroundContact:
    """How a body standing on the ground meets it, and where its floor goes.

    Read the body's cross-section `min_share` of its height above its lowest point. If the
    surface there rises from the floor at `min_angle_deg` or steeper along (nearly) the whole
    line, the body is SEATED, and the floor goes exactly at its lowest point. Otherwise it is
    GRAZING, and the floor is raised to the lowest height at which the
    body crosses it at `min_angle_deg` everywhere - at least `min_share` and never more than
    `max_share` of the body's height."""
    t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if not len(t):
        raise ValueError("a ground contact needs the body's triangles, and there are none")
    zs = t[:, :, VERTICAL_AXIS]
    lowest, top = float(zs.min()), float(zs.max())
    height = top - lowest
    if height <= 0:
        raise ValueError("a body with no height cannot stand on the ground")
    normals, area = _unit_normals(t)             # outward needs the whole closed body
    probe = lowest + min_share * height
    on_floor = zs.max(axis=1) <= probe
    seat = float(area[on_floor & (normals[:, VERTICAL_AXIS] < -0.999)].sum())
    # only the triangles that reach down into the band the floor may be laid in can cross it
    top_of_band = lowest + max(max_share, min_share) * height
    low = zs.min(axis=1) <= top_of_band
    t, normals = t[low], normals[low]

    shallow, worst, segments = _grazing_share(t, normals, probe, min_angle_deg)
    if shallow <= _NOISE_SHARE:
        return GroundContact(SEATED, lowest, lowest, height, round(worst, 2), seat,
                             _footprint(segments, t, probe))
    # the shallowest cut that meets the angle, from the smallest raise up to the cap
    z = probe
    for k in range(_STEPS + 1):
        z = probe + (top_of_band - probe) * k / _STEPS
        shallow, worst, segments = _grazing_share(t, normals, z, min_angle_deg)
        if shallow <= _NOISE_SHARE:
            break
    points, edges = _weld(segments, height)
    return GroundContact(GRAZING, lowest, z, height, round(worst, 2), seat,
                         _footprint(segments, t, z), capped=shallow > _NOISE_SHARE,
                         line_points=points, line_edges=edges)


def _weld(segments: np.ndarray, height: float):
    """The section's segments as one connected polyline: shared ends become one point, and a
    segment that collapsed to a point is dropped."""
    if not len(segments):
        return (), ()
    pts = segments.reshape(-1, 3)
    key = np.round(pts / (1e-9 * height)).astype(np.int64)
    _uniq, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    ends = inverse.reshape(-1, 2)
    ends = ends[ends[:, 0] != ends[:, 1]]
    ends = np.unique(np.sort(ends, axis=1), axis=0)
    used = np.unique(ends)
    remap = {int(old): new for new, old in enumerate(used)}
    points = tuple(tuple(float(v) for v in pts[first[i]]) for i in used)
    edges = tuple((remap[int(a)], remap[int(b)]) for a, b in ends)
    return points, edges


def _footprint(segments: np.ndarray, tris: np.ndarray, z: float):
    if len(segments):
        pts = segments.reshape(-1, 3)
    else:
        # nothing crosses the plane: the body only touches it (a flat seat, a single point)
        pts = tris.reshape(-1, 3)
        pts = pts[pts[:, VERTICAL_AXIS] <= z]
    lo, hi = pts[:, :2].min(axis=0), pts[:, :2].max(axis=0)
    return (float(lo[0]), float(lo[1])), (float(hi[0]), float(hi[1]))


def describe(contact: GroundContact) -> str:
    """The contact in the engineer's words, for the build note."""
    if contact.kind == SEATED:
        return (f"its sides rise steeply from the ground where it stands, so the floor sits at "
                f"its lowest point (z = {contact.floor_z:.4g} m)")
    mm = 1000.0 * contact.penetration_m
    share = 100.0 * contact.penetration_m / contact.height
    how = (f"where it meets the ground its surface curves down onto the floor, and the gap there "
           f"is too thin for mesh cells, so the floor sits {mm:.3g} mm above its lowest point "
           f"(z = {contact.floor_z:.4g} m, {share:.2g}% of its height) and cuts a small flat "
           f"contact patch")
    if contact.capped:
        how += (f"; the cut stops at {share:.2g}% of the height, where the body meets the floor "
                f"at {contact.contact_angle_deg:.0f} degrees")
    return how


__all__ = ["GRAZING", "MAX_PENETRATION_SHARE", "MIN_CONTACT_ANGLE_DEG", "MIN_PENETRATION_SHARE",
           "SEATED",
           "GroundContact", "contact_line", "describe", "measure_contact"]
