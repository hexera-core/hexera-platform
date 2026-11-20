# Responsibility: the patch table read from a decomposed mesh must equal the one read from the
# same mesh reconstructed.
# Boundaries: ASCII polyMesh boundary files only. No OpenFOAM, no subprocess.
"""Summing a decomposed mesh's real patches reproduces the reconstructed mesh exactly.

WHY THIS EXISTS. `reconstructParMesh` is 4,857 s of the cold plate's chain and produces nothing
a solver needs - it writes the mesh out whole so that five readers at home can find
`constant/<region>/polyMesh`, and the solver then decomposes it again. Dropping it means those
readers answer from `processor*/` instead, and the thing that has to be true for that to be safe
is the identity this file pins: the real patches, summed across ranks, ARE the reconstructed
mesh's patches.

WHY IT IS NOT JUST A SUM. Every face on a processor boundary was ONE internal face of the whole
mesh and is a BOUNDARY face on each of the two ranks that share it. Add up every patch a rank
lists and the answer is too big by twice the cut; add up only the patches whose `type` is not
`processor` and it is exact. `type` is the only thing that distinguishes them - the names are
`procBoundaryM_toN` today and nothing may depend on that spelling.

THE REAL-CASE ARM IS THE ONE THAT MATTERS. `runs/unit_cell_100um-4across` is a genuine four-rank
`decomposePar` + `splitMeshRegions` case that carries BOTH the per-rank meshes and the
reconstructed ones, so the two can be compared without anybody deciding what the answer should
be. It is skipped when the directory is absent so the suite stays hermetic, and the hand-built
fixture below covers the same ground where it is.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from foam_fixtures import write_decomposed_row_of_hexes
from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import (
    DecomposedBoundaryError,
    decomposed_region_boundary,
    processor_dirs,
    read_boundary_patches,
)

REAL = Path(__file__).resolve().parents[3] / "runs" / "unit_cell_100um-4across"
needs_real = pytest.mark.skipif(not (REAL / "processor0").is_dir(),
                                reason="the real 4-rank case is not on this machine")


def _counts(rows) -> dict:
    return {r["name"]: r["n_faces"] for r in rows}


# -- the identity, on real mesher output ----------------------------------------------------

@needs_real
@pytest.mark.parametrize("region", ["fluid", "solid"])
def test_the_decomposed_sum_is_the_reconstructed_patch_table(region):
    """The acceptance test for dropping reconstructParMesh, on a mesh nobody hand-wrote."""
    decomposed = _counts(decomposed_region_boundary(REAL, region))
    reconstructed = _counts(read_boundary_patches(REAL / "constant" / region / "polyMesh"))
    assert decomposed == reconstructed, (
        f"{region}: reading the mesh decomposed gives a different patch table than reading the "
        f"same mesh reconstructed\n  decomposed:    {decomposed}\n  reconstructed: {reconstructed}")


@needs_real
def test_the_conjugate_interface_matches_face_for_face_on_both_sides():
    """Conformality is this engine's whole claim, so it is asserted rather than implied."""
    fluid = _counts(decomposed_region_boundary(REAL, "fluid"))
    solid = _counts(decomposed_region_boundary(REAL, "solid"))
    assert fluid["fluid_to_solid"] == solid["solid_to_fluid"], (
        f"the interface is {fluid['fluid_to_solid']} faces on the fluid side and "
        f"{solid['solid_to_fluid']} on the solid side when read decomposed")


@needs_real
def test_no_processor_patch_reaches_the_table():
    """A processor patch in the delivered table is a patch the customer never asked for."""
    for region in ("fluid", "solid"):
        rows = decomposed_region_boundary(REAL, region)
        assert not [r for r in rows if r["type"] == "processor"], rows
        assert not [r for r in rows if r["name"].startswith("procBoundary")], rows


# -- the same ground, hermetically ----------------------------------------------------------

def test_a_hand_built_decomposed_case_sums_to_its_own_serial_counts(tmp_path):
    write_decomposed_row_of_hexes(tmp_path, ranks=3, n_cells=6, region="fluid")
    assert _counts(decomposed_region_boundary(tmp_path, "fluid")) == {
        "inlet": 1, "outlet": 1, "wall": 24}


def test_a_patch_listed_at_zero_faces_on_some_ranks_still_appears(tmp_path):
    """Every rank lists every global patch, most of them empty. Absence of faces is not absence.

    `inlet` has its one face on rank 0 only; ranks 1 and 2 carry it at nFaces 0. A reader that
    dropped empty rows would lose a patch the moment a run decomposed it onto one rank.
    """
    write_decomposed_row_of_hexes(tmp_path, ranks=3, n_cells=6, region="fluid")
    names = [r["name"] for r in decomposed_region_boundary(tmp_path, "fluid")]
    assert names == ["inlet", "outlet", "wall"], names


def test_the_patch_list_is_the_union_over_ranks_not_processor0s(tmp_path):
    """The hand-run script this replaces takes its order from processor0 and drops the rest.

    Here processor0's own boundary is rewritten without `outlet`, which is what a reader
    trusting one rank would then never report. The union still finds it on the rank that has it.
    """
    write_decomposed_row_of_hexes(tmp_path, ranks=3, n_cells=6, region="fluid")
    b = tmp_path / "processor0" / "constant" / "fluid" / "polyMesh" / "boundary"
    text = b.read_text()
    assert "outlet" in text
    b.write_text(text.replace("    outlet\n    {\n        type patch;\n"
                              "        nFaces 0;\n        startFace 2;\n    }\n", ""))
    names = [r["name"] for r in decomposed_region_boundary(tmp_path, "fluid")]
    assert "outlet" in names, (
        f"a patch missing from processor0 was dropped from the whole mesh: {names}")


def test_ranks_that_disagree_about_a_patch_raise_rather_than_sum(tmp_path):
    """Two types for one name means these files are not one mesh. Summing would be confident."""
    write_decomposed_row_of_hexes(tmp_path, ranks=2, n_cells=4, region="fluid")
    b = tmp_path / "processor1" / "constant" / "fluid" / "polyMesh" / "boundary"
    b.write_text(b.read_text().replace("type wall;", "type patch;", 1))
    with pytest.raises(DecomposedBoundaryError, match="not one mesh"):
        decomposed_region_boundary(tmp_path, "fluid")


def test_a_case_with_no_processor_directories_refuses(tmp_path):
    with pytest.raises(DecomposedBoundaryError, match="no processor"):
        decomposed_region_boundary(tmp_path, "fluid")


def test_ranks_are_walked_in_rank_order_not_lexical_order(tmp_path):
    """processor10 sorts before processor2 as a string, which silently reorders any rank-major
    traversal the moment a run uses ten ranks. Unreachable at today's cap of eight, and invisible
    when it stops being."""
    for n in (0, 1, 2, 10, 11):
        (tmp_path / f"processor{n}").mkdir()
    assert [d.name for d in processor_dirs(tmp_path)] == [
        "processor0", "processor1", "processor2", "processor10", "processor11"]
