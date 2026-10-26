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
    """A conjugate build that cannot be trusted. Raised instead of returning a bad mesh."""


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
def render_meshdict(*, block_file: str, fluid_file: str, background_cell_m: float,
                    levels: int = DEFAULT_REFINEMENT_LEVELS,
                    thickness_m: float | None = None) -> str:
    """The meshDict for the single-domain pass over the whole block.

    `surfaceMeshRefinement` is what makes this work. It refines around a surface that is NOT the
    meshed boundary, which is exactly what is needed here: the coolant surface never becomes a wall,
    it only tells the octree where to put small cells. Without it the interface would be staircased
    at the background size, which on a microchannel part is wider than the channels themselves.

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
    return (
        "FoamFile { version 2.0; format ascii; class dictionary; object meshDict; }\n\n"
        f'surfaceFile "{block_file}";\n'
        f"maxCellSize {background_cell_m:.6g};\n\n"
        "surfaceMeshRefinement\n{\n    coolant\n    {\n"
        f'        surfaceFile "{fluid_file}";\n'
        f"        additionalRefinementLevels {levels};\n"
        f"        refinementThickness {thickness:.6g};\n"
        "    }\n}\n")


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
        for role, face in (("inlet", plan.inlet), ("outlet", plan.outlet)):
            n = report.fluid.patches.get(face)
            if n is None:
                p.append(
                    f"the {role} was to be {face}, and the fluid region has no patch of that name. "
                    f"The patches it does have are {sorted(report.fluid.patches)}.")
            elif n == 0:
                p.append(
                    f"the {role} patch {face} exists on the fluid region with ZERO faces, so no "
                    "coolant reaches it. The coolant is sealed on that side, nothing fixes its "
                    "pressure, and the solve dies in the preconditioner.")
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
          bashrc: str = _DEFAULT_BASHRC, timeout: int = 3600) -> ConjugateReport:
    """Mesh the block, split it in two, and report what was actually produced."""
    ws = Path(workspace)
    # The mark BEFORE this build, so the one after it can be attributed. See peak_rss_bytes.
    rss_before = children_peak_rss_bytes()
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "meshDict").write_text(render_meshdict(
        block_file=block_stl, fluid_file=fluid_stl,
        background_cell_m=background_cell_m, levels=levels, thickness_m=thickness_m))
    (ws / "system" / "topoSetDict").write_text(render_toposetdict(fluid_file=fluid_stl))

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

    seconds: dict[str, float] = {}
    for stage, script in (("cartesianMesh", "cartesianMesh"),
                          ("topoSet", "topoSet"),
                          ("splitMeshRegions", " ".join(SPLIT_ARGS))):
        rc, took = _foam(ws, script, ws / f"{stage}.log", bashrc=bashrc, timeout=timeout)
        seconds[stage] = round(took, 1)
        if rc != 0:
            raise ConjugateError(f"{stage} exited {rc} after {took:.0f}s; see {stage}.log")

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
        quality = check_mesh(ws, bashrc=bashrc, region=name)
        seconds[f"checkMesh:{name}"] = round(time.monotonic() - t0, 1)
        reports[name] = read_region(quality, name)
        polymesh = ws / "constant" / name / "polyMesh"
        if (polymesh / "boundary").is_file():
            reports[name].patches = boundary_patches(polymesh)

    rss_after = children_peak_rss_bytes()
    rep = ConjugateReport(fluid=reports[FLUID_REGION], solid=reports[SOLID_REGION],
                          ports=ports, seconds=seconds,
                          peak_rss_bytes=rss_after if rss_after > rss_before else 0)
    rep.interface_faces_fluid = interface_faces(rep.fluid.patches, FLUID_TO_SOLID)
    rep.interface_faces_solid = interface_faces(rep.solid.patches, SOLID_TO_FLUID)
    return verify(rep, expect_fluid_volume_m3=expect_fluid_volume_m3,
                  expect_fluid_pieces=expect_fluid_pieces, volume_tol=volume_tol,
                  narrowest_m=narrowest_m)


__all__ = ["ConjugateError", "ConjugateReport", "RegionReport", "PortPlan",
           "FLUID_REGION", "SOLID_REGION", "FLUID_TO_SOLID", "SOLID_TO_FLUID",
           "SPLIT_ARGS", "DEFAULT_REFINEMENT_LEVELS", "DEFAULT_VOLUME_TOL",
           "BLOCK_FACES", "OPEN_FACE_RTOL",
           "CELL_TO_REGION", "REGION_PROPERTIES", "region_properties",
           "block_bounds", "block_surface_stl", "open_faces", "plan_ports",
           "background_complaint", "MIN_BACKGROUND_CELLS_ACROSS_BLOCK",
           "children_peak_rss_bytes",
           "render_meshdict", "render_toposetdict", "interface_cell_m",
           "read_region", "boundary_patches", "interface_faces", "verify", "build"]
