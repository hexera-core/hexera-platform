"""The face counts a reader is shown come off the mesh that was delivered.

MEASURED, on the delivered coupon (job 18e2f4ed, a two-region cfmesh_conjugate build), 2026-10-29.
`_patch_face_counts` read `constant/polyMesh/boundary` - the PRE-SPLIT BLOCK - and the parts panel
in the viewer shows what it returns:

    block      xmin 1,928  xmax 1,928  ymin 776  ymax 776  zmin 7,504  zmax 17,426
    delivered  the same, except zmax 14,226 - plus inlet 1,600, outlet 1,600, and the interface
               at 346,456 faces on each side

Two things happen to the block after the mesher and before the customer gets it: splitMeshRegions
divides its faces between the regions, and the port cut carves inlet and outlet out of a block
face, on the coolant alone. So zmax was shown 3,200 faces too big - exactly the two ports - and
the ports and the interface had no count at all. The interface is the one patch a conjugate mesh
is for.

AND AT THE SIZES THAT NEED RANKS THERE IS NO BLOCK. The parallel chain stays decomposed through
the split and reconstructs the two REGIONS, never the block, so `constant/polyMesh` does not exist
on a big conjugate build and this returned {} - every count in the panel blank.
"""
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.manifest import _patch_face_counts

PATCH = """    {name}
    {{
        type            wall;
        nFaces          {count};
        startFace       0;
    }}"""

HEADER = ("FoamFile {{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }}\n\n"
          "{n}\n(\n{rows}\n)\n")


def _boundary(polymesh: Path, patches: dict) -> None:
    """A polyMesh/boundary in the shape the reader parses: name { ... nFaces N; ... }."""
    polymesh.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(PATCH.format(name=n, count=c) for n, c in patches.items())
    (polymesh / "boundary").write_text(HEADER.format(n=len(patches), rows=rows))


#: What the coupon actually delivered, and what its block still said.
BLOCK = {"xmin": 1928, "xmax": 1928, "ymin": 776, "ymax": 776, "zmin": 7504, "zmax": 17426}
FLUID = {"fluid_to_solid": 346456, "inlet": 1600, "outlet": 1600}
SOLID = {"xmin": 1928, "xmax": 1928, "ymin": 776, "ymax": 776, "zmin": 7504, "zmax": 14226,
         "solid_to_fluid": 346456}


def _conjugate(tmp_path: Path, *, with_block: bool = True) -> Path:
    if with_block:
        _boundary(tmp_path / "constant" / "polyMesh", BLOCK)
    _boundary(tmp_path / "constant" / "fluid" / "polyMesh", FLUID)
    _boundary(tmp_path / "constant" / "solid" / "polyMesh", SOLID)
    return tmp_path


def test_the_port_cut_is_counted_and_the_block_is_not(tmp_path):
    """zmax 17,426 on the block against 14,226 delivered, and the 3,200 faces between them are
    the two ports - which the block does not have at all."""
    got = _patch_face_counts(_conjugate(tmp_path))
    assert got["zmax"] == 14226, "the block's count is the mesh before the ports were cut"
    assert got["inlet"] == 1600 and got["outlet"] == 1600
    assert got["zmax"] + got["inlet"] + got["outlet"] == BLOCK["zmax"], (
        "the ports came out of zmax, so the three of them are the block's face")


def test_the_interface_has_a_count_at_all(tmp_path):
    """346,456 faces on each side, and before this neither side was in the roster."""
    got = _patch_face_counts(_conjugate(tmp_path))
    assert got["fluid_to_solid"] == 346456
    assert got["solid_to_fluid"] == 346456


def test_the_untouched_block_faces_are_unchanged(tmp_path):
    """The split gave all of xmin..zmin to the metal, so those five are the same either way -
    which is why only one number looked wrong and four were missing."""
    got = _patch_face_counts(_conjugate(tmp_path))
    for n in ("xmin", "xmax", "ymin", "ymax", "zmin"):
        assert got[n] == BLOCK[n] == SOLID[n]


def test_a_parallel_build_has_no_block_and_still_has_counts(tmp_path):
    """The chain that the customer-sized builds take: decomposed through the split, the two
    regions merged and the block never reconstructed. There is no constant/polyMesh to read."""
    ws = _conjugate(tmp_path, with_block=False)
    assert not (ws / "constant" / "polyMesh").exists()
    got = _patch_face_counts(ws)
    assert got["zmax"] == 14226 and got["inlet"] == 1600
    # ten, not the nine the viewer payload carries: the interface is ONE drawn surface there
    # (fluid/fluid_to_solid) and two boundary entries here, one in each region's own file
    assert len(got) == 10, got
    assert got["fluid_to_solid"] == got["solid_to_fluid"] == 346456


def test_two_regions_reaching_the_same_face_are_summed(tmp_path):
    """The manifest is keyed by the BARE name - the one the user asked for at intake - and both
    regions can legitimately reach the same block face. mesh_facts says so where it maps a
    declared patch to the surfaces that draw it."""
    _boundary(tmp_path / "constant" / "fluid" / "polyMesh", {"ymin": 100, "inlet": 7})
    _boundary(tmp_path / "constant" / "solid" / "polyMesh", {"ymin": 250})
    got = _patch_face_counts(tmp_path)
    assert got == {"ymin": 350, "inlet": 7}


def test_a_single_region_mesh_reads_the_one_boundary_it_always_did(tmp_path):
    """Every engine that is not multi-region, unchanged: no region directories, the root block IS
    the delivery."""
    _boundary(tmp_path / "constant" / "polyMesh", {"inlet": 12, "outlet": 12, "walls": 400})
    assert _patch_face_counts(tmp_path) == {"inlet": 12, "outlet": 12, "walls": 400}


def test_nothing_at_all_is_an_empty_roster_and_not_a_crash(tmp_path):
    assert _patch_face_counts(tmp_path) == {}


def test_a_region_directory_without_a_boundary_is_not_a_region(tmp_path):
    """constant/ holds more than meshes - regionProperties, triSurface, the solver's own
    directories. A directory is a region when it has a polyMesh/boundary and not before."""
    (tmp_path / "constant" / "triSurface").mkdir(parents=True)
    _boundary(tmp_path / "constant" / "polyMesh", {"walls": 9})
    assert _patch_face_counts(tmp_path) == {"walls": 9}
