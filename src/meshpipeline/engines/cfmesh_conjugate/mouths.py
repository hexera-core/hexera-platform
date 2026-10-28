# Responsibility: Give a coolant mouth that stops inside the block a way out to a block face, and
# refuse the ones that may not have it.
# Owns: which mouths get a stub, where each one goes, its geometry and what it adds.
# Boundaries: triangles and numbers. It runs no mesher, writes no dictionary and names no patch -
#             plan.py decides the names and conjugate.py writes the dictionaries.
"""A mouth that does not reach the block is given a stub that does.

WHY THIS EXISTS. A conjugate case takes its inlet and outlet from the BLOCK's six named faces, and
that works only while the coolant's mouths lie ON those faces. Eight of the nine parts in this
package do, because a channel milled across a plate ends flush with the side it was cut on. The
first real customer assembly does not. A Corintis cold plate's mouths are fitting BORES, and the
plate's bounding box is set by the fittings' own bodies, so the coolant stopped 2 mm inside the
block on every side and the mesher refused:

    "the coolant reaches no opposite pair of block faces, so there is no obvious way for it to
     flow from one side of the part to the other. It reaches no face at all: it is sealed inside
     the metal."

THE STUB IS A PIPE, NOT A FUDGE. Extending a port out to the boundary along its own axis is what
anyone setting up a case does by hand at an inlet: a mouth flush with the domain wall has no length
for its profile to form in, and the first cells see a top-hat the real plumbing never delivers.
Here it does double duty, because it is also what puts the mouth on a face the mesher can name.

IT IS ONLY DRAWN WHERE THE MOUTH OPENS INTO AIR. `clear_ahead` on the surface record says nothing
of the parts lies straight ahead of it. A mouth that opens into ANOTHER PART's bore gets no stub,
because the stub would be a passage bored through metal that is not in the customer's CAD, and
where that part's own bore reaches the outside is a question for whoever drew it. On the Corintis
plate exactly one of the three mouths is like that, and the extraction had already said so: "mouth
1 opens into another part whose inside reaches the outside some other way".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: A stub is drawn as this many sides. Round enough that the mouth's area survives the
#: approximation - a 24-gon is 0.57% under its circle - and few enough to cost nothing.
STUB_SIDES = 24

#: How far PAST the block face the stub is drawn, as a fraction of the mouth's own diameter. It has
#: to CROSS the boundary rather than land on it: a surface ending exactly on the face leaves
#: surfaceToCell deciding a coincident-face question whose answer depends on rounding. Everything
#: past the face lies outside the block and is never meshed.
OVERSHOOT = 0.5

#: A mouth nearer than this to the face it heads for, as a fraction of its own diameter, is already
#: as good as on it and gets no stub: `open_faces` will have found it.
ALREADY_THERE = 0.05

#: The six block faces, as the rest of this bundle names them, with the axis and sign of each.
FACES = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
_AXIS = {"xmin": (0, -1), "xmax": (0, 1), "ymin": (1, -1), "ymax": (1, 1),
         "zmin": (2, -1), "zmax": (2, 1)}


@dataclass(frozen=True)
class Stub:
    """One mouth's way out: where it goes, how far, and what it costs."""

    port: int                       # index into the surface record's "ports"
    passage: int                    # which passage the mouth belongs to
    face: str                       # the block face it reaches
    centroid: tuple                 # the mouth, metres
    normal: tuple                   # out of the fluid
    radius: float                   # metres, from the mouth's own flow area
    length: float                   # metres, mouth to just past the face
    volume: float                   # metres^3 it adds to the coolant

    @property
    def reach(self) -> tuple:
        """Where the stub ends, past the block face."""
        return tuple(self.centroid[k] + self.normal[k] * self.length for k in range(3))


def _unit(v) -> tuple:
    n = math.sqrt(sum(float(c) ** 2 for c in v))
    return tuple(float(c) / n for c in v) if n > 0 else (0.0, 0.0, 0.0)


def face_ahead(centroid, normal, block_lo, block_hi) -> tuple[str, float]:
    """Which block face a mouth heads for, and how far along its own axis that is.

    ("", inf) when it points at none of them, which cannot happen for a mouth on the coolant's own
    boundary inside the block but is cheap to refuse rather than assume.
    """
    d = _unit(normal)
    best, best_t = "", float("inf")
    for face in FACES:
        axis, sign = _AXIS[face]
        if d[axis] * sign <= 1e-9:          # not travelling towards this face
            continue
        wall = block_hi[axis] if sign > 0 else block_lo[axis]
        t = (wall - centroid[axis]) / d[axis]
        if 0.0 <= t < best_t:
            best, best_t = face, t
    return best, best_t


