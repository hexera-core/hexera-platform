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

#: A flow path needs a way in and a way out, so this many ports or none. Named here rather than
#: imported from cad/fluid_volume_check, which has its own BOTH_ENDS for the same reason: this
#: module is about triangles and block faces and does not read that one's facts.
BOTH_ENDS = 2

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
    def flush(self) -> bool:
        """The mouth is already ON its block face, so there is nothing to extend - only a patch to
        cut. Carried as a Stub of zero length because everything after this point wants the same
        row either way: which face, where on it, how wide, which passage."""
        return self.length <= 0.0

    @property
    def reach(self) -> tuple:
        """Where the stub ends, past the block face."""
        return tuple(self.centroid[k] + self.normal[k] * self.length for k in range(3))

    @property
    def crossing(self) -> tuple:
        """Where the stub crosses the block face, which is where its patch has to be cut.

        NOT `reach`, which is deliberately PAST the face so the surface crosses it rather than
        landing on it. A box centred on `reach` is centred outside the mesh and catches the face
        only by its own half-width.
        """
        if self.flush:
            return self.centroid     # it is on the face; pulling back by the overshoot would
            #                          put the box a radius inside the mesh instead
        d = self.length - OVERSHOOT * 2.0 * self.radius
        return tuple(self.centroid[k] + self.normal[k] * d for k in range(3))


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


#: Which faces face each other. `plan_ports` names an inlet and an outlet from the block's own
#: faces, and it needs an OPPOSITE PAIR to do it: that is what "a way for the coolant to flow from
#: one side of the part to the other" means.
OPPOSITE = {"xmin": "xmax", "xmax": "xmin", "ymin": "ymax", "ymax": "ymin",
            "zmin": "zmax", "zmax": "zmin"}


def nameable_by_faces(faces) -> bool:
    """Whether a set of block faces holds an opposite pair - which is the whole of `plan_ports`'
    test, written here so this module can tell the shape it must act on from the shape it must
    leave alone."""
    got = set(faces)
    return any(OPPOSITE[f] in got for f in got)


def stubs_for(ports: list, block_lo, block_hi) -> tuple[list, list]:
    """The stubs the mouths of `ports` need, and what to say about the ones that get none.

    `ports` is the surface record's own list (cad/fluid_volume.write_surface): each row carries
    where the mouth is, which way the fluid leaves through it, the area the flow crosses and
    whether anything lies straight ahead of it.

    A MOUTH ALREADY ON A FACE GETS A STUB OF ZERO LENGTH, and only when its faces cannot be named
    the ordinary way. The ordinary way is `renameBoundary` on an opposite pair of block faces, and
    it is what eight of the nine parts in this package use, proven: a channel milled across a
    plate ends flush with the side it was cut on, one mouth per side. Those are untouched here.

    What is not untouched is the plate fed and drained through its LID. Both mouths are flush, so
    neither needs extending; both are on zmax, so `renameBoundary` cannot give them different
    names; and `plan_ports` refuses the part for "reaching no opposite pair of block faces" -
    true of the block and nothing about the plumbing. Measured end to end on 2026-10-29, on the
    ported two-scale plate driven through the console:

        conjugate: meshing without named ports - the coolant reaches no opposite pair of block
        faces ... It reaches zmax.

    A mesh with no inlet and no outlet is not solvable, which is the whole reason the port CUTTER
    exists - and the only way into it was through stubbing, so a mouth that needed no stub could
    never reach it.
    """
    out: list = []
    flush: list = []
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
            flush.append(Stub(port=k, passage=int(p.get("passage", 0)), face=face,
                              centroid=centroid, normal=normal, radius=radius,
                              length=0.0, volume=0.0))
            continue
        length = distance + OVERSHOOT * 2.0 * radius
        out.append(Stub(port=k, passage=int(p.get("passage", 0)), face=face, centroid=centroid,
                        normal=normal, radius=radius, length=length,
                        volume=math.pi * radius * radius * length))
    if flush:
        reached = [st.face for st in (*out, *flush)]
        # TWO OF THEM, OR NONE. A cut port is work and risk, and one mouth buys neither a flow
        # path nor a name worth having: `port_rows` says "a solve needs two" and a lone inlet with
        # no outlet is as unsolvable cut as it is uncut. The existing row for this is
        # test_a_mouth_already_flush_with_a_face_is_left_alone, which caught the first version of
        # this rule cutting a single flush mouth out of xmax for nothing.
        if len(out) + len(flush) < BOTH_ENDS:
            said.append(
                f"{len(flush)} mouth(s) already lie on a block face, and one mouth is not a flow "
                "path - so nothing is cut: a port needs a way in and a way out")
        elif nameable_by_faces(reached):
            said.append(
                f"{len(flush)} mouth(s) already lie on a block face that can be renamed, so they "
                "keep the block's own names rather than being cut out of it")
        else:
            # SAID FOR BOTH SHAPES IT COVERS. Two mouths on one face is the commonest - a plate
            # fed and drained through its lid - and one on zmax with one on xmax is the same
            # problem: a renamed face gives the WHOLE face one name, so neither arrangement can
            # carry an inlet and an outlet the coolant flows between.
            said.append(
                f"{len(flush)} mouth(s) already lie on a block face, and the faces reached "
                f"({', '.join(sorted(set(reached)))}) hold no opposite pair - so they are CUT out "
                "of their faces instead of renaming them: a renamed face carries one name for the "
                "whole face, and the coolant needs a way in and a way out")
            out.extend(flush)
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
    # A FLUSH MOUTH HAS NOTHING TO DRAW. It is already on the face; the row exists so the patch
    # gets cut, and joining a zero-length tube onto the coolant would add a degenerate ring of
    # triangles to a surface the mesher reads with surfaceToCell.
    if stub.flush:
        return []
    return _stub_triangles(stub, sides)


