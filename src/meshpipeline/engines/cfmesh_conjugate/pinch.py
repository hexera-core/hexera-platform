# Responsibility: Reconnect metal the voxel split severed at a diagonal step, before the split.
# Boundaries: it reads `cellToRegion` and the cellZones and writes one cellSet and one dictionary.
# It runs nothing; conjugate.build owns the chain, and topoSet does every mesh traversal.
"""THE METAL COMES OUT IN PIECES, AND IT IS ONE BODY.

`surfaceToCell useSurfaceOrientation` tags a cell as coolant when its CENTRE is inside the coolant
surface, so the conjugate interface is a staircase of whole cells. Where the metal web between two
passages narrows below one cell, the cell on the web takes the coolant's side and the metal either
side of it is left touching along an EDGE. `checkMesh` counts regions by FACE connectivity, so it
calls them separate, and a solver then sees fins that conduct nothing.

MEASURED on `two_scale_plate_ported`:

    8 across, 155 M cells   59 solid regions; 58 fragments, 766,192 cells, 16,186 bad edges
    2 across, 7.3 M cells    5 solid regions;  4 fragments, 1,883 cells each, 3,462 bad edges

and the fragments are IDENTICAL in size at each resolution, because they are one repeated design
feature. The count grows as the mesh is refined - 4 at 2 across, 58 at 8 across - so this is not a
mesh that is too coarse, it is a classification that works in whole cells.

IT IS A PINCH, NOT A GAP, and that is the measurement the whole repair rests on. In the SOLID mesh
the coolant cells are absent, so two metal cells sharing only an edge are both present in it and
share that edge's points. On the 2-across case each fragment shares exactly 36 points and ZERO
faces with the main body. The metal is one body; the mesh severed it.

SO ONE CELL PER PINCH RECONNECTS IT. Measured end to end: 296 coolant cells of 2,425,414, which is
0.0122% of them and 0.0089% of the coolant's volume, and the solid goes from 5 regions to 1 (OK).
The coolant is NOT severed in exchange, which was the real risk - a diagonal cuts both ways.

WHY THE WORK IS IN A DICTIONARY AND NOT IN HERE. The first prototype found the pinches by parsing
the mesh in Python. That is fine on 7 M cells and hopeless on 155 M, which is half a billion faces
of ASCII. `topoSet` already walks meshes, and the pinch has an exact set-algebra spelling:

    pinchPoints  = cellToPoint(islandCells)                 every point of the severed pieces
    pinchPoints subset cellToPoint(mainSolid)               ... that the main body also touches
    bridgeCells  = pointToCell(pinchPoints, any)            every cell at those points
    fluidCells  subtract bridgeCells                        which takes only the coolant ones

Measured on the 2-across case, that dictionary reports `pointSet pinchPoints now size 10804` then
`now size 144`, `cellSet bridgeCells now size 576`, and `cellSet fluidCells now size 2425118` -
the same 296 cells the hand-written prototype picked, found without Python seeing a single face.
`action subset` on a pointSet with a `cellToPoint` source is the load-bearing part of that and it
is not documented anywhere I could find; it is measured.

The ONLY thing this module computes is which cells are in a severed piece, and that is read off
`cellToRegion` - a flat list of ints, one per cell - so it costs the same at any mesh size.

WHERE IT SITS IN THE CHAIN, and why it cannot sit anywhere else:

    topoSet                                       builds fluidCells, solidCells and both zones
    splitMeshRegions -cellZones -detectOnly       writes cellToRegion: 9.1 s on 7.3 M cells
    this module                                   writes islandCells
    topoSet -dict topoSetDict.pinch               the algebra above, rebuilding both zones
    splitMeshRegions -cellZonesOnly -overwrite    the real split, as before

AFTER the split is too late: the layer pass runs on the split mesh and re-splitting would throw
its layers away. BEFORE the split there is no `cellToRegion` to read, which is what the detect
pass is for - `-cellZones` walks face connectivity FIRST, which is exactly "connected components
within each zone", and `-detectOnly` writes the answer without building a mesh per region. The
engine's note on SPLIT_ARGS warns that `-cellZones` was slow on an array of 100 channels; that was
the full split building 84 meshes, and the walk on its own is 9.1 s against that case's 25 s split.

NO ISLANDS IS NOT A SPECIAL CASE. An empty `islandCells` makes `pinchPoints` empty, so
`bridgeCells` is empty and `fluidCells` is unchanged: the dictionary rebuilds exactly the zones it
was given. There is no branch here that a part without severed metal can take by surprise.
"""
from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

