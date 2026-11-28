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
    #: Whether the surface reading found NOTHING of the assembly straight ahead of this mouth,
    #: from the record's own `clear_ahead`. Named for what it measures and not for what it is
    #: taken to mean: `cad/fluid_volume._port` sets it False when a single ray along the mouth's
    #: normal meets any solid or loose face before leaving the assembly's own bounding box. A
    #: mouth that opens to real air still reads False whenever anything of the same assembly sits
    #: further along inside that box - the producer's own comment names a bracket and a cross-over
    #: tube - and such a mouth WOULD mesh and WOULD carry faces. So this is one signal, not a
    #: verdict: `fluid_volume_check` prints it as a question for the picture and scores it 0.5,
    #: and `cad/scout._name_openings` was deliberately moved OFF it for role decisions on
    #: 2026-11-13 and 2026-11-14 because it does not mean the same thing at each of the three
    #: producers that set it.
    #:
    #: True when nobody measured it, which neither promotes nor demotes a mouth: an older record
    #: or another extractor must not start refusing parts that were fine. An explicit null counts
    #: as unmeasured too, which `.get(key, True)` alone would not give.
    ahead_is_clear: bool = True
    #: Whether the surface reading ALSO found an open rim at this mouth that it marked sealed.
    #: This is the second signal, and the one that makes the first safe to refuse a run on: a
    #: mouth with air in front of it and a bracket beyond has `ahead_is_clear` False and no sealed
    #: rim, while the customer cold plate's blind 11.8 mm lid port has both - its rim of 63 edges,
    #: 35.941 mm long, sits 0.4998 mm along its own normal and is marked `"sealed": true`.
    sealed_rim: bool = False

    @property
    def is_sealed(self) -> bool:
        """BOTH SIGNALS, because either alone is wrong in a way that costs something real.

        `ahead_is_clear` False alone would refuse a part whose mouth opens to air across a gap
        from a bracket, which meshes and delivers. A sealed rim alone says the coolant surface
        closes there without saying the metal does. Together they are the measured shape of the
        one case this is for: a mouth capped by its own surface with the rest of the assembly
        immediately beyond it.
        """
        return (not self.ahead_is_clear) and self.sealed_rim

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

    Returns (None, why) when a DECLARED PORT cannot be named, because a partly declared set is
    worse than the honest default: half the patches would carry the user's names and half would
    not.

    A MOUTH WITH NO DECLARED PORT IS NOT THAT CASE, and reading it as one cost this part its run.
    The loop used to run over the STUBS and demand that every one of them find a declaration, so a
    part with more mouths than declared ports could never carry the user's names at all - the one
    unmatchable mouth threw away the matches that had already succeeded. The first real customer
    cold plate is exactly that shape: three mouths, two declared ports, and the third mouth is the
    one the customer had just marked "not an opening" on the stage. It came out as

        the declared ports could not be matched to the mouths by position

    with `inlet` sitting 0 mm from a mouth of exactly its declared 11.8 mm and `outlet_1` 0 mm from
    one of exactly its declared 8 mm. Measured: the same declaration against the first two mouths
    alone matches both perfectly, so neither the tolerance nor the position-less `wall` row was
    the cause - it was the third mouth having nothing to match to.

    `port_rows` has always had somewhere to put such a mouth: past the first two it cuts them as
    walls under `mouth_N`, and says so. So the loop now runs over the DECLARATION - every declared
    port must find a mouth, which is the half that must not be partial - and any mouth left over
    takes that same wall default.
    """
    if not stubs or not declared:
        return None, ""
    #: Only a port can be matched by position. A declared `wall` carries no `near_mm` and names no
    #: mouth; it is the outer surface, and `declared_port_names` is what reads it.
    # `opening` IS A PORT HERE, AND LEAVING IT OUT COST A DELIVERY. A mouth whose direction
    # nobody declared is still a mouth the customer NAMED and POINTED AT, and its `near_mm` is
    # the only thing that ties their name to a hole. Dropped, this returns (None, "") and
    # `port_rows` falls through to its own index-based naming over `rec["ports"]` - which is
    # sorted largest passage first, then widest hole first.
    #
    # WHAT THAT COST, measured on job a9fa0aba. The card named the 8 mm bore at
    # (99.9, 54.7, 43.3) `mouth_1` and marked the 11.8 mm lid bore "not an opening". The carve's
    # own order put the 11.8 mm bore at index 0, so it minted `mouth_1` onto the hole the
    # customer had just excluded and shifted both real mouths by one. The run meshed for 161
    # minutes and was refused with "The mesh has no boundary called 'mouth_1'".
    #
    # AND THE REFUSAL WAS LUCK, which is the part that matters. That bore is CAPPED, so topoSet
    # found no faces and createPatch deleted the patch - which is the only reason the mismatch
    # became visible. Had the lid port been open, the same run would have DELIVERED a mesh whose
    # `mouth_1` was the wrong hole and the gate would have passed it: the contract compares
    # names, and the name was there. The delivered `mouth_2` of that run does sit on the bore the
    # customer called `mouth_1`.
    ports = [d for d in declared
             if len(d.get("near_mm") or []) == 3
             and str(d.get("type") or "").strip().lower() in ("inlet", "outlet", "opening")]
    if not ports:
        return None, ""
    rows: list = [None] * len(stubs)
    used: set[int] = set()
    for d in ports:
        near = d["near_mm"]
        best, best_d = None, float("inf")
        for i, st in enumerate(stubs):
            if i in used:
                continue
            gap = math.sqrt(sum((1000.0 * st.centroid[a] - float(near[a])) ** 2
                                for a in range(3)))
            if gap < best_d:
                best, best_d = i, gap
        # the slack is the MATCHED mouth's own, as it always was: a wide bore may sit further from
        # the face centroid the stage measured than a narrow one.
        if best is None or best_d > 1000.0 * DECLARED_WITHIN * 2.0 * stubs[best].radius:
            return None, ("the declared ports could not be matched to the mouths by position, so "
                          "the mesh keeps this engine's own port names")
        name = str(d.get("name") or "")
        if not name:
            return None, "a declared port has no name"
        used.add(best)
        rows[best] = (name, str(d.get("type") or "").strip().lower())
    # EVERY MOUTH THE DECLARATION DID NOT CLAIM: cut as a wall, under the name `port_rows` would
    # have given it anyway.
    #
    # STILL A WALL, AND THAT WAS A DECISION. It was argued that the leftover should take the role
    # its own shape earns - `wall` when `Stub.is_sealed`, `opening` otherwise - on the grounds
    # that `wall` quietly seals a hole the mesh may need open. That argument is about an
    # openings-only declaration, where every mouth is an opening and none should become a wall.
    # Here the declaration has named its ports and a spare mouth is a hole the customer did not
    # ask for, so `wall` delivers exactly the ports promised and nothing else. Three tests pin
    # it deliberately (test_a_cut_port_wears_the_name_the_user_approved), on this part it reads
    # the same either way because the one leftover is the capped lid bore, and there is no
    # measurement behind the change - so it is not made.
    #
    # AND NOT UNDER A NAME THE CUSTOMER ALREADY USED, which `mouth_{i + 1}` cannot promise once
    # the declaration binds by position. On job a9fa0aba the customer's `mouth_1` binds to stub 1
    # and the unclaimed capped bore is stub 0, so the old rule minted a SECOND `mouth_1` onto it:
    # two patches, one name, which is worse than the mismatch this function was fixed to stop. The
    # index is still the default, so a run where nothing collides reads exactly as it always did.
    # `rows` still holds a None for every unclaimed mouth at this point, so the names are
    # read off the filled entries only.
    taken = {r[0] for r in rows if r}
    for i in range(len(stubs)):
        if rows[i] is None:
            k = i + 1
            while f"mouth_{k}" in taken:
                k += 1
            spare = f"mouth_{k}"
            taken.add(spare)
            rows[i] = (spare, "wall")
    told = ", ".join(f"{n} ({r})" for n, r in rows if r in ("inlet", "outlet"))
    spare = sum(1 for _n, r in rows if r == "wall")
    return rows, ("the ports are cut under the names declared at intake, matched to the mouths by "
                  "position: " + told
                  + (f"; the other {spare} mouth(s) are cut as walls" if spare else ""))


def shell_stubs(ports: list, rims: list | None = None, outer: str = "") -> list:
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

    EVERY MOUTH, INCLUDING ONE NOTHING IS CLEAR AHEAD OF. `stubs_for` skips such a mouth and is
    right to: there a stub would be a passage bored through metal that is not in the CAD. Here
    there is nothing to bore, cutting is free, and `empty_patches` came out EMPTY on job 6ddb58aa
    because createPatch deletes a zero-sized patch, so no gate can fire on it. The two signals are
    carried on the Stub instead, so `sealed_rows` can refuse a port DECLARED on such a mouth
    before the mesher starts, which is the thing that was costing whole runs.

    `rims` is the record's `open_rims`, matched to a mouth by position. Pass it: without it every
    mouth reads unsealed and `sealed_rows` can never refuse, which is the safe direction but not
    the useful one.
    """
    from meshpipeline.engines.cfmesh_conjugate import conjugate as C

    sealed_at = [tuple(float(v) for v in (r.get("centroid") or ()))
                 for r in (rims or [])
                 if isinstance(r, dict) and r.get("sealed") and len(r.get("centroid") or ()) == 3]
    out: list[Stub] = []
    for k, port in enumerate(ports or []):
        centroid = tuple(float(v) for v in (port.get("centroid_m") or ()))
        normal = tuple(float(v) for v in (port.get("normal") or ()))
        radius = 0.5 * float(port.get("diameter_m") or 0.0)
        if len(centroid) != 3 or len(normal) != 3 or radius <= 0.0:
            continue
        # An EXPLICIT null is unmeasured, not sealed. `.get(key, True)` only defaults an ABSENT
        # key, so `{"clear_ahead": None}` would otherwise read as a mouth with metal in front.
        ahead = port.get("clear_ahead", True)
        # Within one diameter of the mouth, which on the part this was written against is a rim
        # 0.4998 mm from an 11.8 mm mouth. Wide enough for the rim of the mouth itself and far
        # too narrow to reach another one: that part's other mouths are 23 mm and 34 mm away.
        near = any(math.dist(centroid, c) <= 2.0 * radius for c in sealed_at)
        # `outer` IS WHAT THIS RUN CALLS THE PART'S OUTSIDE, which on the shell path may be
        # the user's own word for it. The cut dict selects faces by this name, so it has to be
        # the name the surface was written under, not the module default.
        out.append(Stub(port=k, passage=int(port.get("passage") or 0),
                        face=(outer or C.OUTER_PATCH),
                        centroid=centroid, normal=normal, radius=radius,
                        length=0.0, volume=0.0,
                        ahead_is_clear=True if ahead is None else bool(ahead),
                        sealed_rim=bool(near)))
    return out