def _stub_triangles(stub: Stub, sides: int = STUB_SIDES) -> list:
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


#: How far a declared port may be from the mouth it names, as a multiple of the mouth's own
#: diameter. The declaration's `near_mm` comes from the stage, where the user confirmed an opening
#: the scout measured off the same faces - so they agree closely, and this is slack for the
#: difference between a face's centroid and the mouth the inside-space map read on the skin.
DECLARED_WITHIN = 2.0


def declared_for(stubs: list, declared: list) -> tuple[list | None, str]:
    """Each stub's name and role from the DECLARATION, bound by where the port is.

    WHY NOT BY ORDER. `port_rows` names the first mouth it measured the inlet and says out loud
    that it is a guess. The user has already said which is which, at the stage, with a position
    for each - so the guess is unnecessary, and acting on it instead cost the coupon plate its
    whole run:

        failed_gate patch_contract: The mesh has no boundary called 'inlet' or 'outlet' (it has
        'coolant_in' and 'coolant_out') ... so the mesh does not match what you approved.

    The mesh was built. It was refused for calling the ports by this module's default names.

    BY LOCATION AND NOT BY THE ORDER THE DECLARATION IS IN EITHER, because the two lists are
    measured by different code on different surfaces: the declaration off the B-rep faces at
    intake, the stubs off the coolant's own mouths. Nothing promises they come out in the same
    order, and an inlet and an outlet swapped is a case that solves and runs backwards.

    Returns (None, why) when every stub cannot be named, because a partly declared set is worse
    than the honest default: half the patches would carry the user's names and half would not.
    """
    rows: list = []
    if not stubs or not declared:
        return None, ""
    used: set[int] = set()
    for st in stubs:
        want = 1000.0 * DECLARED_WITHIN * 2.0 * st.radius          # mm
        best, best_d = None, float("inf")
        for i, d in enumerate(declared):
            if i in used:
                continue
            near = d.get("near_mm") or []
            if len(near) != 3:
                continue
            gap = math.sqrt(sum((1000.0 * st.centroid[a] - float(near[a])) ** 2
                                for a in range(3)))
            if gap < best_d:
                best, best_d = i, gap
        if best is None or best_d > want:
            return None, ("the declared ports could not be matched to the mouths by position, so "
                          "the mesh keeps this engine's own port names")
        used.add(best)
        d = declared[best]
        role = str(d.get("type") or "").strip().lower()
        rows.append((str(d.get("name") or ""), role if role in ("inlet", "outlet") else "wall"))
    if not all(n for n, _r in rows):
        return None, "a declared port has no name"
    return rows, ("the ports are cut under the names declared at intake, matched to the mouths by "
                  "position: " + ", ".join(f"{n} ({r})" for n, r in rows))


