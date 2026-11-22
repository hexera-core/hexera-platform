"""The metal comes out in pieces, and it is ONE BODY.

`surfaceToCell useSurfaceOrientation` tags a cell as coolant when its CENTRE is inside the coolant
surface, so the conjugate interface is a staircase of whole cells. Where the metal web between two
passages narrows below one cell, the cell on the web takes the coolant's side and the metal either
side of it is left touching along an EDGE. `checkMesh` counts regions by FACE connectivity, so it
calls them separate, and a solver sees fins that conduct nothing.

MEASURED on `two_scale_plate_ported`: 59 solid regions at 8 across (58 fragments, 766,192 cells,
16,186 bad edges) and 5 at 2 across (4 fragments of exactly 1,883 cells each, 3,462 bad edges). The
fragments are identical in size at each resolution because they are one repeated design feature,
and the COUNT GROWS with refinement - so this is the classification working in whole cells, not a
mesh that is too coarse.

IT IS A PINCH, NOT A GAP. In the SOLID mesh the coolant cells are absent, so two metal cells
sharing only an edge are both present in it and share that edge's points. Each fragment shares
exactly 36 points and ZERO faces with the main body.

SO ONE CELL PER PINCH RECONNECTS IT, measured end to end through `conjugate.build` on the 2-across
case: 296 coolant cells of 2,425,414 - 0.0122% of them, 0.0089% of the coolant's volume - and the
solid goes from 5 regions to `Number of regions: 1 (OK)`, with bad edges 3,462 -> 3,326 and the
coolant NOT severed in exchange, which was the real risk since a diagonal cuts both ways.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import pinch as P

#: A `cellZones` file shaped like the real one, META HEADER AND ALL. That header is the trap: it
#: carries `names 2 ( fluid solid );` inside the FoamFile block, so looking for a zone's name from
#: the top of the file lands in that list and the `cellLabels` found after it belong to the FIRST
#: zone. Measured on the real case: asking for `solid` returned 609,359 cells, the FLUID count.
CELLZONES = """FoamFile
{
    version     2.0;
    format      ascii;
    class       regIOobject;
    location    "constant/polyMesh";
    object      cellZones;
    meta
    {
        names           2 ( fluid solid );
    }
}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

2
(fluid
{
    type            cellZone;
    cellLabels      List<label>
3
(
0
1
2
)
;
}
solid
{
    type            cellZone;
    cellLabels      List<label>
4
(
3
4
5
6
)
;
}
)
"""


def _labels_file(n_and_values) -> str:
    vals = list(n_and_values)
    return ("FoamFile\n{\n    version 2.0;\n    format ascii;\n    class labelList;\n"
            "    object cellToRegion;\n}\n\n"
            + str(len(vals)) + "\n(\n" + "\n".join(str(v) for v in vals) + "\n)\n")


def _rank(root, *, regions, zones=CELLZONES):
    pm = root / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "cellZones").write_text(zones)
    (root / "constant" / P.CELL_TO_REGION).write_text(_labels_file(regions))
    return root


# ------------------------------------------------------------------ reading the mesh's own files

def test_a_zone_is_read_past_the_header_that_names_every_zone(tmp_path):
    """THE TRAP THIS EXISTS FOR. `cellZones` carries `meta { names 2 ( fluid solid ); }` inside its
    FoamFile block, so a search from the top of the file finds `solid` in that list and the
    `cellLabels` after it are FLUID's. On the real case that returned 609,359 cells for the solid,
    which is that rank's coolant count."""
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "cellZones").write_text(CELLZONES)
    assert P.zone_cells(pm, "fluid") == [0, 1, 2]
    assert P.zone_cells(pm, "solid") == [3, 4, 5, 6]


def test_a_zone_that_is_not_there_is_said_so(tmp_path):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "cellZones").write_text(CELLZONES)
    with pytest.raises(P.PinchError) as got:
        P.zone_cells(pm, "coolant")
    assert "no zone called" in str(got.value)


def test_a_file_that_is_not_a_list_is_refused(tmp_path):
    p = tmp_path / "cellToRegion"
    p.write_text("FoamFile\n{\n    class labelList;\n}\n\nnothing here\n")
    with pytest.raises(P.PinchError):
        P.read_labels(p)


# ------------------------------------------------------------------ which cells are severed

