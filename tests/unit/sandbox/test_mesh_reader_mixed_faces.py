# Responsibility: Prove the review renderer's mesh reader draws surfaces that mix triangles and quads.
from __future__ import annotations

import gmsh
import pytest

from meshpipeline.sandbox import mesh_reader as MR


@pytest.fixture
def mixed_surface():
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("mixed")
        s = gmsh.model.addDiscreteEntity(2)
        # a unit square split into one quad and two triangles - a shell mesh in miniature
        coords = [0, 0, 0, 1, 0, 0, 2, 0, 0, 0, 1, 0, 1, 1, 0, 2, 1, 0]
        gmsh.model.mesh.addNodes(2, s, [1, 2, 3, 4, 5, 6], coords)
        gmsh.model.mesh.addElementsByType(s, 3, [1], [1, 2, 5, 4])           # quad
        gmsh.model.mesh.addElementsByType(s, 2, [2, 3], [2, 3, 6, 2, 6, 5])  # two triangles
        yield s
    finally:
        gmsh.finalize()


def test_a_surface_of_triangles_and_quads_is_drawn_not_refused(mixed_surface):
    meshes = MR.extract_patch_meshes({"plate": [mixed_surface]}, "mixed.msh")
    mesh = meshes["plate"]
    assert mesh.n_cells == 3
    assert sorted(int(mesh.get_cell(i).n_points) for i in range(mesh.n_cells)) == [3, 3, 4]