def shell_stubs(ports: list) -> list:
    """Every mouth as a port to cut on the part's own outside, with nothing to extend.

    A STUB EXISTS BECAUSE THE COOLANT HAS TO REACH A BLOCK FACE. On a shell domain it already
    does: the domain is metal fused with the coolant, so the fuse capped each mouth with the
    coolant's own end face and the mouth IS part of the boundary. There is no gap to bridge, no
    geometry added to the coolant, and no `extend` pass - only the patch to cut, which is what a
    Stub of zero length has always carried (see `Stub.flush`).

    Every mouth becomes a port, in the order they were measured. Which block faces the coolant
    happens to reach does not come into it, and that is the point: `plan_ports` refused the first
    customer assembly for reaching no opposite pair of faces, which was true of its cuboid and
    said nothing about its plumbing.
    """
    from meshpipeline.engines.cfmesh_conjugate import conjugate as C

    out: list[Stub] = []
    for k, port in enumerate(ports or []):
        centroid = tuple(float(v) for v in (port.get("centroid_m") or ()))
        normal = tuple(float(v) for v in (port.get("normal") or ()))
        radius = 0.5 * float(port.get("diameter_m") or 0.0)
        if len(centroid) != 3 or len(normal) != 3 or radius <= 0.0:
            continue
        out.append(Stub(port=k, passage=int(port.get("passage") or 0), face=C.OUTER_PATCH,
                        centroid=centroid, normal=normal, radius=radius,
                        length=0.0, volume=0.0))
    return out


def port_rows(stubs: list, *, inlet: str = "coolant_in",
              outlet: str = "coolant_out",
              named: list | None = None) -> tuple[list, str]:
    """The stubs as patches to cut, and what to say about which end is which.

    WHY THIS IS NOT `plan_ports`. That one chooses an inlet and an outlet from the BLOCK FACES the
    coolant reaches, which is the right question while one port lands on each face. These are cut
    out of a face, so two of them can share one - and on the first customer assembly they do,
    because both fittings point +x. `plan_ports` refuses that part for reaching no opposite pair,
    which is true of its block and says nothing about its plumbing.

    WHICH END IS COLD CANNOT BE INFERRED, and this does not pretend otherwise. Nothing in a pair
    of bores says which one is fed; the order here is the order the mouths were measured in, said
    out loud, exactly as `plan_ports` says it for the two-faces-not-opposite case. A declared pair
    of names from intake overrides it.

    Fewer than two stubs is not an error here: a part with one way in and no way out is refused by
    the flowable gate with the numbers to show for it, which is a better place to say so than a
    planner that has not meshed anything yet.
    """
    rows = []
    for k, s in enumerate(stubs):
        if named is not None and k < len(named):
            name, role = named[k][0], named[k][1]
        else:
            role, name = (("inlet", inlet) if k == 0 else
                          ("outlet", outlet) if k == 1 else
                          ("wall", f"mouth_{k + 1}"))
        rows.append({"name": name, "type": role, "face": s.face,
                     "centre_m": tuple(float(v) for v in s.crossing),
                     "diameter_m": 2.0 * float(s.radius)})
    if len(stubs) < 2:
        return rows, (f"{len(stubs)} mouth(s) can be opened to a block face, and a solve needs "
                      "two: one to be fed through and one to leave by")
    first, second = ((named[0][0], named[1][0]) if named is not None and len(named) > 1
                     else (inlet, outlet))
    said = (f"{first} and {second} are cut out of "
            + (f"{stubs[0].face}" if stubs[0].face == stubs[1].face
               else f"{stubs[0].face} and {stubs[1].face}")
            + f", {2000.0 * stubs[0].radius:.3f} mm and {2000.0 * stubs[1].radius:.3f} mm across")
    if stubs[0].face == stubs[1].face:
        said += (". Both are on one face, which a renamed block face cannot express and is why "
                 "they are cut rather than renamed")
    if len(stubs) > 2:
        said += (f". The other {len(stubs) - 2} mouth(s) are cut as walls: which of several is "
                 "fed is not something the geometry says")
    said += (f". The direction is a guess - {inlet} is the mouth measured first - so name them "
             "at intake to fix it")
    return rows, said