def test_the_majority_is_decided_across_every_rank_at_once(tmp_path):
    """A REGION CAN STRADDLE RANKS. `splitMeshRegions -parallel` numbers regions globally, so an id
    means the same thing everywhere - but if the biggest piece were chosen rank by rank, the main
    body would be called an island on any rank where it happens to be thin.

    Here region 9 is the main body with 5 cells across the two ranks and region 4 has 3, but on
    rank 0 alone region 4 looks bigger. Deciding per rank would sever the main body on rank 0.
    """
    _rank(tmp_path / "processor0", regions=[0, 0, 0, 4, 4, 4, 9])
    _rank(tmp_path / "processor1", regions=[0, 0, 0, 9, 9, 9, 9])
    found = P.severed_cells(tmp_path)
    assert found["main_region"] == 9
    assert found["main_cells"] == 5
    assert found["severed_cells"] == 3
    islands = {root.name: ids for root, ids in found["per_rank"].items()}
    assert islands["processor0"] == [3, 4, 5]
    assert islands["processor1"] == []


def test_metal_that_came_out_whole_severs_nothing(tmp_path):
    _rank(tmp_path / "processor0", regions=[0, 0, 0, 7, 7, 7, 7])
    found = P.severed_cells(tmp_path)
    assert found["regions"] == 1
    assert found["severed_cells"] == 0
    assert list(found["per_rank"].values()) == [[]]


def test_an_empty_island_set_is_still_written(tmp_path):
    """NOT A SPECIAL CASE, deliberately. With no islands the dictionary selects no points, bridges
    no cells and rebuilds the zones it was given, so a part whose metal is whole takes the same
    path as one whose metal is not - and there is no branch to get wrong."""
    _rank(tmp_path / "processor0", regions=[0, 0, 0, 7, 7, 7, 7])
    report = P.write_island_sets(tmp_path)
    written = tmp_path / "processor0" / "constant" / "polyMesh" / "sets" / P.ISLAND_SET
    assert written.is_file()
    assert P.read_labels(written) == []
    assert report["severed_cells"] == 0


def test_the_island_set_holds_the_severed_cells(tmp_path):
    _rank(tmp_path / "processor0", regions=[0, 0, 0, 4, 9, 9, 9])
    P.write_island_sets(tmp_path)
    written = tmp_path / "processor0" / "constant" / "polyMesh" / "sets" / P.ISLAND_SET
    assert P.read_labels(written) == [3]


def test_a_case_that_never_had_the_regions_detected_says_so(tmp_path):
    """The detect pass is what writes `cellToRegion`, so its absence is a chain-order mistake and
    worth naming rather than reading as "no islands"."""
    pm = tmp_path / "processor0" / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "cellZones").write_text(CELLZONES)
    with pytest.raises(P.PinchError) as got:
        P.severed_cells(tmp_path)
    assert "cellToRegion" in str(got.value) and "detectOnly" in str(got.value)


# ------------------------------------------------------------------ the dictionary

def _actions(text):
    """Each action as (name, action, source, set), in the order the dictionary lists them."""
    import re
    out = []
    for block in re.findall(r"\{([^{}]*)\}", text):
        got = {}
        for line in block.splitlines():
            bits = line.strip().rstrip(";").split()
            if len(bits) == 2:
                got[bits[0]] = bits[1]
        if "name" in got:
            out.append((got.get("name"), got.get("action"), got.get("source"), got.get("set")))
    return out


def test_the_pinches_are_the_points_the_main_body_also_touches():
    """THE ONE STEP THE WHOLE REPAIR TURNS ON, and the reason it is exact rather than every cell
    round every island. Measured on the 2-across case: `pointSet pinchPoints now size 10804` for
    the islands' own points, then `now size 144` after this subset, then `bridgeCells now size
    576` and `fluidCells now size 2425118` - the same 296 cells a hand-written prototype picked by
    parsing the mesh in Python, found here without Python seeing a single face.

    `action subset` on a pointSet with a `cellToPoint` source is not documented anywhere I could
    find. It is measured, so it is asserted.
    """
    acts = _actions(P.render_pinch_toposetdict())
    assert ("pinchPoints", "new", "cellToPoint", P.ISLAND_SET) in acts
    assert ("pinchPoints", "subset", "cellToPoint", "mainSolid") in acts
    assert ("bridgeCells", "new", "pointToCell", "pinchPoints") in acts
    assert (P.FLUID_CELLS, "subtract", "cellToCell", "bridgeCells") in acts


