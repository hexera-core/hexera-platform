# Responsibility: Verify cfMesh hands the B-rep internal staging the same reading of the CAD's solid as snappyHexMesh (a body is False, not None), so the declared-body rules read one part one way whichever engine meshes it.
# Boundaries: the value _configure_internal passes to tessellate_internal; the staging itself is stood in for.
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh import cfmesh_runner as R
from meshpipeline.engines.workspace_facts import fluid_solid_of


class _Stop(Exception):
    pass


@pytest.mark.parametrize("kind,want", [("fluid-domain", True), ("solid-body", False), ("body-surface", False),
                                       ("", False)])
def test_cfmesh_reads_the_solid_as_snappy_does(tmp_path, monkeypatch, kind, want):
    import meshpipeline.cad.prepared_surface as PS
    (tmp_path / "geometry.step").write_text("ISO-10303-21;")
    if kind:
        (tmp_path / "input_kind").write_text(kind)
    seen = {}

    class _Staged:
        path = tmp_path / "absent.stl"
        consumed = object()

    monkeypatch.setattr(PS, "require_metre_surface", lambda surface, ws, name: _Staged())

    def _fake(solid, out, **kw):
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(R, "tessellate_internal", _fake)
    with pytest.raises(_Stop):
        R._configure_internal(tmp_path, strategy={}, wall_patch="wall", contract_patches=[], args={},
                              cell_budget=1_000_000)
    assert seen["fluid_solid"] is want
    assert fluid_solid_of(kind) is want


def test_no_engine_leaves_a_body_undeclared():
    # snappy's driver, cfMesh's runner and the surface staging read the solid through the one function
    import inspect

    from meshpipeline.engines.snappy import drivers
    assert "fluid_solid=fluid_solid_of(kind)" in inspect.getsource(drivers)
    assert "fluid_solid=fluid_solid_of(_kind(workspace))" in inspect.getsource(R)