#: How the connected components are found. `-cellZones`, NOT `-cellZonesOnly`: the first walks face
#: connectivity and then matches zones, which is the per-zone component count this needs, and
#: `-detectOnly` writes `cellToRegion` without building a mesh per region.
DETECT_ARGS = ("splitMeshRegions", "-cellZones", "-detectOnly", "-overwrite")

#: What `-detectOnly` writes, per processor directory, as a flat labelList.
CELL_TO_REGION = "cellToRegion"

#: The set this module writes and the dictionary that reads it.
ISLAND_SET = "islandCells"
PINCH_DICT = "topoSetDict.pinch"

#: The zone names, which have to match conjugate.FLUID_REGION / SOLID_REGION. Imported from there
#: would be a circular import - conjugate imports this - so they are arguments with these defaults
#: and `conjugate` passes its own.
FLUID_ZONE = "fluid"
SOLID_ZONE = "solid"
#: The cellSet names `topoSet` left behind, which the pinch dictionary edits rather than rebuilds.
FLUID_CELLS = "fluidCells"
SOLID_CELLS = "solidCells"

_LIST_HEADER = re.compile(rb"^\s*(\d+)\s*\(", re.M)
#: Where the FoamFile header stops. Everything above it can mention a zone name without being one.
_HEADER_END = "// * * *"


class PinchError(RuntimeError):
    """Raised when the repair cannot be prepared, never when there is simply nothing to repair."""


def read_labels(path: Path) -> list[int]:
    """A FoamFile holding `N ( a b c ... )` of integers, as a list.

    Everything before the first `}` is the header; a `labelList` body is `N` then the bracketed
    run. Nothing here is clever because nothing here needs to be: `cellToRegion` and a cellSet are
    both flat.
    """
    raw = Path(path).read_bytes()
    cut = raw.find(b"}")
    body = raw[cut + 1:] if cut != -1 else raw
    match = _LIST_HEADER.search(body)
    if not match:
        raise PinchError(f"{path} does not hold a list: no `N (` after its header")
    try:
        end = body.rindex(b")")
    except ValueError as exc:
        raise PinchError(f"{path} has no closing bracket") from exc
    return [int(x) for x in body[match.end():end].split()]


def zone_cells(polymesh: Path, name: str) -> list[int]:
    """The cells of one cellZone.

    PAST THE HEADER, AND THAT IS NOT FUSSINESS. `cellZones` carries
    `meta { names 2 ( fluid solid ); }` inside its FoamFile block, so looking for the zone's name
    from the top of the file lands in that list and the `cellLabels` found after it belong to the
    FIRST zone, whichever zone was asked for. Measured: asking for `solid` returned 609,359 cells,
    which is that rank's FLUID count.
    """
    pm = Path(polymesh)
    text = (pm / "cellZones").read_text(errors="replace")
    try:
        text = text[text.index(_HEADER_END):]
    except ValueError as exc:
        raise PinchError(f"{pm / 'cellZones'} has no FoamFile header") from exc
    at = text.find(name)
    if at == -1:
        raise PinchError(f"{pm / 'cellZones'} has no zone called {name!r}")
    labels = text.find("cellLabels", at)
    if labels == -1:
        raise PinchError(f"the {name!r} zone in {pm / 'cellZones'} has no cellLabels")
    open_br = text.find("(", labels)
    close_br = text.find(")", open_br)
    if open_br == -1 or close_br == -1:
        raise PinchError(f"the {name!r} zone's cellLabels are not a bracketed list")
    return [int(x) for x in text[open_br + 1:close_br].split()]


def processor_dirs(case: Path) -> list[Path]:
    """The `processorN` directories, in rank order, or none for a serial case."""
    return sorted((p for p in Path(case).glob("processor*") if p.name[9:].isdigit()),
                  key=lambda p: int(p.name[9:]))


def _rank_roots(case: Path) -> list[Path]:
    """Where each mesh lives: one processor directory each in parallel, the case itself serially."""
    ranks = processor_dirs(case)
    return ranks or [Path(case)]