def sealed_rows(stubs: list, rows: list | None) -> str:
    """Why a port cannot be cut where it is about to be cut, or "" when it can.

    ASKED OF THE ROWS `port_rows` PRODUCED, not of the declaration it was built from, and that is
    the whole of the fix. An earlier version read `declared_for`'s output instead, which left the
    exact failure it exists to stop still reachable: a declaration that carries inlet and outlet
    roles but no bindable `near_mm` makes `declared_for` return None with an EMPTY reason, so
    nothing is logged, `port_rows(roles=True)` falls through to its own defaults, and mouth 0 is
    named `coolant_in`. Driven on job e8256476's own record with one `near_mm` moved out of
    tolerance, that rebuilds job 6ddb58aa's recorded plan field for field - `coolant_in`, inlet,
    diameter 0.0118, at the blind mouth - the plan that meshed 283,410,955 cells in 11,217.3 s and
    was refused at the end. Reading the rows closes the declared path, the defaulted path, the box
    path and any default added later, because all four end here.

    TWO SIGNALS, NOT ONE, and `Stub.is_sealed` carries the argument. `clear_ahead` alone is a
    single ray down the mouth's normal bounded at the assembly's bounding box, so a mouth opening
    to real air with a bracket beyond it reads False - and that mouth meshes, carries faces and
    delivers. Refusing a run on it would be worse than the defect being fixed.

    MEASURED TWICE ON THE SAME PART, both at the end of a mesh:

      job 6ddb58aa  no declaration existed, so the engine INVENTED coolant_in on the blind 11.8 mm
                    lid port. 0 of wall_outer's 2,640 faces in its box; "Removed zero-sized patch
                    coolant_in type patch at position 2"; refused for "the inlet was to be
                    coolant_in, and the fluid region has no patch of that name". 283,410,955
                    cells, 11,217.3 s of mesher time on an n2-highmem-64.
      job e8256476  the USER declared that same mouth outlet_2, under a patches_contract.txt
                    headed "USER-CONFIRMED AT INTAKE - ENFORCED BY THE PIPELINE". The identical
                    box, the identical 0 faces, the same removal, refused for "the outlet was to
                    be outlet_2". 14,910.8 s, on a second 64-core machine.

    A REFUSAL rather than a warning, because the mesh is refused either way: run through job
    e8256476's own figures, `_gate_flowable` and `verify`'s port block both refuse a declared port
    that ends with no faces. What changes is that it is said before the machine is paid for.

    A `wall` is not a flow boundary and is not refused: job 6ddb58aa's own plan put one on its
    third mouth and that was correct.
    """
    if not stubs or not rows:
        return ""
    bad, by_passage = [], {}
    for k, st in enumerate(stubs):
        row = rows[k] if k < len(rows) else None
        if not isinstance(row, dict):
            continue
        role = str(row.get("type") or "").strip().lower()
        # AN `opening` TOO - BUT ONLY ONE A DECLARATION BOUND. Once `declared_for` can bind an
        # `opening`, it can bind one to a capped mouth, which is the 161-minute refusal this
        # check exists to price before the machine is paid for: "a patch cut on one comes back
        # with no faces at all, so createPatch deletes it and the finished mesh is refused for a
        # boundary it does not carry".
        #
        # AND NOT THE ENGINE'S OWN DEFAULT, which is where a first version of this went wrong.
        # With nothing declared, `port_rows` gives EVERY mouth the role `opening` - so refusing
        # on the word alone refused a run in which the customer had promised nothing at all.
        # `test_nothing_named_is_nothing_refused` is that rule, and it is right: a name the
        # engine minted for itself is not in the patch contract, so nothing downstream can be
        # refused for its absence. An inlet or an outlet stays refused either way, declared or
        # invented, because `plan.ports_from` reads those roles back out and the flowable gate
        # then holds the run to a port the run invented.
        asked = role in ("inlet", "outlet") or (role == "opening" and bool(row.get("declared")))
        if not asked or not st.is_sealed:
            continue
        where = ", ".join(f"{v * 1000:.1f}" for v in st.centroid)
        bad.append(f"the {role} {row.get('name')} is to be cut on the "
                   f"{2000.0 * st.radius:.3f} mm mouth at ({where}) mm")
        by_passage.setdefault(st.passage, []).append(k)
    if not bad:
        return ""
    many = len(bad) > 1
    it, they, that = ("them", "those mouths", "Those mouths") if many else ("it", "that mouth",
                                                                            "That mouth")
    said = ("; ".join(bad) + f". {that} "
            + ("have" if many else "has")
            + " another part of the assembly straight ahead of "
            + f"{it}, inside the part's own bounding box, and the surface reading closed "
            + ("them" if many else "it")
            + " off with a rim of its own that it marked sealed. Both readings together are what "
            "a capped mouth looks like, and a patch cut on one comes back with no faces at all, "
            "so createPatch deletes it and the finished mesh is refused for a boundary it does "
            "not carry. That has cost two full runs on this shape already")

    # THE REMEDY THE PART CAN ACTUALLY TAKE, decided from the passage numbers already in hand
    # rather than offered blind. "Declare it on a mouth that opens to the outside" is impossible
    # on the part this was written against: its stubs carry passages [0, 0, 1] and only mouth 2
    # on passage 0 opens outward, and that one is already the other end - so the advice sent the
    # customer straight into `ports_on_one_passage`'s refusal instead, which is the check that
    # knows the answer and which never runs because this one refuses first.
    free = [(k, st) for k, st in enumerate(stubs)
            if not st.is_sealed and st.passage in by_passage]
    if free:
        where = "; ".join(f"{2000.0 * st.radius:.3f} mm at "
                          + ", ".join(f"{v * 1000:.1f}" for v in st.centroid) + " mm"
                          for _k, st in free)
        said += (f". On the same passage {'these mouths do' if len(free) > 1 else 'this mouth does'}"
                 f" open outward and could carry the role instead: {where}")
    else:
        said += (". No other mouth on the same passage opens outward, so there is nowhere on this "
                 "part to move the role to: the metal ahead has to be opened in the CAD")
    return said


