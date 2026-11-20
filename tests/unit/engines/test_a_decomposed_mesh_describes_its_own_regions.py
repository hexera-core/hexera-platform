# Responsibility: a run that never merged its mesh must still be able to say what regions and
# patches it built.
# Boundaries: ASCII polyMesh files only. No OpenFOAM, no subprocess, no mesher.
"""The readers at home find the regions without a merged mesh being made for them.

WHY THIS EXISTS. `reconstructParMesh` is 4,857 s of the cold plate's chain and writes nothing a
solver needs: it exists so that five readers can find `constant/<region>/polyMesh`, after which
the solver decomposes the mesh again. Four of those five want only a patch table - which regions
exist and what patches each has - and the fifth wants to know the cells are really on disk.

So `_publish_decomposed_boundaries` writes the patch table, summed over the ranks' REAL patches,
and `meshed_regions` looks for the cells where they actually are. Between them the five readers
answer from `processor*/` and the merge stops being load-bearing.

WHAT IS DELIBERATELY NOT DONE: no `owner`, `faces` or `points` file is synthesized. Describing a
mesh is not the same as having one, and a fake `owner` beside a real boundary would defeat the
very guard `meshed_regions` exists to provide - gates.py says the owner check is there because
splitMeshRegions makes the directory before it writes into it.
"""
from __future__ import annotations

from tests.foam_fixtures import write_decomposed_row_of_hexes

from meshpipeline.engines.cfmesh_conjugate import gates
from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    _publish_decomposed_boundaries,
    boundary_patches,
)


def _two_region_case(tmp_path, ranks=3):
    write_decomposed_row_of_hexes(tmp_path, ranks=ranks, n_cells=6, region="fluid")
    write_decomposed_row_of_hexes(tmp_path, ranks=ranks, n_cells=4, region="solid")
    return tmp_path


# -- what gets written ----------------------------------------------------------------------

def test_every_decomposed_region_gets_a_patch_table(tmp_path):
    ws = _two_region_case(tmp_path)
    assert _publish_decomposed_boundaries(ws) == ["fluid", "solid"]
    assert boundary_patches(ws / "constant" / "fluid" / "polyMesh") == {
        "inlet": 1, "outlet": 1, "wall": 24}
    assert boundary_patches(ws / "constant" / "solid" / "polyMesh") == {
        "inlet": 1, "outlet": 1, "wall": 16}


def test_it_never_writes_over_a_real_one(tmp_path):
    """While the reconstruct still runs those files are the mesher's own and are the truth."""
    ws = _two_region_case(tmp_path)
    real = ws / "constant" / "fluid" / "polyMesh"
    real.mkdir(parents=True)
    (real / "boundary").write_text("the mesher wrote this")
    assert _publish_decomposed_boundaries(ws) == ["solid"], "it rewrote a real boundary file"
    assert (real / "boundary").read_text() == "the mesher wrote this"


def test_a_reconstructed_run_is_left_entirely_alone(tmp_path):
    """The common case today. No processor dirs at all means there is nothing to describe."""
    (tmp_path / "constant" / "fluid" / "polyMesh").mkdir(parents=True)
    assert _publish_decomposed_boundaries(tmp_path) == []


def test_it_does_not_invent_the_mesh_itself(tmp_path):
    """Only the patch table. A synthesized `owner` would defeat the gate below."""
    ws = _two_region_case(tmp_path)
    _publish_decomposed_boundaries(ws)
    pm = ws / "constant" / "fluid" / "polyMesh"
    assert (pm / "boundary").is_file()
    for never in ("owner", "faces", "points", "neighbour"):
        assert not (pm / never).exists(), f"{never} was synthesized; it describes nothing real"


# -- and the gate that says the cells are really there ----------------------------------------

def test_the_gate_sees_a_region_whose_cells_are_per_rank(tmp_path):
    ws = _two_region_case(tmp_path)
    _publish_decomposed_boundaries(ws)
    assert gates.meshed_regions(ws) == ["fluid", "solid"]


def test_the_gate_still_refuses_a_region_one_rank_never_finished(tmp_path):
    """The half-written-region failure, moved down a level rather than given up.

    A parallel splitMeshRegions writes its ranks independently, so "some rank has cells" is
    exactly the state that must not pass. Every rank must hold the region's owner file.
    """
    ws = _two_region_case(tmp_path)
    _publish_decomposed_boundaries(ws)
    (ws / "processor1" / "constant" / "fluid" / "polyMesh" / "owner").unlink()
    assert gates.meshed_regions(ws) == ["solid"], (
        "a region that one rank never finished writing was reported as meshed")


def test_the_gate_is_unchanged_on_a_merged_mesh(tmp_path):
    """A reconstructed region passes on its own owner file, with no processor dirs in sight."""
    pm = tmp_path / "constant" / "fluid" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("0\n(\n)\n")
    (tmp_path / "constant" / "solid" / "polyMesh").mkdir(parents=True)
    assert gates.meshed_regions(tmp_path) == ["fluid"], (
        "a region directory with no owner file must still be refused")