def _rank_labels(root: Path, solid_zone: str) -> tuple[list[int], list[int]]:
    """One rank's region-per-cell and its solid zone, with the missing-detect case named."""
    ctr = root / "constant" / CELL_TO_REGION
    if not ctr.is_file():
        raise PinchError(
            f"{ctr} is missing, so the connected regions were never detected - "
            f"`{' '.join(DETECT_ARGS)}` has to run before this")
    return read_labels(ctr), zone_cells(root / "constant" / "polyMesh", solid_zone)


def solid_region_counts(case: Path, *, solid_zone: str = SOLID_ZONE) -> Counter:
    """How many solid cells each detected region holds, summed over every rank.

    ONE RANK IN MEMORY AT A TIME. The labels are discarded as they are counted, because only the
    counts decide the majority. Keeping them all cost about 8 GB on the 155 M-cell plate - fine on
    a 512 GiB VM and not on the Cloud Run mesh job's 32 GiB, and a step that only works on the big
    machine is a trap for whoever meshes something smaller.
    """
    counts: Counter = Counter()
    for root in _rank_roots(Path(case)):
        region, solid = _rank_labels(root, solid_zone)
        counts.update(region[c] for c in solid if c < len(region))
        del region, solid
    return counts


def severed_cells(case: Path, *, solid_zone: str = SOLID_ZONE) -> dict:
    """Which solid cells are in a severed piece, per rank, and what the piece count is.

    THE MAJORITY IS DECIDED ACROSS ALL RANKS AT ONCE. `splitMeshRegions -parallel` numbers its
    regions globally, so a region id means the same thing on every rank - but a region can straddle
    ranks, and picking the biggest piece rank by rank would call the main body an island wherever
    it happens to be thin. So the counts are summed first, in one pass, and the majority chosen
    once; the second pass re-reads a rank to name its severed cells and lets it go again.
    """
    case = Path(case)
    counts = solid_region_counts(case, solid_zone=solid_zone)
    if not counts:
        raise PinchError(f"the {solid_zone!r} zone holds no cells on any rank")
    main_region, main_cells = counts.most_common(1)[0]

    out = {"regions": len(counts), "main_region": int(main_region),
           "main_cells": int(main_cells),
           "severed_cells": int(sum(counts.values()) - main_cells),
           "per_rank": {}}
    for root in _rank_roots(case):
        region, solid = _rank_labels(root, solid_zone)
        out["per_rank"][root] = sorted(
            c for c in solid if c < len(region) and region[c] != main_region)
        del region, solid
    return out


def render_island_set(cells) -> str:
    """`constant/polyMesh/sets/islandCells`, which the pinch dictionary reads."""
    ids = list(cells)
    return "\n".join([
        "FoamFile",
        "{",
        "    version     2.0;",
        "    format      ascii;",
        '    arch        "LSB;label=32;scalar=64";',
        "    class       cellSet;",
        '    location    "constant/polyMesh/sets";',
        f"    object      {ISLAND_SET};",
        "}",
        "",
        str(len(ids)),
        "(",
        *(str(int(c)) for c in ids),
        ")",
        "",
    ])