def port_rows(stubs: list, *, inlet: str = "coolant_in",
              outlet: str = "coolant_out",
              named: list | None = None, roles: bool = True) -> tuple[list, str]:
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

    AND `roles=False` NAMES NO END AT ALL, which is what a part whose intake declared no ports has
    to get. The mouths are still CUT - the delivered mesh carries every opening as a named,
    unassigned patch a solver setup can be put on later - but nothing here guesses which one is
    fed. Guessing is not harmless, because `plan.ports_from` reads the roles back out of these
    rows and `finalize` sets `coolant_ports_declared` from that, so the flowable gate ends up
    holding the run to a port the run itself invented. MEASURED, job 6ddb58aa: the customer's own
    notes said not to name any ports on the file and that the boundary patch list is empty, no
    port declaration was written, and the plan still carried `coolant_in` as the inlet on an
    11.8 mm mouth whose own record says nothing is clear ahead of it. topoSet found 0 of the
    coolant's 2,640 outer faces in its box, createPatch removed the zero-sized patch, and 3.94 h
    of meshing was refused with "the inlet was to be coolant_in, and the fluid region has no patch
    of that name". [[project-corintis-port-roles]]
    """
    rows = []
    for k, s in enumerate(stubs):
        # WHETHER A DECLARATION PUT THIS ROLE HERE, which `sealed_rows` needs and cannot infer.
        # A leftover `declared_for` filled in for an unclaimed mouth rides in `named` too, but a
        # sealed one of those is a `wall` and a `wall` is never refused, so the flag is safe to
        # set for the whole branch.
        from_declaration = False
        if named is not None and k < len(named):
            name, role = named[k][0], named[k][1]
            from_declaration = True
        elif not roles:
            # "opening" is not in ROLE_TO_OF_TYPE, and `render_port_patch_dicts` falls back to
            # OpenFOAM type `patch` for a role it does not know - which is the unassigned shape
            # this customer's two mouths have already been shipped under as port_a and port_b.
            # A `wall` would be the wrong physics for a real hole in a mesh that has to be
            # solvable later, and `finalize.delivered_boundary_types` reads the OF type back out
            # of the boundary file rather than this role word, so nothing downstream needs it.
            role, name = "opening", f"mouth_{k + 1}"
        else:
            role, name = (("inlet", inlet) if k == 0 else
                          ("outlet", outlet) if k == 1 else
                          ("wall", f"mouth_{k + 1}"))
        rows.append({"name": name, "type": role, "face": s.face,
                     "centre_m": tuple(float(v) for v in s.crossing),
                     "diameter_m": 2.0 * float(s.radius),
                     "declared": from_declaration})
    if len(stubs) < 2:
        return rows, (f"{len(stubs)} mouth(s) can be opened to a block face, and a solve needs "
                      "two: one to be fed through and one to leave by")
    # THE TWO THIS SENTENCE IS ABOUT ARE THE TWO THAT ARE PORTS, not the first two mouths
    # measured. They are the same thing when nothing is declared, because the default names mouth
    # 0 the inlet and mouth 1 the outlet. Once a declaration binds names BY POSITION they need not
    # be, and reading rows[0] and rows[1] would then put a wall mouth's name and another mouth's
    # bore into the same sentence.
    ends = [k for k, r in enumerate(rows) if r["type"] in ("inlet", "outlet")][:2]
    if len(ends) < 2:
        ends = [0, 1]
    a, b = stubs[ends[0]], stubs[ends[1]]
    first, second = rows[ends[0]]["name"], rows[ends[1]]["name"]
    said = (f"{first} and {second} are cut out of "
            + (f"{a.face}" if a.face == b.face else f"{a.face} and {b.face}")
            + f", {2000.0 * a.radius:.3f} mm and {2000.0 * b.radius:.3f} mm across")
    if a.face == b.face:
        said += (". Both are on one face, which a renamed block face cannot express and is why "
                 "they are cut rather than renamed")
    if len(stubs) > 2 and roles:
        said += (f". The other {len(stubs) - 2} mouth(s) are cut as walls: which of several is "
                 "fed is not something the geometry says")
    elif len(stubs) > 2:
        said += f". The other {len(stubs) - 2} mouth(s) are cut the same way"
    if named is None and not roles:
        said += (". No inlet and no outlet is named: none was declared at intake, and which end "
                 "is fed is not something the geometry says. Every mouth is delivered as an "
                 "unassigned opening")
        return rows, said
    # ...AND IT IS ONLY A GUESS WHILE NOBODY HAS SAID. This was appended unconditionally, so a
    # run whose ports had just been bound to the customer's own declaration BY POSITION still
    # signed off with "The direction is a guess - coolant_in is the mouth measured first - so
    # name them at intake to fix it": wrong about the direction, and naming a patch that is not
    # in the mesh, directly after the line that had named them inlet and outlet_1. It also asked
    # the customer to do at intake the thing they had already done. Seen on the first real
    # customer cold plate, in the same log line as the fix that named its ports.
    if named is None:
        said += (f". The direction is a guess - {inlet} is the mouth measured first - so name "
                 "them at intake to fix it")
    return rows, said