def test_the_main_body_is_the_solid_minus_the_islands():
    acts = _actions(P.render_pinch_toposetdict())
    assert ("mainSolid", "new", "cellToCell", P.SOLID_CELLS) in acts
    assert ("mainSolid", "subtract", "cellToCell", P.ISLAND_SET) in acts


def test_both_zones_are_rebuilt_and_neither_is_left_implicit():
    """For the reason `render_toposetdict` builds both: an unzoned remainder leaves
    `splitMeshRegions` to work out what those cells are, and what it works out is one region per
    connected lump of metal - which is the very thing this repair is for."""
    acts = _actions(P.render_pinch_toposetdict(fluid_zone="fluid", solid_zone="solid"))
    assert ("fluid", "new", "setToCellZone", P.FLUID_CELLS) in acts
    assert ("solid", "new", "setToCellZone", P.SOLID_CELLS) in acts


def test_the_zone_names_come_from_the_caller():
    text = P.render_pinch_toposetdict(fluid_zone="coolant", solid_zone="metal")
    acts = _actions(text)
    assert ("coolant", "new", "setToCellZone", P.FLUID_CELLS) in acts
    assert ("metal", "new", "setToCellZone", P.SOLID_CELLS) in acts


def test_the_dictionary_is_one_the_case_guard_accepts():
    """`sandbox/foam_case_guard` refuses an active preprocessor directive, and refuses again inside
    `foam_exec.check_mesh` - so a dictionary it dislikes throws a finished mesh away with every
    stage behind it green."""
    from meshpipeline.sandbox import foam_case_guard as G

    assert not G._HASH_DIRECTIVE.search(P.render_pinch_toposetdict())


def test_writing_the_sets_does_not_write_the_dictionary(tmp_path):
    """THE ORDER THE GUARD NEEDS. `build` scans `system/` before the first stage and says why above
    the layer dicts: a dictionary written after that scan is one the guard never reads, and the
    next reader is `check_mesh` with the whole mesh already built. `pinchBridge` runs mid-chain, so
    anything IT writes is late - which is why the dictionary is `build`'s to write and this writes
    only the cellSet."""
    _rank(tmp_path / "processor0", regions=[0, 0, 0, 4, 9, 9, 9])
    P.write_island_sets(tmp_path)
    assert not (tmp_path / "system" / P.PINCH_DICT).exists()


# ------------------------------------------------------------------ the chain

def test_the_repair_runs_before_the_split_and_after_the_zones():
    """AFTER THE SPLIT IS TOO LATE: the layer pass runs on the split mesh, so re-splitting would
    throw its layers away. BEFORE the zones is too early: there is nothing to detect yet."""
    names = [n for n, _ in C._pinch_chain(8)]
    assert names == ["pinchDetect", "pinchBridge", "pinchZones"]


def test_the_detect_pass_walks_connectivity_and_builds_nothing():
    """`-cellZones`, NOT `-cellZonesOnly`: the first walks face connectivity and then matches
    zones, which is the per-zone component count this needs. `-detectOnly` writes `cellToRegion`
    without building a mesh per region - 9.1 s on the 7.3 M-cell case against its 25 s split, so
    the warning on SPLIT_ARGS about `-cellZones` being slow is about the mesh building, not the
    walk."""
    detect = dict(C._pinch_chain(8))["pinchDetect"]
    assert "-cellZones " in detect + " " and "-cellZonesOnly" not in detect
    assert "-detectOnly" in detect
    assert "splitMeshRegions" in detect


def test_the_bridge_step_is_not_run_under_mpi():
    """It reads EVERY rank's `cellToRegion` in one process, because the majority region has to be
    decided across all of them at once - see
    `test_the_majority_is_decided_across_every_rank_at_once`."""
    bridge = dict(C._pinch_chain(8))["pinchBridge"]
    assert "mpirun" not in bridge
    assert bridge.startswith("python3 -m meshpipeline.engines.cfmesh_conjugate.pinch")


def test_a_serial_build_keeps_the_chain_it_had():
    """Every measurement of this is on a decomposed case and `cellToRegion` is read per processor
    directory, so a serial build is left alone - as the layer pass is."""
    assert C._pinch_chain(1) == ()
    assert C._pinch_chain(0) == ()