def render_pinch_toposetdict(*, fluid_zone: str = FLUID_ZONE,
                             solid_zone: str = SOLID_ZONE) -> str:
    """The set algebra that moves one cell per pinch out of the coolant.

    Both zones are rebuilt, for the same reason `render_toposetdict` builds both rather than
    leaving a remainder: an unzoned remainder leaves `splitMeshRegions` to decide what those cells
    are, and what it decides is one region per connected lump of metal.
    """
    return "\n".join([
        "FoamFile { version 2.0; format ascii; class dictionary; object topoSetDict; }",
        "actions",
        "(",
        "    // the main metal body: every solid cell not in a severed piece",
        "    {",
        "        name      mainSolid;",
        "        type      cellSet;",
        "        action    new;",
        "        source    cellToCell;",
        f"        set       {SOLID_CELLS};",
        "    }",
        "    {",
        "        name      mainSolid;",
        "        type      cellSet;",
        "        action    subtract;",
        "        source    cellToCell;",
        f"        set       {ISLAND_SET};",
        "    }",
        "    // THE PINCHES: the severed pieces' points that the main body also touches. `subset`",
        "    // on a pointSet with a cellToPoint source is what makes this exact rather than",
        "    // every cell round every island - 144 points here against 10,804 before it.",
        "    {",
        "        name      pinchPoints;",
        "        type      pointSet;",
        "        action    new;",
        "        source    cellToPoint;",
        f"        set       {ISLAND_SET};",
        "        option    all;",
        "    }",
        "    {",
        "        name      pinchPoints;",
        "        type      pointSet;",
        "        action    subset;",
        "        source    cellToPoint;",
        "        set       mainSolid;",
        "        option    all;",
        "    }",
        "    // every cell at a pinch; subtracting takes only the coolant ones out of the coolant",
        "    {",
        "        name      bridgeCells;",
        "        type      cellSet;",
        "        action    new;",
        "        source    pointToCell;",
        "        set       pinchPoints;",
        "        option    any;",
        "    }",
        "    {",
        f"        name      {FLUID_CELLS};",
        "        type      cellSet;",
        "        action    subtract;",
        "        source    cellToCell;",
        "        set       bridgeCells;",
        "    }",
        "    {",
        f"        name      {fluid_zone};",
        "        type      cellZoneSet;",
        "        action    new;",
        "        source    setToCellZone;",
        f"        set       {FLUID_CELLS};",
        "    }",
        "    {",
        f"        name      {SOLID_CELLS};",
        "        type      cellSet;",
        "        action    new;",
        "        source    boxToCell;",
        "        box       (-1e6 -1e6 -1e6) (1e6 1e6 1e6);",
        "    }",
        "    {",
        f"        name      {SOLID_CELLS};",
        "        type      cellSet;",
        "        action    subtract;",
        "        source    cellToCell;",
        f"        set       {FLUID_CELLS};",
        "    }",
        "    {",
        f"        name      {solid_zone};",
        "        type      cellZoneSet;",
        "        action    new;",
        "        source    setToCellZone;",
        f"        set       {SOLID_CELLS};",
        "    }",
        ");",
        "",
    ])


def write_island_sets(case: Path, *, solid_zone: str = SOLID_ZONE) -> dict:
    """Write `islandCells` on every rank. Returns what was found.

    THE DICTIONARY IS NOT WRITTEN HERE, and that is not tidiness. `build` runs `scan_case_dicts`
    over `system/` before the first stage, and says why above the layer dicts: a dictionary written
    after that scan is one the guard never reads, and the next thing that would read it is
    `check_mesh`, by which point a mesh of hundreds of millions of cells has been built and would
    be thrown away on it. This stage runs in the middle of the chain, so anything it writes is
    late. `topoSetDict.pinch` depends on nothing but the zone names, so `build` writes it early;
    only the cellSet has to wait for the detect pass, and a cellSet is not a dictionary.

    AN EMPTY islandCells IS NOT A SPECIAL CASE. The dictionary then selects no points, bridges no
    cells and rebuilds the zones it was given, so a part whose metal came out whole takes the same
    path as one that did not.
    """
    case = Path(case)
    found = severed_cells(case, solid_zone=solid_zone)
    for root, ids in found["per_rank"].items():
        sets_dir = root / "constant" / "polyMesh" / "sets"
        sets_dir.mkdir(parents=True, exist_ok=True)
        (sets_dir / ISLAND_SET).write_text(render_island_set(ids))
    return {"solid_regions": found["regions"], "main_cells": found["main_cells"],
            "severed_cells": found["severed_cells"],
            "ranks": {root.name: len(ids) for root, ids in found["per_rank"].items()}}


def main(argv: list[str] | None = None) -> int:
    """`python3 -m meshpipeline.engines.cfmesh_conjugate.pinch [case]`, which is how the chain
    runs it: the stages are shell commands and this is the one step that is not a FOAM utility."""
    args = list(sys.argv[1:] if argv is None else argv)
    case = Path(args[0]) if args else Path.cwd()
    report = write_island_sets(case)
    print(f"solid regions {report['solid_regions']}, "
          f"main body {report['main_cells']:,} cells, "
          f"severed {report['severed_cells']:,} cells")
    for name, n in sorted(report["ranks"].items()):
        print(f"  {name}: {ISLAND_SET} {n:,}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the chain's entry point
    raise SystemExit(main())


__all__ = ["DETECT_ARGS", "CELL_TO_REGION", "ISLAND_SET", "PINCH_DICT", "PinchError",
           "read_labels", "zone_cells", "processor_dirs", "solid_region_counts", "severed_cells",
           "render_island_set", "render_pinch_toposetdict", "write_island_sets", "main"]
