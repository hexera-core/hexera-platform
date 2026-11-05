# Responsibility: Verify the review scene builds for centimetre-scale bodies, not just metre-scale ones.
from __future__ import annotations

import pytest

pytest.importorskip("gmsh")

from meshpipeline.sandbox.mesh_reader import read_mesh  # noqa: E402


def _tiny_msh(path, edge_m: float):
    import gmsh
    owned = not gmsh.isInitialized()
    if owned:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("tiny")
        gmsh.model.occ.addBox(0, 0, 0, edge_m, edge_m, edge_m)
        gmsh.model.occ.synchronize()
        for dim, tag in gmsh.model.getEntities(2):
            g = gmsh.model.addPhysicalGroup(2, [tag])
            gmsh.model.setPhysicalName(2, g, f"face_{tag}")
        gmsh.model.mesh.generate(2)
        gmsh.write(str(path))
        gmsh.model.remove()
    finally:
        if owned:
            gmsh.finalize()


@pytest.mark.parametrize("edge_m", [0.05, 0.4, 2.0])
def test_the_pan_step_is_positive_at_every_body_scale(tmp_path, edge_m):
    # pan_step was round(longest * 0.10, 1) - one decimal, in METRES - so any body shorter than
    # half a metre floored to 0.0 and the scene contract refused to build. That is a crash for
    # most real industrial parts: brackets, manifolds and heat sinks are centimetre-scale, and
    # every geometry that ever survived it was simply a metre or larger.
    msh = tmp_path / "tiny.msh"
    _tiny_msh(msh, edge_m)
    loaded = read_mesh(str(msh), "m")
    assert loaded.pan_step > 0, (edge_m, loaded.pan_step)
    assert loaded.pan_step == pytest.approx(edge_m * 0.10, rel=0.01), (edge_m, loaded.pan_step)