def stubs_for(ports: list, block_lo, block_hi) -> tuple[list, list]:
    """The stubs the mouths of `ports` need, and what to say about the ones that get none.

    `ports` is the surface record's own list (cad/fluid_volume.write_surface): each row carries
    where the mouth is, which way the fluid leaves through it, the area the flow crosses and
    whether anything lies straight ahead of it.
    """
    out: list = []
    said: list = []
    for k, p in enumerate(ports):
        centroid = tuple(float(v) for v in p["centroid_m"])
        normal = _unit(p["normal"])
        radius = math.sqrt(max(float(p["area_m2"]), 0.0) / math.pi)
        where = ", ".join(f"{v * 1000:.1f}" for v in centroid)
        if radius <= 0.0:
            said.append(f"mouth {k + 1} has no flow area, so it is not a port")
            continue
        if not p.get("clear_ahead", True):
            said.append(f"mouth {k + 1} at ({where}) mm opens into another part rather than into "
                        "air, so it is left sealed: a stub there would be a passage bored through "
                        "metal that is not in the CAD")
            continue
        face, distance = face_ahead(centroid, normal, block_lo, block_hi)
        if not face:
            said.append(f"mouth {k + 1} at ({where}) mm points at no block face")
            continue
        if distance <= ALREADY_THERE * 2.0 * radius:
            continue                        # flush already; open_faces will have found it
        length = distance + OVERSHOOT * 2.0 * radius
        out.append(Stub(port=k, passage=int(p.get("passage", 0)), face=face, centroid=centroid,
                        normal=normal, radius=radius, length=length,
                        volume=math.pi * radius * radius * length))
    return out, said


def _basis(normal) -> tuple:
    """Two unit vectors across `normal`, for drawing the stub's rim."""
    n = _unit(normal)
    other = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _unit((n[1] * other[2] - n[2] * other[1],
               n[2] * other[0] - n[0] * other[2],
               n[0] * other[1] - n[1] * other[0]))
    v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
    return u, v


def stub_triangles(stub: Stub, sides: int = STUB_SIDES) -> list:
    """The stub as triangles wound OUT of the fluid, ready to join the coolant surface.

    A tube, a far cap, AND A NEAR CAP WOUND THE OTHER WAY. The near cap is coincident with the
    mouth's own cap, which the coolant surface already carries, and opposite to it - so the two
    cancel and the joined surface encloses the coolant plus the stub, with the mouth no longer a
    wall. Cutting the original cap out instead would be cleaner, and it would mean finding its
    triangles in a surface this module deliberately knows nothing about; `added_volume_agrees`
    exists so the cancellation is checked rather than assumed.

    THE TUBE'S WINDING IS DERIVED, NOT GUESSED, because it was guessed first and it was wrong. With
    (u, v, n) right-handed and the rim at angle t being c + r(cos t u + sin t v), the triangle
    (a0, b0, b1) has normal (b0 - a0) x (b1 - b0) = L r (n x rho1 - n x rho0), and n x rho is
    cos t v - sin t u, which at t = 0 leaves -u: pointing INTO the tube. The two tube triangles are
    therefore wound the other way round. The caps are already right by the same derivation: the far
    cap gives +n and the near cap -n. Measured on the customer's two mouths before and after -
    -840.735 mm3 against an expected 2551.247, then 2551.247 - which is what `added_volume_agrees`
    is for, since a closed surface that encloses the wrong volume raises no other complaint.
    """
    u, v = _basis(stub.normal)
    r = stub.radius
    c0, c1 = stub.centroid, stub.reach

    def ring(centre, k):
        a = 2.0 * math.pi * (k % sides) / sides
        return tuple(centre[j] + r * (math.cos(a) * u[j] + math.sin(a) * v[j]) for j in range(3))

    tris = []
    for k in range(sides):
        a0, a1 = ring(c0, k), ring(c0, k + 1)
        b0, b1 = ring(c1, k), ring(c1, k + 1)
        tris.append((a0, b1, b0))           # the tube, outward
        tris.append((a0, a1, b1))
        tris.append((c1, b0, b1))           # the far cap, facing along +normal
        tris.append((c0, a1, a0))           # the near cap, facing back along -normal
    return tris


def signed_volume(triangles) -> float:
    """What a closed set of triangles encloses, by its own winding."""
    total = 0.0
    for a, b, c in triangles:
        total += (a[0] * (b[1] * c[2] - b[2] * c[1])
                  - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return total / 6.0


def added_volume_agrees(before: float, after: float, stubs: list, tol: float = 0.02) -> bool:
    """Whether joining the stubs added exactly the volume they say they are.

    THE CHECK THAT THE CAPS CANCELLED. If a near cap were wound the same way as the mouth's own,
    the pair would enclose nothing between them and the stub's volume would go missing - and the
    surface would still be closed, so nothing else downstream would notice. A stub is a cylinder
    whose volume is known in closed form, so this is a real comparison and not a plausibility
    check. The tolerance covers the 24-gon's 0.57% shortfall against its circle.
    """
    want = sum(s.volume for s in stubs)
    if want <= 0.0:
        return abs(after - before) <= tol * max(abs(before), 1e-30)
    return abs((after - before) - want) <= tol * want


def extend(triangles: list, stubs: list) -> tuple[list, float]:
    """The coolant's triangles with every stub joined on, and the volume they added."""
    out = list(triangles)
    for s in stubs:
        out.extend(stub_triangles(s))
    return out, sum(s.volume for s in stubs)
