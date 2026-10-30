# Responsibility: Build a two-region conjugate mesh with cfMesh, which cannot make one directly.
# Boundaries: authors the dictionaries and reads the result; it judges nothing the criteria own.
"""A conformal solid/fluid mesh out of a mesher that only does manifold domains.

THE PROBLEM. Conjugate heat transfer needs two regions - the part and the coolant - sharing an
interface where every face on one side has exactly one partner on the other. cfMesh cannot produce
that: it meshes manifold domains only and discards zone information. snappyHexMesh can, through
`locationsInMesh`, but carries a hard cell ceiling that a chip-scale job walks straight into.

THE APPROACH. Never ask cfMesh for regions. Give it ONE manifold domain - the outer block of the
whole part - and let it build a single octree across metal and coolant alike. Then cut the finished
mesh in two: tag every cell whose centre lies inside the fluid surface, name the rest, and let
`splitMeshRegions` separate them along faces that already exist.

The interface is conformal BY CONSTRUCTION, and that is the whole point. Both regions are made of
the same cells from the same octree, so the surface between them is a set of internal faces that
were always there. Nothing is reconstructed, nothing is matched up afterwards, and there is no
tolerance to tune. The approach that fails is meshing the two regions separately and stitching two
independently generated surfaces together.

AND THE COOLANT NEEDS A WAY IN AND OUT. A cold plate cools by moving coolant through itself, so a
sealed blob of liquid is not a cooling answer, it is a thermal battery. It is also unsolvable: with
no boundary anywhere fixing pressure the pressure matrix is singular, and the first conjugate solve
run here died inside the preconditioner for exactly that reason while the energy equation solved
happily. So the block is handed to cfMesh as SIX NAMED FACES rather than one anonymous box, and the
faces the coolant reaches arrive in the fluid mesh already named and ready to be an inlet.

MEASURED, 2026-10-21, on three fluid topologies - a connected branching tree, 100 channels that
never touch each other, and 200 channels separated by 15 um fins. Interface face counts came out
equal on both sides every time, and the fluid region's volume landed within 0.12%, 1.04% and 3.21%
of the CAD it was built from. A conjugate solve then ran on the result.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import resource
except ImportError:                                      # pragma: no cover - absent on Windows
    resource = None  # type: ignore[assignment]

from meshpipeline.engines.cfmesh_conjugate.foam_exec import (
    _DEFAULT_BASHRC,
    _foam_env,
    check_mesh,
    scan_case_dicts,
)
from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches
from meshpipeline.sandbox.safe_exec import run_guarded

logger = logging.getLogger(__name__)

#: Names the two regions carry through split, checkMesh and the solver case.
FLUID_REGION = "fluid"
SOLID_REGION = "solid"

#: How the interface patches are named once `splitMeshRegions` has run.
FLUID_TO_SOLID = f"{FLUID_REGION}_to_{SOLID_REGION}"
SOLID_TO_FLUID = f"{SOLID_REGION}_to_{FLUID_REGION}"

#: Meshing the whole block instead of only the coolant multiplies the cell count by
#: block volume / fluid volume. On a branching microchannel part that ratio is about 22, so the
#: intent is that the background stays coarse and octree levels are spent only where the coolant
#: surface is, because a silicon block does not need 10 um cells in the middle of it.
#:
#: THAT INTENT IS ONLY MET WHERE THE CHANNELS ARE FAR APART, and on these parts they are not.
#: `refinementThickness` defaults to one background cell, so the fine band reaches a background
#: cell into the metal from every coolant surface; a channel array at 200 um pitch with a 400 um
#: background has no metal outside the band at all. Measured on the 100-channel part at 2 cells
#: across: the solid region came out at a 50 um mean cell against a 400 um background, so the
#: conjugate mesh was effectively uniform at the interface size. The cell estimate in semi.py
#: models this directly rather than assuming the metal is coarse, which is what it assumed before
#: and why it came in 2.4 times short.
DEFAULT_REFINEMENT_LEVELS = 3

#: Written as a named constant because an inline escape here has been mangled twice by
#: tooling that rewrites backslashes; a name cannot be re-escaped by accident.
_NEWLINE = chr(10)

#: Each extra level halves the cell, so the interface cell is background / 2**levels.
_LEVEL_DIVISOR = 2


class ConjugateError(RuntimeError):
    """A conjugate build that cannot be trusted. Raised instead of returning a bad mesh.

    CARRIES THE STAGE'S OWN EXIT CODE when one of the five OpenFOAM runs is what failed, because
    the shared native-result seam reads a NEGATIVE return code as "killed by a signal" and names
    the signal from it. A mesher the kernel kills for memory - measured peak 1.67 GB on a small
    plate, so not hypothetical - has to arrive as signalled and not as a plain refusal, or nothing
    downstream can tell an out-of-memory run from a mesh this engine declined to trust.
    """

    def __init__(self, message: str, *, rc: int | None = None, stage: str = "") -> None:
        super().__init__(message)
        self.rc = rc
        self.stage = stage


# #
# the outer block, and its six named faces
# #
#: The six outer faces of the block, each a separate named patch in the surface handed to cfMesh.
#:
#: NAMING THEM AT THE SURFACE is what makes a port possible at all, and it costs nothing. cfMesh
#: takes its patch names from the `solid` names of an ASCII STL, and `splitMeshRegions` carries
#: those patches into both regions, keeping on each side only the faces that region actually owns.
#: So a face the coolant reaches arrives in the fluid mesh already named, with a face count that
#: proves the coolant really is there, and can be given an inlet condition by name.
#:
#: One anonymous box gives one patch covering the lot, and then the faces the coolant reaches and
#: the faces the metal reaches are the same patch. There is nothing to make an inlet out of, and
#: nothing short of re-cutting patches after the fact can separate them.
BLOCK_FACES: tuple[str, ...] = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")

#: The one patch a SHELL domain has: the part's own outside. When the domain handed to cfMesh is
#: the part rather than the cuboid round it (cad/fluid_volume.write_domain) there are no six faces
#: to name - there is one surface, and the ports are cut out of it exactly as they are cut out of
#: a block face today. Measured on the first customer cold plate: the cuboid is 846,543 mm3 and
#: metal plus coolant is 274,807 mm3, which is 32.5% of it BY VOLUME.
#: AND THE VOLUME SHARE IS NOT THE CELL SHARE, which is the mistake this comment used to make.
#: Measured 2026-10-30 on that same customer plate, the same plan either way and only the block
#: changed (ws_par182 against ws_fitted on the bench, 8 ranks, background cell 0.3521 mm, levels 1,
#: 4 extra levels on the 98 um microchannels):
#:
#:     cuboid block, 12 triangles         336,223,062 cells
#:     the part's own skin, 27,696         269,650,437 cells
#:     so the air cost                     66,572,625 cells, 19.8%
#:
#: NOT 67.5%. The cuboid holds 2.71x the volume, and reasoning from that to cells is wrong by more
#: than three times: cells gather on surfaces, and the air the cuboid adds sits at the background
#: size where cells are cheapest. 19.8% is still worth taking, and meshing only the part is worth
#: more than the cells, but the number to quote is 19.8%.
OUTER_PATCH = "wall_outer"


def use_shell_domain(surface: dict | None) -> bool:
    """Whether to fill the part's own skin instead of the cuboid round it.

    The record `cad/fluid_volume.write_domain` leaves on the coolant surface has already measured
    both: `share_of_box` is how much of its bounding box the domain actually is, and
    BOX_WORTH_KEEPING is where the cuboid stops being worth its air. A conjugate mesher fills ONE
    manifold volume and cuts the regions out of it afterwards, so every cell between the part and
    that cuboid is paid for at the background size.

    MEASURED ON THE FIRST CUSTOMER COLD PLATE: the domain is 276,372 mm3 of a 750,015 mm3 box,
    share_of_box 0.3685, which is 2.71x less volume to fill. IN CELLS IT IS WORTH FAR LESS THAN
    THAT - 336,223,062 against 269,650,437 on the bench with only the block changed, so 19.8% -
    and the comment beside OUTER_PATCH has the run. This docstring used to say the volume ratio
    and the cell ratio were the same thing. They are not, and the difference is a factor of three.

    FALSE FOR EVERYTHING ELSE, deliberately. No domain, a domain that did not come out, or a part
    that fills its own box: the box stands exactly as it always has, which is the simpler surface
    and costs almost nothing on a part that is nearly a cuboid.
    """
    from meshpipeline.cad.fluid_volume import BOX_WORTH_KEEPING

    dom = (surface or {}).get("domain") if isinstance(surface, dict) else None
    if not isinstance(dom, dict) or not dom.get("ok"):
        return False
    share = dom.get("share_of_box")
    # A NUMBER, not something that parses as one. `float("0.3")` is 0.3 and a string here means
    # the record went through something that stringified it, which is not a measurement this may
    # act on - the box stands and the run is the one it always was.
    if not isinstance(share, (int, float)) or isinstance(share, bool):
        return False
    return 0.0 < float(share) < BOX_WORTH_KEEPING


def is_domain_face(face: str) -> bool:
    """Whether `face` names a surface a port may be cut out of: a block face, or the shell."""
    return face in BLOCK_FACES or face == OUTER_PATCH

#: Which axis a face is normal to, and which end of that axis it sits at.
_FACE_AXIS: dict[str, tuple[int, int]] = {
    "xmin": (0, -1), "xmax": (0, 1),
    "ymin": (1, -1), "ymax": (1, 1),
    "zmin": (2, -1), "zmax": (2, 1),
}

#: The opposite face, for picking a port pair the flow can cross the part between.
_OPPOSITE: dict[str, str] = {"xmin": "xmax", "xmax": "xmin", "ymin": "ymax",
                             "ymax": "ymin", "zmin": "zmax", "zmax": "zmin"}

#: Corner indices of the block, and the two outward-wound triangles of each face.
_CORNERS = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
            (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))
_FACE_TRIS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "zmin": ((0, 2, 1), (0, 3, 2)),
    "zmax": ((4, 5, 6), (4, 6, 7)),
    "ymin": ((0, 1, 5), (0, 5, 4)),
    "ymax": ((2, 3, 7), (2, 7, 6)),
    "xmin": ((0, 4, 7), (0, 7, 3)),
    "xmax": ((1, 2, 6), (1, 6, 5)),
}

#: How close to a block face the coolant surface has to come before that face counts as open, as a
#: fraction of the block's diagonal. The STL carrying the coolant is written in float32, so an
#: exactly flush face lands a few parts in 10^7 off the plane and an absolute nanometre tolerance
#: would call a genuinely open face sealed. 1e-5 of a 38 mm diagonal is 0.4 um, two orders below
#: the thinnest wall in any part here (15 um).
OPEN_FACE_RTOL = 1e-5


def block_bounds(lo: tuple[float, float, float], hi: tuple[float, float, float],
                 grow: dict[str, float] | None = None) -> tuple[tuple, tuple]:
    """The part's own outer extent, with named faces optionally pushed outward.

    GROWING A FACE ADDS SOLID, never coolant, because the coolant is whatever lies inside the fluid
    surface and that surface does not move. So this is how a lid gets built: a part whose channels
    are open at the top has its coolant flush with the block's own zmax, and growing zmax by the lid
    thickness puts metal over them. The channel roof stops being an outer boundary and becomes part
    of the interface, where heat can cross it.

    Sealing that face as a wall is the other honest answer and needs no growth at all - it is a lid
    of zero thickness that conducts nothing sideways. Which one is right is the operator's call, so
    neither is taken here by default.
    """
    out_lo = [float(x) for x in lo]
    out_hi = [float(x) for x in hi]
    for face, dist in (grow or {}).items():
        if face not in _FACE_AXIS:
            raise ConjugateError(f"{face!r} is not a block face; expected one of {BLOCK_FACES}")
        if dist < 0:
            raise ConjugateError(
                f"cannot grow {face} by {dist}: a face only moves outward, and moving it inward "
                "would cut into the part")
        axis, sign = _FACE_AXIS[face]
        if sign < 0:
            out_lo[axis] -= dist
        else:
            out_hi[axis] += dist
    if any(h <= l for l, h in zip(out_lo, out_hi)):
        raise ConjugateError(f"degenerate block bounds: {tuple(out_lo)} -> {tuple(out_hi)}")
    return (out_lo[0], out_lo[1], out_lo[2]), (out_hi[0], out_hi[1], out_hi[2])


#: How many background cells have to fit across the block's thinnest direction before the
#: background cell is a size the mesh could actually use.
#:
#: Below this it is not. cfMesh cannot place a 400 um cell inside a 200 um wide block, so the levels
#: asked for are not the levels delivered and the sizing request is partly notional. That is worth
#: saying out loud before a run, because afterwards it looks like nothing at all.
#:
#: WHAT IT DOES NOT PREDICT is the volume error, and the measurements say so plainly. At 2 cells
#: across this fires on unit_cell_100um, pin_fin_300um_staggered and lee_garimella_2005_test1, whose
#: errors were -27.5%, -15.3% and -1.75%: the worst two and the third best. A guess that it would
#: separate good meshes from bad ones was wrong, and it is kept because the condition it reports is
#: real on its own terms.
MIN_BACKGROUND_CELLS_ACROSS_BLOCK = 2.0


def background_complaint(lo, hi, background_m: float) -> str:
    """Empty when the background cell is a size this block could use, otherwise what is wrong.

    See MIN_BACKGROUND_CELLS_ACROSS_BLOCK for what this does and does not tell you.
    """
    if background_m <= 0:
        return ""
    thinnest = min(hi[k] - lo[k] for k in range(3))
    across = thinnest / background_m
    if across >= MIN_BACKGROUND_CELLS_ACROSS_BLOCK:
        return ""
    return (
        f"the background cell is {background_m * 1e6:.0f} um and the block is only "
        f"{thinnest * 1e6:.0f} um across its thinnest direction, which is {across:.2f} background "
        f"cells. Under {MIN_BACKGROUND_CELLS_ACROSS_BLOCK:g} the coarse background is not a size "
        "this mesh can use, so the octree levels asked for are not the ones delivered. Fewer "
        "levels, or a coarser cells-across, would give the background somewhere to live. It says "
        "nothing about whether the mesh will be accurate.")


#: A part longer than this is not a part, it is a unit. Everything downstream of the staging is in
#: METRES, and nothing this engine meshes - cold plate, coupon, manifold, heat sink - is ten metres
#: across. A millimetre surface read as metres is at least a thousand times its real size, so any
#: part from 10 mm upward lands past this bar.
#:
#: WHY A SIZE AND NOT A CELL COUNT. The cell estimate cannot see a unit error at all: budget.py
#: prices area/cell**2 and volume/background**3, which are dimensionless ratios, so a consistently
#: millimetre job produces the IDENTICAL estimate to a consistently metre one and every gate built
#: on the estimate - CELL_HARD_LIMIT, refuse_for_octree, the archive ceiling - passes it. The only
#: number that reveals the mistake is an absolute length. Measured: the plate whose surfaces were
#: written unscaled estimated 334,340,370 cells, passed every gate that was asked, and built
#: 877,659,515 octree boxes before it was killed at 250 of 251 GiB.
#:
#: The same check exists for the authoring side as devtools/semiconductor/parts.MAX_PART_M, at
#: 0.3 because that package only ever holds chips. This one is the engine's, so it is set where no
#: real part can reach it.
MAX_PART_M = 10.0

#: Round factors a unit mistake actually comes out as, so the complaint can name one instead of
#: leaving a reader to divide. 1e3 is millimetres read as metres (and 1e6 is that twice over, which
#: is what an unscaled OpenCASCADE transfer of a metre-declared file gives); 25.4 and 10 are inches
#: and centimetres read as millimetres.
_UNIT_FACTORS = ((1e6, "millimetres twice over, or a metre-declared file transferred unscaled"),
                 (1e3, "millimetres read as metres"),
                 (25.4, "inches read as millimetres"),
                 (10.0, "centimetres read as millimetres"))


def part_size_complaint(lo, hi) -> str:
    """Empty when the part's extent could be metres, otherwise what is wrong with it.

    See MAX_PART_M for why this is measured in metres rather than in cells.

    IT DOES NOT NAME THE FACTOR, deliberately. One extent cannot tell you which unit was missed:
    a plate left in millimetres reads 170.2, and 170.2 divided by 25.4 is 6.7 m, which is just as
    plausible a part as 0.1702 m. Guessing picked the wrong one. `driver.coolant_sits_inside` has
    TWO extents to compare and so can name the factor exactly; this one reports the size and stops.
    """
    longest = max(float(hi[k]) - float(lo[k]) for k in range(3))
    if longest <= MAX_PART_M:
        return ""
    return (f"the part's longest side is {longest:.6g} m, and nothing this engine meshes is over "
            f"{MAX_PART_M:g} m. Every length below here is metres, so this one was never "
            "converted: scale the geometry on the way in rather than meshing it as it stands - it "
            "does not fail, it runs until someone kills it.")


def block_surface_stl(lo, hi) -> str:
    """The outer block as an ASCII STL of six named solids, one per face.

    cfMesh meshes the volume ENCLOSED by the surface it is handed, so for a conjugate build that
    surface has to be the outside of the whole part - not the coolant, and not the channelled solid.
    That boundary is a box, and this is it.

    ASCII rather than binary, and six solids rather than one, so each face becomes its own named
    patch: binary STL carries no names, and a nameless box cannot have an inlet. The cost is a
    12-triangle text file.

    Every face is wound OUTWARD. A box with one face wound inward still looks like a box, still
    tessellates and still renders, and cfMesh then meshes the complement of what was intended.
    """
    (x0, y0, z0), (x1, y1, z1) = block_bounds(lo, hi)
    span = ((x0, x1), (y0, y1), (z0, z1))
    verts = [tuple(span[k][c[k]] for k in range(3)) for c in _CORNERS]
    out: list[str] = []
    for face in BLOCK_FACES:
        out.append(f"solid {face}")
        for ia, ib, ic in _FACE_TRIS[face]:
            pa, pb, pc = verts[ia], verts[ib], verts[ic]
            ux, uy, uz = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
            wx, wy, wz = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
            nx, ny, nz = (uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx)
            m = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
            out.append(f"  facet normal {nx / m:.6e} {ny / m:.6e} {nz / m:.6e}")
            out.append("    outer loop")
            for p in (pa, pb, pc):
                out.append(f"      vertex {p[0]:.9e} {p[1]:.9e} {p[2]:.9e}")
            out.append("    endloop")
            out.append("  endfacet")
        out.append(f"endsolid {face}")
    return _NEWLINE.join(out) + _NEWLINE


# #
# where the coolant reaches the outside: the ports
# #
def open_faces(fluid_lo, fluid_hi, block_lo, block_hi, *,
               rtol: float = OPEN_FACE_RTOL) -> tuple[str, ...]:
    """Which block faces the coolant surface reaches, from the two bounding boxes.

    This is a CANDIDATE LIST and nothing stronger. A fluid bounding box touching a plane proves
    some coolant geometry lies on that plane; it does not prove any cell centre was tagged there,
    and on a staircased build the last row of cells can fall short. The proof is the face count on
    that patch in the finished fluid region, which `verify` insists on.

    Cheap as it is, it is what turns "give the coolant an inlet" from CAD work into naming: of the
    nine parts in this package, eight have their channel ends already flush with a block face and
    only one is sealed inside the metal.
    """
    diag = sum((block_hi[k] - block_lo[k]) ** 2 for k in range(3)) ** 0.5
    tol = rtol * diag
    found = []
    for face in BLOCK_FACES:
        axis, sign = _FACE_AXIS[face]
        gap = (fluid_lo[axis] - block_lo[axis]) if sign < 0 else (block_hi[axis] - fluid_hi[axis])
        if gap <= tol:
            found.append(face)
    return tuple(found)


@dataclass(frozen=True)
class PortPlan:
    """Which named block faces the coolant enters and leaves by, and what the rest of them are."""

    inlet: str
    outlet: str
    #: Faces the coolant also reaches but which are not ports, so they are sealed. This is the
    #: normal case and not a defect: a block cut out of a wider channel array has coolant flush
    #: with the sides it was cut on, and those sides are walls.
    walls: tuple[str, ...] = ()
    reason: str = ""

    @property
    def patches(self) -> tuple[str, str]:
        return (self.inlet, self.outlet)


def plan_ports(open_: tuple[str, ...], extent, *,
               inlet: str | None = None, outlet: str | None = None) -> PortPlan:
    """Choose the inlet and outlet from the faces the coolant reaches.

    THE DEFAULT RULE IS THE LONG WAY DOWN ONE PASSAGE. Among the opposite pairs of faces the coolant
    reaches, take the pair on the axis `extent` is longest along. Coolant runs the length of a
    channel, not across its width.

    `extent` has to be ONE PASSAGE's spans and not the whole coolant's bounding box, which is the
    distinction that was got wrong first time round. Tuckerman and Pease's part is 200 channels,
    each 14 mm long, spread across 20 mm of silicon: the fluid's bounding box is widest ACROSS the
    channels, so ranking axes by it sent the flow sideways through two hundred dead ends. Per
    passage it picks the channel. See parts.dominant_passage_extent.

    It is a default and not a law, which is why `--inlet`/`--outlet` override it. A part whose
    coolant reaches no opposite pair cannot be flowed through as built, and saying so here is better
    than finding out when the pressure matrix turns out to be singular.
    """
    if (inlet is None) != (outlet is None):
        raise ConjugateError("name both the inlet and the outlet, or neither")
    if inlet is not None and outlet is not None:
        for role, face in (("inlet", inlet), ("outlet", outlet)):
            if face not in BLOCK_FACES:
                raise ConjugateError(f"{role} {face!r} is not a block face; expected {BLOCK_FACES}")
            if face not in open_:
                raise ConjugateError(
                    f"{role} {face!r} was asked for, but the coolant does not reach it. The faces "
                    f"the coolant reaches are {open_ or '(none)'}.")
        if inlet == outlet:
            raise ConjugateError(f"inlet and outlet are both {inlet!r}")
        return PortPlan(inlet=inlet, outlet=outlet,
                        walls=tuple(f for f in open_ if f not in (inlet, outlet)),
                        reason="named on the command line")

    pairs = [f for f in open_ if _OPPOSITE[f] in open_ and _FACE_AXIS[f][1] < 0]
    if pairs:
        best = max(pairs, key=lambda f: extent[_FACE_AXIS[f][0]])
        axis = _FACE_AXIS[best][0]
        other = _OPPOSITE[best]
        return PortPlan(
            inlet=best, outlet=other,
            walls=tuple(f for f in open_ if f not in (best, other)),
            reason=f"one passage runs {extent[axis] * 1e3:.3f} mm along {'xyz'[axis]}, the "
                   f"furthest of the axes whose faces the coolant reaches at both ends")

    # No opposite pair. Two faces that are not opposite is a real and ordinary design - a cold
    # plate fed through its side and drained through its lid has exactly that - and with only two
    # candidates there is nothing to choose, so they are both used. WHICH WAY ROUND CANNOT BE
    # INFERRED: nothing in the geometry says which end is cold. The order is the one BLOCK_FACES
    # happens to list, said out loud, and --inlet/--outlet swaps it.
    if len(open_) == 2:
        first, second = sorted(open_, key=BLOCK_FACES.index)
        return PortPlan(
            inlet=first, outlet=second,
            reason=f"the only two faces the coolant reaches, and they are not opposite, so the "
                   f"direction is a guess: {first} is being used as the inlet because it comes "
                   f"first in {BLOCK_FACES}. Pass --inlet {second} --outlet {first} to reverse it")

    reached = ", ".join(open_) if open_ else "no face at all: it is sealed inside the metal"
    raise ConjugateError(
        "the coolant reaches no opposite pair of block faces, so there is no obvious way for it "
        f"to flow from one side of the part to the other. It reaches {reached}. Name a pair "
        "yourself with --inlet/--outlet, or cut ports into the coolant geometry, or grow the "
        "block so the faces it does reach stop being outer boundaries.")


# #
# the dictionaries
# #
#: The OpenFOAM patch type each declared role becomes, for renameBoundary. A port is a `patch`;
#: everything else on the block is wall. The interface patches are not here: splitMeshRegions
#: creates them itself as a coupled pair and names them (FLUID_TO_SOLID, SOLID_TO_FLUID).
ROLE_TO_OF_TYPE = {"inlet": "patch", "outlet": "patch", "wall": "wall",
                   "external": "wall", "symmetry": "symmetryPlane", "empty": "empty"}


#: How far either side of a mouth's centre the port box reaches, as a multiple of its own
#: diameter. Generous on purpose: the box only has to contain the faces the mouth actually
#: crosses, and those are staircased, so a box that hugs the nominal circle clips the corners off
#: and the port comes back with fewer faces than the bore has. Everything outside the mouth is
#: still excluded, because the box is intersected with ONE block face rather than the whole mesh.
PORT_BOX_DIAMETERS = 0.75

#: The file the port cutter's own topoSet actions are written to. Separate from `topoSetDict`
#: because that one belongs to the region split and runs later with different contents; one file
#: holding both would mean the split's actions ran while the ports were being cut.
PORT_TOPOSET = "topoSetDict.ports"


def render_port_patch_dicts(ports: list) -> tuple[str, str]:
    """(topoSetDict, createPatchDict) that cut one named patch per mouth out of a block face.

    WHY A PATCH AND NOT A RENAMED FACE. `renameBoundary` gives a whole block face the user's name,
    which works while one port lands on each face and fails the moment two do. The first customer
    assembly is exactly that: its inlet and outlet are both fittings pointing +x, so after their
    bores are stubbed out to the boundary BOTH land on `xmax`, and a face can only carry one name.
    `plan_ports` then refuses the part for reaching no opposite pair - a true statement about the
    block's faces and nothing about the plumbing, which is perfectly ordinary. A cold plate fed
    and drained through its lid has the same shape.

    So the port is cut out of the face instead: the faces of that block face lying inside a box
    around the mouth become a patch of their own, and what is left of the face stays a wall.

    CUT BEFORE THE SPLIT, on the block mesh, which is where `renameBoundary` acts too and for the
    same reason: the coolant's end and the metal's outer face at that end are two faces of one
    plane, and both should carry the name the user gave that end of the part. splitMeshRegions
    then hands each region the part of the patch it owns.

    `ports` rows carry: name, type (inlet/outlet/...), face (which block face), centre_m, and
    diameter_m. An empty list gives two empty dictionaries and no stage runs.
    """
    if not ports:
        return "", ""
    acts: list[str] = []
    entries: list[str] = []
    seen: set[str] = set()
    for p in ports:
        name = str(p.get("name") or "").strip()
        face = str(p.get("face") or "").strip()
        if not name or not is_domain_face(face):
            raise ConjugateError(
                f"a port patch needs a name and one of the block faces {BLOCK_FACES} "
                f"or {OUTER_PATCH!r}, "
                f"got name={name!r} face={face!r}")
        if name in seen:
            raise ConjugateError(f"two ports are both called {name!r}")
        seen.add(name)
        centre = [float(v) for v in (p.get("centre_m") or ())]
        diameter = float(p.get("diameter_m") or 0.0)
        if len(centre) != 3:
            raise ConjugateError(f"port {name!r} has no centre in three axes")
        if diameter <= 0.0:
            raise ConjugateError(f"port {name!r} has diameter {diameter}, which is not a mouth")
        r = PORT_BOX_DIAMETERS * diameter
        lo = " ".join(f"{centre[k] - r:.9g}" for k in range(3))
        hi = " ".join(f"{centre[k] + r:.9g}" for k in range(3))
        sel = f"port_{name}"
        acts += [f"    {{ name {sel}; type faceSet; action new; source patchToFace; "
                 f"patch {face}; }}",
                 f"    {{ name {sel}; type faceSet; action subset; source boxToFace; "
                 f"box ({lo}) ({hi}); }}"]
        of_type = ROLE_TO_OF_TYPE.get(str(p.get("type") or "").strip().lower(), "patch")
        entries.append(f"    {{ name {name}; patchInfo {{ type {of_type}; }} "
                       f"constructFrom set; set {sel}; }}")

    head = "FoamFile {{ version 2.0; format ascii; class dictionary; object {obj}; }}\n\n"
    topo = head.format(obj="topoSetDict") + "actions\n(\n" + "\n".join(acts) + "\n);\n"
    # EVERY ENTRY IS SET-BASED, and that is not incidental. snappy_multiregion records what
    # happens otherwise: "a whole-patch move in the same dict as the set moves overrides them
    # (the ports came back empty and every exterior face became wall)". What is left of the block
    # face keeps its own name by doing nothing to it.
    patch = (head.format(obj="createPatchDict") + "pointSync false;\n\npatches\n(\n"
             + "\n".join(entries) + "\n);\n")
    return topo, patch


def render_meshdict(*, block_file: str, fluid_file: str, background_cell_m: float,
                    levels: int = DEFAULT_REFINEMENT_LEVELS,
                    thickness_m: float | None = None,
                    patch_names: dict[str, tuple[str, str]] | None = None,
                    fine_zones: list | None = None,
                    fine_surfaces: list | None = None,
                    stretch_zones: list | None = None) -> str:
    """The meshDict for the single-domain pass over the whole block.

    `surfaceMeshRefinement` is what makes this work. It refines around a surface that is NOT the
    meshed boundary, which is exactly what is needed here: the coolant surface never becomes a wall,
    it only tells the octree where to put small cells. Without it the interface would be staircased
    at the background size, which on a microchannel part is wider than the channels themselves.

    `patch_names` MAPS A BLOCK FACE TO WHAT THE USER CALLED IT: {"ymin": ("coolant_in", "inlet")}.
    Without it the delivered mesh carries the six block-face names and nothing else, so a customer
    who declared an opening called coolant_in gets a mesh with no patch of that name and the
    patch-contract gate refuses a mesh that is geometrically perfect. cfMesh renames before the
    split, so a renamed face is inherited by BOTH regions - which is correct for a port, since the
    coolant's end and the metal's outer face at that end are two different faces of the same
    plane and both should carry the name the user gave that end of the part.

    `refinementThickness` is how far from that surface the extra levels reach, and the default is
    one background cell, which is 2**levels interface cells deep. That is enough for the octree to
    grade back out, and on a channel narrower than twice the background it covers the coolant
    outright. It is not a sizing rule and does not try to be one: engines/gap_sizing owns how big
    the cells are, and this says only how far the small ones extend.
    """
    if background_cell_m <= 0:
        raise ConjugateError(f"background cell must be positive, got {background_cell_m}")
    if levels < 0:
        raise ConjugateError(f"refinement levels cannot be negative, got {levels}")
    thickness = background_cell_m if thickness_m is None else float(thickness_m)
    if thickness <= 0:
        raise ConjugateError(f"refinement thickness must be positive, got {thickness}")
    # A SECOND SURFACE, FINER, OVER THE PART OF THE COOLANT THAT IS THINNER. This is the same
    # mechanism as `coolant` above and not a new one: a surface that is not the meshed boundary,
    # telling the octree where to put small cells. The surface is a SUBSET of the coolant's own
    # triangles - the ones whose walls are thin, written out separately by cad/scales - so the
    # extra levels reach only where the thin channels are.
    #
    # NOT A BOX, AND THE DIFFERENCE IS THE WHOLE RUN. `objectRefinements` below takes a box and
    # fills its VOLUME at the given size. On this part the thin layer is 35.9% of the wetted area
    # and about 2% of the volume, so its bounding box is almost all metal: a box zone priced out
    # at 456,206,329 cells against 224,431,537 for a band round the same surface. Measured
    # 2026-10-28 - the box run produced 478,648,313 octree boxes and cartesianMesh aborted,
    # because 8 octree vertices per box is 3,829,186,504 and this OpenFOAM is built with 32-bit
    # labels (linux64GccDPInt32Opt, WM_LABEL_SIZE=32). It had used 50 GB of 251 at the time; the
    # ceiling is the integer, not the memory. See budget.OCTREE_BOX_CEILING.
    rows = ["    coolant\n    {\n"
            f'        surfaceFile "{fluid_file}";\n'
            f"        additionalRefinementLevels {levels};\n"
            f"        refinementThickness {thickness:.6g};\n"
            "    }\n"]
    for k, fine in enumerate(fine_surfaces or []):
        name = str(fine.get("name") or f"fine{k}").strip() or f"fine{k}"
        f_file = str(fine.get("file") or "").strip()
        f_levels = int(fine.get("levels", 0))
        if not f_file:
            raise ConjugateError(f"the fine surface {name!r} has no surface file")
        if f_levels <= levels:
            # a fine surface no finer than the coolant itself buys nothing and would read as a
            # deliberate choice to someone looking at the dictionary later
            raise ConjugateError(
                f"the fine surface {name!r} asks for {f_levels} levels against the coolant's "
                f"{levels}; a finer surface has to be finer")
        f_thick = fine.get("thickness_m")
        f_thick = (background_cell_m / (2 ** f_levels) * 4.0 if f_thick is None
                   else float(f_thick))
        if f_thick <= 0:
            raise ConjugateError(f"the fine surface {name!r} has thickness {f_thick}")
        rows.append(f"    {name}\n    {{\n"
                    f'        surfaceFile "{f_file}";\n'
                    f"        additionalRefinementLevels {f_levels};\n"
                    f"        refinementThickness {f_thick:.6g};\n"
                    "    }\n")
    out = (
        "FoamFile { version 2.0; format ascii; class dictionary; object meshDict; }\n\n"
        f'surfaceFile "{block_file}";\n'
        f"maxCellSize {background_cell_m:.6g};\n\n"
        "surfaceMeshRefinement\n{\n" + "".join(rows) + "}\n")
    # CELLS SPENT WHERE THE SMALL THINGS ARE, not spread evenly over a surface that is not evenly
    # sized. `surfaceMeshRefinement` above puts ONE cell size on the whole coolant, which is the
    # right answer only while the coolant has one scale. The first real customer part has two: a
    # manifold of ~3.5 mm channels and a stack of ~15 slots about 98 um across. A single size that
    # suits the slots is unaffordable over the manifold, and one that suits the manifold cannot see
    # the slots at all - at 0.0947 mm cells the manifold got 28 cells across it and the slots got
    # one. The mean hides it completely: 4V/A over that whole coolant is 3.5 mm.
    #
    # Each zone is a box in METRES with its own cell size, which cfMesh's objectRefinements takes
    # directly. The boxes come from the geometry (where the coolant is thin), not from a person,
    # and `cellSize` is absolute rather than a level count so a zone cannot be quietly rounded to
    # a power of two away from what it was asked for.
    for zone in (fine_zones or []):
        lo, hi, cell = zone["lo"], zone["hi"], float(zone["cell_m"])
        if cell <= 0:
            raise ConjugateError(f"a refinement zone needs a positive cell size, got {cell}")
        if any(float(hi[k]) <= float(lo[k]) for k in range(3)):
            raise ConjugateError(f"a refinement zone needs a positive extent, got {lo} .. {hi}")
        centre = [0.5 * (float(lo[k]) + float(hi[k])) for k in range(3)]
        span = [float(hi[k]) - float(lo[k]) for k in range(3)]
        name = str(zone.get("name") or f"fine{len(out)}").strip() or "fine"
        out += ("\nobjectRefinements\n{\n" if "objectRefinements" not in out else "")
        out += (f"    {name}\n    {{\n"
                "        type box;\n"
                f"        cellSize {cell:.6g};\n"
                f"        centre ({centre[0]:.9g} {centre[1]:.9g} {centre[2]:.9g});\n"
                f"        lengthX {span[0]:.9g};\n"
                f"        lengthY {span[1]:.9g};\n"
                f"        lengthZ {span[2]:.9g};\n"
                "    }\n")
    if fine_zones:
        out += "}\n"

    # CELLS SHAPED LIKE THE FEATURE THEY RESOLVE. An octree cell is a cube, and a microchannel is
    # not: the first customer's slots are 0.098 mm wide, run tens of mm in plane, and are 1.9 mm
    # tall with a constant cross-section through that height. Resolving the WIDTH to 5 cells with
    # cubes therefore buys about 97 cells through a height where a dozen would do, and the whole
    # field is priced at the cost of the axis that needed nothing.
    #
    # `anisotropicSources` is cfMesh's own answer and it ships in its ship5415Octree tutorial.
    # MEASURED HERE, because the sign convention decides the sizing and guessing it the wrong way
    # quadruples the mesh instead of quartering it. A 10 mm box at 1 mm cells, 2026-10-29:
    #
    #     plain          896 cells, max cell volume 1.256e-9
    #     scaleY 4.0     320 cells, max cell volume 5.142e-9   (4.09x taller, 2.8x fewer cells)
    #     scaleY 0.25  3,776 cells, max cell volume 4.344e-10  (4x shorter, 4.2x more cells)
    #
    # So scale > 1 stretches the cell along that axis and the saving follows the factor. The box
    # is 10 cells tall, which is why the count saving is 2.8x rather than 4x here: the surface
    # layer cannot stretch. Over a field many cells deep it approaches the factor.
    #
    # A STRETCH IS NOT A REFINEMENT and must not be read as one. It changes the SHAPE of the
    # cells in its box, nothing about where the small ones go. The axis to stretch is the one the
    # geometry is prismatic along; stretching an axis that carries features smears them.
    for z in (stretch_zones or []):
        lo, hi = z["lo"], z["hi"]
        sx, sy, sz = (float(s) for s in z["scale"])
        if any(float(hi[k]) <= float(lo[k]) for k in range(3)):
            raise ConjugateError(f"a stretch zone needs a positive extent, got {lo} .. {hi}")
        if min(sx, sy, sz) <= 0.0:
            raise ConjugateError(f"a stretch factor must be positive, got ({sx} {sy} {sz})")
        if (sx, sy, sz) == (1.0, 1.0, 1.0):
            raise ConjugateError("a stretch zone that scales nothing is a dictionary entry that "
                                 "reads as a deliberate choice and does nothing")
        centre = [0.5 * (float(lo[k]) + float(hi[k])) for k in range(3)]
        span = [float(hi[k]) - float(lo[k]) for k in range(3)]
        name = str(z.get("name") or "stretch").strip() or "stretch"
        out += ("\nanisotropicSources\n{\n" if "anisotropicSources" not in out else "")
        out += (f"    {name}\n    {{\n"
                "        type box;\n"
                f"        centre ({centre[0]:.9g} {centre[1]:.9g} {centre[2]:.9g});\n"
                f"        lengthX {span[0]:.9g};\n"
                f"        lengthY {span[1]:.9g};\n"
                f"        lengthZ {span[2]:.9g};\n"
                f"        scaleX {sx:.6g};\n"
                f"        scaleY {sy:.6g};\n"
                f"        scaleZ {sz:.6g};\n"
                "    }\n")
    if stretch_zones:
        out += "}\n"

    if patch_names:
        # NO defaultName and NO defaultType. Every block face is already a named solid of the
        # block STL, so there is nothing for a default to catch - and on the 2D cfMesh path a
        # default absorbed 34k faces that identity entries had failed to match
        # (cfmesh_runner.py:663-668). A face not listed here keeps its own name, which is what
        # should happen to the five faces that are not the port the user named.
        rows = []
        for face in BLOCK_FACES:
            if face not in patch_names:
                continue
            name, role = patch_names[face]
            of_type = ROLE_TO_OF_TYPE.get(str(role).strip().lower(), "patch")
            rows.append(f"        {face} {{ newName {name}; type {of_type}; }}")
        if rows:
            out += ("\nrenameBoundary\n{\n    newPatchNames\n    {\n"
                    + "\n".join(rows) + "\n    }\n}\n")
    return out


def interface_cell_m(background_cell_m: float, levels: int = DEFAULT_REFINEMENT_LEVELS) -> float:
    """The cell size actually achieved at the coolant surface."""
    return background_cell_m / (_LEVEL_DIVISOR ** levels)


def render_toposetdict(*, fluid_file: str) -> str:
    """Tag the coolant cells, then name everything else.

    `useSurfaceOrientation` lets topoSet decide inside from outside using the surface's own normals,
    so no interior seed point has to be supplied. On a branching network there is no obvious point
    to pick, and that guess is exactly where a seed-based selection goes wrong.

    BOTH ZONES ARE NAMED, and leaving the second one implicit is the mistake this avoids. An
    unzoned remainder leaves `splitMeshRegions` to work out what those cells are, and what it works
    out is one region per connected lump of metal.
    """
    return (
        "FoamFile { version 2.0; format ascii; class dictionary; object topoSetDict; }\n"
        "actions\n(\n"
        "    {\n"
        "        name      fluidCells;\n"
        "        type      cellSet;\n"
        "        action    new;\n"
        "        source    surfaceToCell;\n"
        f'        file      "{fluid_file}";\n'
        "        useSurfaceOrientation true;\n"
        "        outsidePoints ((-1e6 -1e6 -1e6));\n"
        "        includeCut      false;\n"
        "        includeInside   true;\n"
        "        includeOutside  false;\n"
        "        nearDistance    -1;\n"
        "        curvature       -100;\n"
        "    }\n"
        f"    {{\n        name      {FLUID_REGION};\n        type      cellZoneSet;\n"
        "        action    new;\n        source    setToCellZone;\n"
        "        set       fluidCells;\n    }\n"
        "    {\n"
        "        name      solidCells;\n"
        "        type      cellSet;\n"
        "        action    new;\n"
        "        source    boxToCell;\n"
        "        box       (-1e6 -1e6 -1e6) (1e6 1e6 1e6);\n"
        "    }\n"
        "    {\n"
        "        name      solidCells;\n"
        "        type      cellSet;\n"
        "        action    subtract;\n"
        # cellToCell, not setToCell. `setToCell` does not exist, and topoSet rejects the whole
        # dictionary rather than the one action, so the mesh is built and then thrown away.
        "        source    cellToCell;\n"
        "        set       fluidCells;\n"
        "    }\n"
        f"    {{\n        name      {SOLID_REGION};\n        type      cellZoneSet;\n"
        "        action    new;\n        source    setToCellZone;\n"
        "        set       solidCells;\n    }\n"
        ");\n")


#: What `splitMeshRegions` leaves behind in `constant/`, and where it has to be moved to.
#:
#: It is one integer per cell saying which region that cell went to: mesh DATA, and nothing reads
#: it after the split. But it sits in `constant/` and it is not inside a `polyMesh/`, so
#: `sandbox.foam_case_guard` scans it as if it were an authored dictionary, and above its 8 MB cap
#: it refuses the whole case rather than read something it cannot bound. On a 1.4 M cell part that
#: file is 9.9 MB, so every checkMesh after the split came back empty, every region came back with
#: no cells, and a perfectly good mesh - conformal, ported, 305,696 interface faces on each side -
#: was reported as having none.
#:
#: Moved rather than weakened. Exempting the name in the guard would fix every engine at once and
#: would also mean one more filename a model could write a `#codeStream` into and have gone
#: unscanned, and that trade is not this module's to make. WORTH KNOWING: snappy_multiregion and
#: snapgrid both run `splitMeshRegions` and neither handles this file, so a multi-region mesh over
#: roughly 1.2 M cells walks into the same wall on those paths.
CELL_TO_REGION = "cellToRegion"
#: WHICH REGIONS THE CASE HAS AND WHAT EACH ONE IS, which is the file that makes a pile of
#: per-region polyMeshes into a multi-region OpenFOAM case. splitMeshRegions does not write it.
#:
#: It is mesh-side metadata, not physics - it names the regions and says which carries a fluid -
#: so it belongs here rather than with the solver. Without it `chtMultiRegionFoam` cannot open the
#: case at all, and on the platform's side it is the marker the whole deliverable is recognised by
#: and the file four separate gates read to learn what the regions are. The external harness has
#: written its own copy all along (devtools/semiconductor/cht.py), which is why no measured run
#: ever missed it and nothing in the platform could ever have used one.
REGION_PROPERTIES = "regionProperties"

#: `-cellZonesOnly`, never `-cellZones`. The second means "ADDITIONALLY split cellZones off into
#: separate regions": it walks face connectivity FIRST, so every lump of mesh that touches no other
#: lump becomes its own region. That is invisible on a part whose coolant is one connected network
#: inside one connected block, and on an array of 100 parallel channels it produced 84 regions and
#: was still counting an hour later. Disconnected is the NORMAL case for a cooled part: any array of
#: parallel passages is disconnected until a plenum joins it.
SPLIT_ARGS = ("splitMeshRegions", "-cellZonesOnly", "-overwrite")


#: WHY A BIG MESH NEEDS MORE THAN ONE PROCESS, and it is not about how long it takes. cfMesh's
#: octree addressing holds about twelve 32-bit labels per box, so ONE process runs out of label
#: space at (2^31 - 1) / 12 boxes. The run this was measured on asked for 181,673,384 cells - the
#: Corintis plate at 22.00 um with its cells stretched 6x along the channels - which is 2.7M over
#: the ceiling, and it died inside `meshOctreeAddressing::createNodeLabels` with "Negative size
#: requested" after 231 s (2026-10-29). Not out of memory: out of int.
#:
#: The ceiling is PER PROCESS. Each rank octrees its own share of the domain, so N ranks lift it
#: N-fold, and nothing about the geometry or the cell size changes. That is why the way past it is
#: a rank count rather than a coarser mesh.
OCTREE_BOXES_PER_PROCESS = 178_956_970

#: Two flags, both measured rather than chosen.
#:
#: `--allow-run-as-root`: the mesh container runs as root and Open MPI refuses to start at all
#: otherwise.
#:
#: `--oversubscribe`: Open MPI counts the slots it believes it has, not the cores the box has.
#: Inside the container `nproc` reported 8 and mpirun still refused 8 ranks - "Either request
#: fewer slots for your application, or make more slots available for use" - until this was
#: passed. Measured on mesh-bench (8 vCPU, Open MPI 4.1.2), 2026-10-29.
MPI_FLAGS = ("--allow-run-as-root", "--oversubscribe")

#: HOW BIG THE BUFFERED SEND MAY BE, in bytes, and this one is not optional above a few million
#: cells. cfMesh's octree load balancing moves boxes between ranks with `MPI_Bsend`, which sends
#: into a buffer the application attached beforehand - OpenFOAM attaches `mpiBufferSize` from
#: controlDict, 20 MB by default, and `MPI_BUFFER_SIZE` overrides it. A 272M-cell build of the
#: Corintis plate on 8 ranks got four rebalances in and died on the fifth after 40 s:
#:
#:     Distributing load between processors
#:     *** An error occurred in MPI_Bsend
#:     *** MPI_ERR_BUFFER: invalid buffer pointer
#:
#: The same chain at 6.1M cells never came near the default. One gigabyte a rank, because the
#: buffer is resident for the whole run: eight ranks is 8 GiB held against a 251 GB box, which is
#: affordable, and the message that overflowed 20 MB is nowhere near a thousand times that.
MPI_BUFFER_BYTES = 1_000_000_000

#: scotch, because this decomposition only has to be BALANCED. Nothing is solved on it: the mesh
#: is merged again before topoSet, the split or any reader touches it, so the tidier subdomain
#: boundaries a geometric method gives buy nothing here and its hand-set coefficients are one
#: more thing to get wrong on a part nobody has seen.
DECOMPOSE_METHOD = "scotch"

#: cfMesh's own parallel preparation: it reads system/decomposeParDict and lays out one
#: processorN directory per rank for `cartesianMesh -parallel` to mesh into. It is cfMesh's, not
#: decomposePar's - there is no mesh yet to decompose.
PREPARE_PAR = "preparePar"

#: The one line of `system/decomposeParDict` that is not a number, kept out of the f-string
#: because it is the only place in this file where a quote has to survive being written.
DECOMPOSE_HEADER = ('FoamFile { version 2.0; format ascii; class dictionary; '
                    'location "system"; object decomposeParDict; }')

#: ...and the merge back to one mesh in constant/polyMesh. It prints "Not well tested & use at
#: your own risk!" on every run; measured on an 8-rank 6,100,416-cell build of the ported
#: two-scale plate (2026-10-29) the merged mesh came back from checkMesh with "Mesh OK." and
#: 18,420,912 faces.
#: Merge the two REGIONS instead of the whole block, which is what lets topoSet and the split run
#: decomposed. The root block mesh is never reconstructed and nothing downstream wants it: the
#: viewer refuses to read it on purpose (it carries both regions' face counts added together, so a
#: viewer pointed there draws a mesh that was never delivered), and `foamToVTK -allRegions` was
#: measured on a case without it - rc 0, both regions' internal.vtu, 9 boundary files, the same as
#: with it.
#: ONE MERGE PER REGION, NAMED. Not `-allRegions`, and not `-regions` either.
#:
#: `-allRegions` means, in its own help, "Use all regions in regionProperties", and build() writes
#: `constant/regionProperties` at the END, after the merge - so there is no such file when this
#: runs and the option falls back to every mesh it can find, the pre-split block included. It does
#: reach the two regions afterwards, which is why every parallel build delivered correctly; it was
#: slow, not wrong.
#:
#: `-regions '(fluid solid)'` reads like the fix and is not. OpenFOAM accepts the names, ignores
#: them, and says so in one line nobody was reading:
#:
#:     Exec   : reconstructParMesh -constant -regions (fluid solid)
#:     Warning: No regionProperties, assume default region
#:
#: MEASURED THREE WAYS on the same processor directories of a 182,046,234-cell build, mesh-bench,
#: 2026-10-29 - the controlled comparison, because the disk could not afford a second mesh:
#:
#:     -regions '(fluid solid)'   2,039 s   constant/polyMesh 39 GB   and neither region
#:     -region fluid                824 s   constant/fluid    16 GB
#:     -region solid              1,169 s   constant/solid    23 GB
#:
#: 1,993 s for exactly the two meshes the delivery needs, against 2,039 s spent on the block
#: BEFORE the regions were started. In processor0 the block is 5.7 GB against fluid's 2.4 and
#: solid's 2.9, so it was always going to be more than half the stage - and it is the one mesh the
#: viewer refuses to read on purpose, because it carries both regions' face counts added together.
#:
#: ONE CHAIN ENTRY, keeping the stage name: `engines/mesher_stage` maps `reconstructParMesh` to
#: what the customer is shown while it runs ("joining the pieces back together"), and two stages
#: would say it twice. `&&` so a failed first merge stops the second and the stage's exit code is
#: the one that failed.
#:
#: The coupon cannot test any of this: at 59,074 cells the whole stage is seconds either way,
#: which is why the equivalence test that proved this chain CORRECT could not also prove it quick.
RECONSTRUCT_REGIONS = (f"reconstructParMesh -constant -region {FLUID_REGION}"
                       f" && reconstructParMesh -constant -region {SOLID_REGION}")
#: WHAT THE PARALLEL SPLIT LEAVES BEHIND. `splitMeshRegions` writes each region's system/<region>
#: dictionaries beside the mesh it split, which when it runs decomposed is inside processor0 and
#: not at the root - so the reconstructed case has constant/fluid/polyMesh and no
#: system/fluid/fvSchemes, and the next command to open that region exits on the missing file
#: (measured: checkMesh -region fluid, "cannot find file system/fluid/fvSchemes"). `-n` so this
#: cannot clobber the port cut's own topoSetDict, which build writes to the same directory before
#: the chain runs.
REGION_DICTS = "cp -rn processor0/system/. system/ 2>/dev/null; true"


def ranks_for(cells: int, *, available: int = 0) -> int:
    """How many ranks a mesh of `cells` cells needs to stay under the per-process label ceiling.

    1 for anything that fits in one process, which is what every part in the corpus does. The
    count is what the CEILING demands and not what the box can run: a plan that needs more ranks
    than the machine has cores is still a plan that needs them, and saying so is more use than
    silently meshing something smaller. `available` caps it only when a caller passes one.
    """
    n = int(cells)
    if n <= OCTREE_BOXES_PER_PROCESS:
        return 1
    need = -(-n // OCTREE_BOXES_PER_PROCESS)              # ceiling division
    return min(need, int(available)) if available else need


def render_decompose_pardict(ranks: int, *, method: str = DECOMPOSE_METHOD) -> str:
    """`system/decomposeParDict`: how many ways preparePar splits the domain, and by what rule."""
    n = int(ranks)
    if n < 2:
        raise ConjugateError(f"a parallel run needs at least two ranks, got {ranks}")
    if not str(method).isalnum():
        # It is interpolated into a dictionary OpenFOAM reads; the case guard scans for the
        # executable constructs and a method name is not the place to start trusting free text.
        raise ConjugateError(f"not a decomposition method name: {method!r}")
    return _NEWLINE.join((
        DECOMPOSE_HEADER,
        f"numberOfSubdomains {n};",
        f"method          {method};",
        ""))


def omp_threads(ranks: int, *, cores: int = 0) -> int:
    """How many OpenMP threads one rank may take, so the ranks do not fight over the cores.

    MEASURED ON THE RUN THAT MADE THIS NECESSARY. A 279,621,370-cell build of the Corintis plate
    on mesh-bench (8 vCPU), eight ranks, 2026-10-29:

        8 ranks x 10 threads = 84 threads on 8 cores
        every rank pegged at ~100% of ONE core, 180,053 CPU-seconds in 22,536 s = 8.0 cores
        six hours fifteen in the mesher, and it never left it

    cfMesh is built with OpenMP and its default is a thread per core it can SEE. In one process
    that is right and it is the whole reason a single-process build gets 2.5x out of 4 cores. Once
    there are as many processes as cores it is exactly wrong: each rank still asks for eight
    threads, gets one core's worth between them, and spends it on barriers.

    `cores // ranks`, floored at one. On a box with more cores than ranks each rank still gets its
    share of threads, so the setting costs nothing where OpenMP was helping.

    `determinism.job_cpus()` AND NOT `os.cpu_count()`, which is the trap its own docstring names:
    "Cloud Run hands the process a wide mask and os.cpu_count() reports the host". Counting the
    host there would hand every rank as many threads as the machine has cores while the cgroup
    gives the container a handful, which is the exact oversubscription this exists to stop, on the
    one platform where it is hardest to see. It reads the cgroup quota first, the affinity mask
    next, and os.cpu_count() only when nothing else says.
    """
    from meshpipeline.engines.determinism import job_cpus

    n = max(1, int(ranks))
    try:
        c = int(cores) or job_cpus()
    except Exception:                      # noqa: BLE001 - a thread count never costs a build
        c = int(cores) or (os.cpu_count() or 1)
    return max(1, int(c) // n)


def mpi_command(script: str, ranks: int, *, threads: int = 0) -> str:
    """One OpenFOAM utility run across `ranks` processes, with the flags that make it start.

    `-x MPI_BUFFER_SIZE` is not decoration: setting the variable in the launching shell sets it
    for mpirun and for nothing else. Each rank is its own process and reads its own environment
    at Pstream init, so without the export the buffer stays at controlDict's 20 MB in all of
    them and the build dies in cfMesh's load balancing.

    `OMP_NUM_THREADS` travels the same way and for the same reason - see `omp_threads` for the
    run that proved it has to.
    """
    omp = int(threads) or omp_threads(ranks)
    return (f"MPI_BUFFER_SIZE={MPI_BUFFER_BYTES} OMP_NUM_THREADS={omp} "
            f"mpirun {' '.join(MPI_FLAGS)} -x MPI_BUFFER_SIZE -x OMP_NUM_THREADS "
            f"-np {int(ranks)} {script} -parallel")


def region_properties() -> str:
    """`constant/regionProperties`: the two regions, and which of them is the coolant."""
    return ("FoamFile { version 2.0; format ascii; class dictionary; object regionProperties; "
            "location \"constant\"; }\n\n"
            f"regions\n(\n    fluid       ({FLUID_REGION})\n"
            f"    solid       ({SOLID_REGION})\n);\n")


# #
# reading the result
# #
@dataclass
class RegionReport:
    name: str
    cells: int = 0
    volume_m3: float = 0.0
    pieces: int = 1
    mesh_ok: bool = False
    max_non_ortho: float | None = None
    max_skewness: float | None = None
    #: Every patch this region ended up owning, and how many faces each has. The face count is
    #: what proves the coolant actually reaches a face: a patch that survived the split with zero
    #: faces is a name with nothing behind it.
    patches: dict[str, int] = field(default_factory=dict)
    #: Defects `checkMesh` calls fatal, in foam_exec's words. Carried through rather than
    #: re-derived, because that list is where "negative volume cells" and "empty patches on a 3D
    #: mesh" are already named, and both make every solver refuse the case.
    fatal: tuple[str, ...] = ()
    #: WHAT THE MESH IS MADE OF, and the range of sizes in it. checkMesh prints all of this on
    #: every run and this report kept six fields and dropped the rest, so the readings a customer
    #: asks for first - "cell types and count per type", "min and max cell sizes" (Corintis,
    #: 2026-10-29) - were measured, thrown away, and would have been re-derived by hand.
    #: Per region, never collapsed with the other: on a conjugate mesh the coolant is the fine
    #: region and the metal is the coarse one, and a single pair of numbers describes neither.
    faces: int = 0
    points: int = 0
    cell_types: dict[str, int] = field(default_factory=dict)
    min_cell_m: float | None = None
    max_cell_m: float | None = None
    max_aspect_ratio: float | None = None
    avg_non_ortho: float | None = None
    skew_faces: int = 0
    #: Cells too flat for a solver to integrate over. checkMesh names these only when it finds
    #: them, so zero is the honest default rather than a missing reading.
    small_determinant_cells: int = 0
    #: WHERE THE POOR CELLS ARE, not how many: [{name, path, bytes}] per set checkMesh
    #: collected them into (foam_exec.POOR_SETS), kept under poor_cells/<region>/. The
    #: count has been carried since the readings were widened, and a count says a
    #: customer has a problem without saying where - "highlight of poor-quality cells"
    #: was the sixth thing Corintis asked for (2026-10-29). Empty on a mesh that passed,
    #: because the sets ARE the cells that failed a check and a clean mesh has none.
    poor_sets: tuple[dict, ...] = ()

    @property
    def mean_cell_m(self) -> float:
        """The cube root of the mean cell volume, as a stand-in for the cell size.

        A LOWER BOUND ON HOW FINE THE REGION IS, not a measurement of the interface cell. The
        refinement only reaches `refinementThickness` from the coolant surface, so a wide passage
        keeps coarser cells down the middle and drags this number up. Where the refinement covers
        the passage outright - the usual case on a microchannel - it is the real cell size.
        """
        if self.cells <= 0 or self.volume_m3 <= 0:
            return 0.0
        return (self.volume_m3 / self.cells) ** (1.0 / 3.0)

    def cells_across(self, width_m: float) -> float:
        """How many mean cells fit across a passage of this width. Evidence, never a rule."""
        c = self.mean_cell_m
        return (width_m / c) if c > 0 else 0.0


@dataclass
class ConjugateReport:
    fluid: RegionReport
    solid: RegionReport
    interface_faces_fluid: int = 0
    interface_faces_solid: int = 0
    ports: PortPlan | None = None
    #: {block face -> the name the user gave it}, as renameBoundary was told. The port gate reads
    #: it because a declared port's patch is called what the user called it, not what the face it
    #: was cut from is called, and looking for the face name reported a missing port on a mesh
    #: that had it under the right name.
    patch_names: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    #: Things worth knowing that do not make the mesh unusable. Kept apart from `problems` on
    #: purpose: a run that prints eight warnings and a pass reads exactly like a run that failed.
    notes: list[str] = field(default_factory=list)
    #: The high-water mark of resident memory across every stage, in bytes. MEASURED on every run,
    #: because guessing it wrong is how a job gets OOM-killed: a predicted 4.9 GB build was killed
    #: by the kernel at 8.0 GB resident. The finished mesh's footprint and the mesher's peak while
    #: building it are different numbers, and only the first was ever measured here.
    #:
    #: ZERO WHEN IT CANNOT BE ATTRIBUTED TO THIS BUILD, which is not the same as zero memory.
    #: `RUSAGE_CHILDREN.ru_maxrss` is a maximum over every child the process has ever reaped, not
    #: over this build's, so inside a sweep the second build inherits the first one's figure. It is
    #: reported only when this build raised the mark; otherwise a smaller build would be recorded
    #: as having used a bigger build's memory, and the fitted bytes-per-cell would be fitted on
    #: that.
    peak_rss_bytes: int = 0
    #: Seconds per stage. MEASURED, because the cost is not where anyone expects it: on a
    #: 100-channel part cartesianMesh took 617 s and the tagging and splitting after it took over
    #: an hour. For a conjugate mesh the post-processing dominates by a wide margin, so a turnaround
    #: promise that budgets only for the mesher is a promise about the wrong stage. Nothing here is
    #: optimised yet; this is the measurement that says where to start.
    seconds: dict[str, float] = field(default_factory=dict)

    @property
    def mesher_seconds(self) -> float:
        return self.seconds.get("cartesianMesh", 0.0)

    @property
    def post_seconds(self) -> float:
        """Everything after the mesher: tagging, splitting and the per-region checks."""
        return sum(v for k, v in self.seconds.items() if k != "cartesianMesh")

    @property
    def post_share(self) -> float:
        """The share of wall time spent after the mesher, 0 to 1."""
        total = sum(self.seconds.values())
        return (self.post_seconds / total) if total else 0.0

    @property
    def conformal(self) -> bool:
        return (self.interface_faces_fluid > 0
                and self.interface_faces_fluid == self.interface_faces_solid)

    @property
    def inlet_faces(self) -> int:
        return self.fluid.patches.get(self.ports.inlet, 0) if self.ports else 0

    @property
    def outlet_faces(self) -> int:
        return self.fluid.patches.get(self.ports.outlet, 0) if self.ports else 0

    @property
    def flowable(self) -> bool:
        """Whether the coolant has a way in and a way out, which a solve needs before anything
        else: with neither, nothing fixes its pressure and no solver will run."""
        return self.inlet_faces > 0 and self.outlet_faces > 0

    @property
    def ok(self) -> bool:
        return not self.problems


def read_region(quality: dict, name: str) -> RegionReport:
    """Turn one region's `foam_exec.check_mesh` result into a report.

    THE CHECKMESH OUTPUT IS NOT PARSED HERE. `foam_exec.check_mesh` has parsed it for every cfMesh
    path since long before this one existed, so a second reader of the same text is a second thing
    that can disagree with the first about what `checkMesh` said.

    It also runs `scan_case_dicts`, but only when IT runs - which is after the mesher, the tagging
    and the split have all executed. That is not a guard on those three, and this docstring used
    to claim it was. `build` scans the case itself before the first stage now.

    PER REGION, never `-allRegions`. `checkMesh -allRegions` reports the PRE-SPLIT mesh, which
    looks like a successful verification and is a measurement of the wrong thing.
    """
    return RegionReport(
        name=name,
        mesh_ok=bool(quality.get("mesh_ok")),
        cells=int(quality.get("cells") or 0),
        volume_m3=float(quality.get("total_volume") or 0.0),
        # checkMesh prints the piece count only when it looks; absent means one body.
        pieces=int(quality.get("regions") or 1),
        max_non_ortho=quality.get("max_non_ortho"),
        max_skewness=quality.get("max_skewness"),
        fatal=tuple(quality.get("fatal") or ()),
        # Everything else checkMesh measured. This used to stop at the six fields above, which
        # meant a run paid for the measurement and then kept a tenth of it.
        faces=int(quality.get("faces") or 0),
        points=int(quality.get("points") or 0),
        cell_types=dict(quality.get("cell_types") or {}),
        min_cell_m=quality.get("min_cell_m"),
        max_cell_m=quality.get("max_cell_m"),
        max_aspect_ratio=quality.get("max_aspect_ratio"),
        avg_non_ortho=quality.get("avg_non_ortho"),
        skew_faces=int(quality.get("skew_faces") or 0),
        small_determinant_cells=int(quality.get("small_determinant_cells") or 0),
        poor_sets=tuple(dict(x) for x in (quality.get("poor_sets") or ())),
    )


def boundary_patches(polymesh) -> dict[str, int]:
    """Every patch in a region's polyMesh, with its face count.

    `polymesh_surface.read_boundary_patches` already reads this file, in this package, for
    engines/passage and engines/passage_flow. What is wanted here is the same answer in a shape
    convenient for asking about one patch, so this narrows it rather than re-reading the file.

    The face COUNT is what matters after a split: the question is not "does this patch exist" but
    "which of the block's faces did this region end up owning, and with how many faces each". A
    port is exactly that question, and a patch that survived the split with zero faces is a name
    with nothing behind it.
    """
    return {p["name"]: p["n_faces"] for p in read_boundary_patches(Path(polymesh))}


def interface_faces(patches: dict[str, int], patch: str) -> int:
    """The face count on one side of the interface."""
    return patches.get(patch, 0)


#: How far the meshed coolant may be from the CAD before the tagging is not believed, when nobody
#: says otherwise. It is the staircase allowance at the resolution the platform's own rule asks for,
#: and it was the ONLY tolerance for a while, which made the gate ask a 2-cells-across mesh to be
#: something it was never going to be. Measured errors at 2 across run to 27.5% and at 5 across to
#: 13.3%, so a flat 5% refused six of the eight coarse meshes in this package for being coarse, and
#: `check` and `sweep` never reached their solve. The caller passes a tolerance derived from
#: engines/staircase_error for the resolution it actually asked for; see semi.volume_tolerance.
DEFAULT_VOLUME_TOL = 0.05


def verify(report: ConjugateReport, *, expect_fluid_volume_m3: float | None = None,
           expect_fluid_pieces: int | None = None, volume_tol: float = DEFAULT_VOLUME_TOL,
           narrowest_m: float | None = None) -> ConjugateReport:
    """Decide whether this mesh can be handed on, and say exactly why not when it cannot.

    FIVE CHECKS, and each one catches something the others cannot.

    Conformality is the first, and on its own it proves almost nothing: a split that put half the
    metal in the coolant region would still be perfectly conformal. What it proves is that the two
    sides share a surface.

    The fluid VOLUME against the CAD is what proves the tagging was right - which cells ended up on
    which side. The two together are the real test.

    CONNECTIVITY is the one checkMesh will not give you. It prints the piece count with its own
    warning asterisk and still says "Mesh OK", because disconnection is not fatal to it. A mesh can
    pass every geometric bar and be unsolvable, because an island of cells has no face path to the
    pressure reference cell.

    PORTS are the other thing checkMesh is happy without. A sealed coolant region passes every
    geometric bar there is and cannot be solved, because nothing anywhere fixes its pressure, and
    the first conjugate solve run here died in the preconditioner for precisely that reason. The
    failure mode worth catching is a named patch with zero faces: the name survives the split
    whether or not any coolant reached it.

    And each region's own checkMesh, because `-allRegions` measured the mesh from before the split.
    """
    p = report.problems
    if not report.conformal:
        p.append(
            f"interface is not conformal: {report.interface_faces_fluid} faces on the fluid side "
            f"against {report.interface_faces_solid} on the solid side. The two regions do not see "
            "the same surface, so the heat coupling is unsound whatever checkMesh says.")
    for r in (report.fluid, report.solid):
        if not r.mesh_ok:
            p.append(f"{r.name}: checkMesh did not report Mesh OK")
        if r.cells <= 0:
            p.append(f"{r.name}: no cells")
        for defect in r.fatal:
            p.append(f"{r.name}: {defect}, which every solver refuses outright")
    if expect_fluid_pieces is not None and report.fluid.pieces != expect_fluid_pieces:
        p.append(
            f"fluid is in {report.fluid.pieces} disconnected piece(s), expected "
            f"{expect_fluid_pieces}. Islands mean the cells are too coarse to hold the passage "
            "together; this mesh will not solve even though checkMesh calls it OK.")
    if expect_fluid_volume_m3:
        err = abs(report.fluid.volume_m3 - expect_fluid_volume_m3) / expect_fluid_volume_m3
        if err > volume_tol:
            p.append(
                f"fluid volume is {err * 100:.2f}% from the {expect_fluid_volume_m3:.6g} m^3 the "
                f"CAD declares, past the {volume_tol * 100:.0f}% staircase allowance. The tagged "
                "cells are not the coolant.")
    if (plan := report.ports) is not None:
        # THE NAME THE MESH CARRIES, not the block face it was cut from. A declared port is
        # renamed before the split (render_meshdict's patch_names), so the fluid region's patch is
        # called whatever the user called it - coolant_in, not ymin - and looking for the face name
        # reported a missing port on a mesh that had it. Measured: renaming ymin to coolant_in gave
        # the fluid region coolant_in with 128 faces, exactly what ymin had.
        renamed = report.patch_names or {}
        for role, face in (("inlet", plan.inlet), ("outlet", plan.outlet)):
            want = renamed.get(face, face)
            n = report.fluid.patches.get(want)
            as_cut = "" if want == face else f" (the {face} face, renamed)"
            if n is None:
                p.append(
                    f"the {role} was to be {want}{as_cut}, and the fluid region has no patch of "
                    f"that name. The patches it does have are {sorted(report.fluid.patches)}.")
            elif n == 0:
                p.append(
                    f"the {role} patch {want}{as_cut} exists on the fluid region with ZERO faces, "
                    "so no coolant reaches it. The coolant is sealed on that side, nothing fixes "
                    "its pressure, and the solve dies in the preconditioner.")
        if report.flowable:
            lo, hi = sorted((report.inlet_faces, report.outlet_faces))
            if hi > 10 * lo:
                report.notes.append(
                    f"the {plan.inlet} port carries {report.inlet_faces:,} faces and {plan.outlet} "
                    f"carries {report.outlet_faces:,}, more than ten to one. That is normal for a "
                    "manifold and a sign of a staircased-away port on anything symmetric, so it is "
                    "worth knowing which this is.")
        if plan.walls:
            report.notes.append(
                f"the coolant also reaches {', '.join(plan.walls)}, which are sealed as walls. On a "
                "block cut out of a wider channel array that is correct - those are the faces it "
                "was cut on - and on a part whose channels are open at the top it is a lid of zero "
                "thickness. Grow the block on that face instead if the lid should conduct.")
    if narrowest_m:
        across = report.fluid.cells_across(narrowest_m)
        report.notes.append(
            f"the fluid region averages {report.fluid.mean_cell_m * 1e6:.2f} um per cell, which is "
            f"{across:.1f} cells across the part's narrowest {narrowest_m * 1e6:.0f} um passage. "
            "A lower bound: cells away from the coolant surface are coarser and pull the mean up.")
    return report


# #
# running it
# #
def _foam(workspace: Path, script: str, log: Path, *, bashrc: str,
          timeout: int) -> tuple[int, float]:
    """Run one OpenFOAM stage and return its exit code AND how long it took.

    The duration is returned rather than logged because the stage split is the finding: on a
    100-channel part the mesher took 617 s and everything after it took over an hour, so a
    turnaround budget that counts only the mesher is counting the wrong stage. See
    ConjugateReport.seconds.
    """
    # chr(10) rather than an escape, because this line has been mangled twice by tooling that
    # processes backslashes on the way through. A named constant cannot be re-escaped by accident.
    cmd = _NEWLINE.join((f"source {bashrc} >/dev/null 2>&1 || true", script, ""))
    t0 = time.monotonic()
    with log.open("w") as fh:
        proc = run_guarded(["bash", "-lc", cmd], cwd=str(workspace), env=_foam_env(),
                           stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
    return proc.returncode, time.monotonic() - t0


def children_peak_rss_bytes() -> int:
    """The high-water mark of resident memory across every child process reaped so far.

    `RUSAGE_CHILDREN.ru_maxrss` is cumulative-maximum rather than per-child, so this is the peak of
    the whole run and not of one stage. That is the number worth having: what matters is whether
    the machine could hold the biggest moment, and on Linux the biggest moment is cartesianMesh's
    octree. Returns 0 where it cannot be read rather than guessing.
    """
    if resource is None:
        return 0
    try:
        # ru_maxrss is KILOBYTES on Linux and BYTES on macOS. Linux is the only place this runs.
        return int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * 1024
    except (OSError, ValueError):                        # pragma: no cover
        return 0


def build(workspace, *, block_stl: str = "constant/triSurface/block.stl",
          fluid_stl: str = "constant/triSurface/fluid.stl",
          background_cell_m: float, levels: int = DEFAULT_REFINEMENT_LEVELS,
          thickness_m: float | None = None,
          expect_fluid_volume_m3: float | None = None,
          expect_fluid_pieces: int | None = None, volume_tol: float = DEFAULT_VOLUME_TOL,
          ports: PortPlan | None = None, narrowest_m: float | None = None,
          patch_names: dict | None = None, fine_zones: list | None = None,
          fine_surfaces: list | None = None, stretch_zones: list | None = None,
          port_patches: list | None = None, ranks: int = 1,
          bashrc: str = _DEFAULT_BASHRC, timeout: int = 3600) -> ConjugateReport:
    """Mesh the block, split it in two, and report what was actually produced."""
    ws = Path(workspace)
    # The mark BEFORE this build, so the one after it can be attributed. See peak_rss_bytes.
    rss_before = children_peak_rss_bytes()
    (ws / "system").mkdir(parents=True, exist_ok=True)
    # THE CASE'S OWN DICTIONARIES, if nobody has written them yet. In a pipeline run the spec's
    # `case_scaffold` hook has already run and this does nothing. Outside one it is the difference
    # between a mesher that starts and a FOAM FATAL ERROR on a missing controlDict after the
    # workspace has been staged - every OpenFOAM application reads controlDict before doing
    # anything, and this function writes two dictionaries into the same directory while assuming
    # a third. It assumes it no longer.
    if not (ws / "system" / "controlDict").is_file():
        from meshpipeline.engines.cfmesh_conjugate.case_scaffold import write_case_skeleton

        write_case_skeleton(ws)
    # THE ZONES ARE PASSED HERE, and for one run they were not. render_meshdict has taken
    # `fine_zones` since the day it was written, the unit tests hand them to it directly, and this
    # - the only place in the package that writes a meshDict - left the argument off. cfMesh read a
    # dictionary with no objectRefinements block, built the uniform mesh, and reported success: the
    # run came back with 99,172,391 cells and a 74.61 GB peak, matching the un-zoned run of the
    # same part to the byte. Nothing failed. A renderer tested through its own front door cannot
    # catch an argument the caller never supplies, which is what
    # test_the_zones_reach_the_dictionary_the_mesher_reads now covers.
    (ws / "system" / "meshDict").write_text(render_meshdict(
        block_file=block_stl, fluid_file=fluid_stl,
        background_cell_m=background_cell_m, levels=levels, thickness_m=thickness_m,
        patch_names=patch_names, fine_zones=fine_zones, fine_surfaces=fine_surfaces,
        stretch_zones=stretch_zones))
    (ws / "system" / "topoSetDict").write_text(render_toposetdict(fluid_file=fluid_stl))
    # BEFORE THE SCAN, not after it. scan_case_dicts below reads every dictionary in system/ and
    # constant/, and a dictionary written after it is a dictionary nothing checked.
    if int(ranks) > 1:
        (ws / "system" / "decomposeParDict").write_text(render_decompose_pardict(int(ranks)))

    # THE CASE IS SCANNED BEFORE ANYTHING RUNS, which is where every other OpenFOAM bundle scans
    # (cfmesh/native.py, cfmesh/cfmesh_runner.py, snappy/native.py, snappy_multiregion/native.py
    # all do it at the top of their runner). This path only scanned when `check_mesh` ran, which
    # is AFTER cartesianMesh, topoSet and splitMeshRegions have all executed - so the guard was
    # reporting on dictionaries that had already been handed to OpenFOAM three times. An engine
    # row is exactly the change that makes a customer's workspace the argument here, so the order
    # matters from now on rather than in principle.
    reason = scan_case_dicts(ws)
    if reason:
        raise ConjugateError(f"the case dictionaries were rejected before any stage ran: {reason}")

    # THE PORT PATCHES ARE CUT AFTER THE SPLIT, ON THE COOLANT ALONE, and that placement was
    # measured rather than reasoned. Cut on the block mesh beforehand, one cut serves both regions
    # - which sounds right, and is how `renameBoundary` works, because a renamed face genuinely is
    # shared: the coolant's end and the metal's outer face at that end are two faces of one plane.
    # A CUT port is not that. The box round a mouth catches the lid metal ringing the bore as well
    # as the bore itself, and the split then hands the metal a patch called `coolant_in`. Measured
    # on the ported two-scale plate, 2026-10-29: the coolant got 36 faces and 36, and the metal got
    # 50 and 53 of lid surface wearing the coolant's names. Those are external walls and naming
    # them after a port is how a solver comes to have a mass-flow inlet on the outside of a plate.
    #
    # After the split and with `-region fluid` the same box selects from the coolant's own `zmax`
    # and the metal keeps its face intact.
    #
    # THE WHOLE CHAIN GOES PARALLEL, AND THE MERGE IS OF THE REGIONS. The ceiling that forces the
    # mesher onto ranks is cfMesh's octree addressing (OCTREE_BOXES_PER_PROCESS) and nothing
    # downstream shares it, so this used to merge the block the moment it existed and run the rest
    # serially. That was right about correctness and wrong about time. Measured on the 336,666,582
    # cell customer build:
    #
    #     cartesianMesh        2,399.6 s   on 8 ranks
    #     reconstructParMesh   4,518.4 s   one process
    #     topoSet              2,240.2 s   one process
    #     splitMeshRegions     6,073.3 s   one process
    #
    # The three serial stages are 4.4x the mesher. Staying decomposed to the end makes topoSet and
    # the split parallel and leaves one merge, of two smaller meshes instead of the whole block.
    #
    # THE TWO CHAINS WERE MEASURED AGAINST EACH OTHER on the coupon's own block and coolant, at a
    # size where the answer is checkable:
    #
    #                     fluid cells   solid cells   interface   fluid non-ortho   solid
    #     serial               29,812        29,262      22,534           16.2739   35.3268
    #     parallel to end      29,813        29,261      22,534           16.2739   35.3442
    #
    # Both pass checkMesh. The interface is 22,534 faces on both sides in both chains, which is
    # the conformality the whole approach rests on, and the total is 59,074 either way. ONE CELL
    # MOVES between the regions: `topoSet surfaceToCell` classifies a cell on a processor boundary
    # differently from how it classifies it with the mesh whole. That is a real difference and not
    # a rounding - the parallel chain is not bit-identical - and it is one cell in 59,074 on the
    # boundary between two regions that share 22,534 faces exactly.
    if int(ranks) > 1:
        chain: list[tuple[str, str]] = [
            (PREPARE_PAR, PREPARE_PAR),
            ("cartesianMesh", mpi_command("cartesianMesh", int(ranks))),
            ("topoSet", mpi_command("topoSet", int(ranks))),
            ("splitMeshRegions", mpi_command(" ".join(SPLIT_ARGS), int(ranks))),
            ("reconstructParMesh", RECONSTRUCT_REGIONS),
            ("regionDicts", REGION_DICTS),
        ]
    else:
        chain = [("cartesianMesh", "cartesianMesh"),
                 ("topoSet", "topoSet"),
                 ("splitMeshRegions", " ".join(SPLIT_ARGS))]
    if port_patches:
        topo_ports, create_ports = render_port_patch_dicts(list(port_patches))
        # UNDER THE REGION'S OWN system/, AND UNDER THE DEFAULT NAME, so neither command needs
        # `-dict` at all. `-dict` with `-region` does not mean what it looks like: the path is
        # resolved as <case>/<region>/<path>, so `system/fluid/topoSetDict.ports` asks for
        # case/fluid/system/fluid/... and a bare filename asks for case/fluid/<name>. Both exit
        # on the missing file. The region-aware lookup finds system/<region>/topoSetDict by
        # itself and does not fall back to the root one, which the split's own dict relies on in
        # the other direction.
        sysdir = ws / "system" / FLUID_REGION
        sysdir.mkdir(parents=True, exist_ok=True)
        (sysdir / "topoSetDict").write_text(topo_ports)
        (sysdir / "createPatchDict").write_text(create_ports)
        # `-constant` on createPatch is not an option and it exits on it, the same way
        # splitMeshRegions does. topoSet takes it; createPatch does not. Both were written from
        # the same habit and only one of them was right.
        chain += [("topoSetPorts", f"topoSet -constant -region {FLUID_REGION}"),
                  ("createPatch", f"createPatch -overwrite -region {FLUID_REGION}")]

    seconds: dict[str, float] = {}
    for stage, script in chain:
        rc, took = _foam(ws, script, ws / f"{stage}.log", bashrc=bashrc, timeout=timeout)
        seconds[stage] = round(took, 1)
        if rc != 0:
            raise ConjugateError(f"{stage} exited {rc} after {took:.0f}s; see {stage}.log",
                                 rc=rc, stage=stage)

    # THE PER-RANK COPIES GO, once the merged mesh is on disk. They are a second whole copy of
    # the mesh: at the sizes that need ranks at all that is hundreds of gigabytes of scratch, and
    # the workspace is archived and returned whole. Removed only after reconstructParMesh has
    # exited 0, which the chain above guarantees by raising on any other code.
    if int(ranks) > 1:
        import shutil

        for d in sorted(ws.glob("processor*")):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    # Out of constant/ before anything scans it again. See CELL_TO_REGION.
    leftover = ws / "constant" / CELL_TO_REGION
    if leftover.is_file():
        leftover.replace(ws / CELL_TO_REGION)

    regions = sorted(d.name for d in (ws / "constant").iterdir()
                     if d.is_dir() and (d / "polyMesh").is_dir())
    if set(regions) != {FLUID_REGION, SOLID_REGION}:
        # The check that would have stopped the 84-region run in its first minute instead of an
        # hour in. More than two regions means the split walked connectivity rather than the zones.
        raise ConjugateError(
            f"expected exactly the regions {FLUID_REGION} and {SOLID_REGION}, got {regions}")
    (ws / "constant" / REGION_PROPERTIES).write_text(region_properties())

    reports = {}
    for name in (FLUID_REGION, SOLID_REGION):
        t0 = time.monotonic()
        # write_fields: checkMesh writes down the per-cell numbers it has already worked out, so
        # the viewer can colour a face with the measurement rather than a second implementation
        # of it. +15.1% on this call, and its verdict is byte-identical (foam_exec.check_mesh).
        quality = check_mesh(ws, bashrc=bashrc, region=name, write_fields=True)
        seconds[f"checkMesh:{name}"] = round(time.monotonic() - t0, 1)
        reports[name] = read_region(quality, name)
        polymesh = ws / "constant" / name / "polyMesh"
        if (polymesh / "boundary").is_file():
            reports[name].patches = boundary_patches(polymesh)

    rss_after = children_peak_rss_bytes()
    rep = ConjugateReport(fluid=reports[FLUID_REGION], solid=reports[SOLID_REGION],
                          ports=ports, seconds=seconds,
                          patch_names={f: n for f, (n, _r) in (patch_names or {}).items()},
                          peak_rss_bytes=rss_after if rss_after > rss_before else 0)
    rep.interface_faces_fluid = interface_faces(rep.fluid.patches, FLUID_TO_SOLID)
    rep.interface_faces_solid = interface_faces(rep.solid.patches, SOLID_TO_FLUID)
    return verify(rep, expect_fluid_volume_m3=expect_fluid_volume_m3,
                  expect_fluid_pieces=expect_fluid_pieces, volume_tol=volume_tol,
                  narrowest_m=narrowest_m)


__all__ = ["ConjugateError", "ConjugateReport", "RegionReport", "PortPlan",
           "FLUID_REGION", "SOLID_REGION", "FLUID_TO_SOLID", "SOLID_TO_FLUID",
           "SPLIT_ARGS", "DEFAULT_REFINEMENT_LEVELS", "DEFAULT_VOLUME_TOL",
           "OCTREE_BOXES_PER_PROCESS", "MPI_FLAGS", "DECOMPOSE_METHOD", "PREPARE_PAR",
           "ranks_for", "render_decompose_pardict", "mpi_command", "omp_threads",
           "MPI_BUFFER_BYTES",
           "BLOCK_FACES", "OPEN_FACE_RTOL", "OUTER_PATCH", "is_domain_face", "use_shell_domain",
           "RECONSTRUCT_REGIONS", "REGION_DICTS",
           "CELL_TO_REGION", "REGION_PROPERTIES", "region_properties",
           "block_bounds", "block_surface_stl", "open_faces", "plan_ports",
           "background_complaint", "MIN_BACKGROUND_CELLS_ACROSS_BLOCK",
           "part_size_complaint", "MAX_PART_M",
           "children_peak_rss_bytes",
           "render_meshdict", "render_toposetdict", "interface_cell_m",
           "read_region", "boundary_patches", "interface_faces", "verify", "build"]
