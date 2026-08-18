# Responsibility: Verify Gmsh imports normalise to metres once, and the staged BRep and preview agree.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("gmsh")
pytest.importorskip("OCP.STEPControl")

from tests._geometry_support import materialized  # noqa: E402
from tests.cad_fixtures import SIDE, write_iges, write_step, write_step_of_units  # noqa: E402
from tests.unit.cad.test_surface_family_metres import _extent  # noqa: E402

from meshpipeline.cad.staging import prepare_surface  # noqa: E402
from meshpipeline.contracts.geometry_units import LengthUnit  # noqa: E402

CASES = [
    ("MM", LengthUnit.millimetre, SIDE * 1e-3),
    ("CM", LengthUnit.centimetre, SIDE * 1e-2),
    ("INCH", LengthUnit.inch, SIDE * 0.0254),
    ("M", LengthUnit.metre, SIDE * 1.0),
]


def _gmsh_bbox(path) -> list[float]:
    import gmsh

    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("probe")
        gmsh.model.occ.importShapes(str(path))
        gmsh.model.occ.synchronize()
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        out = [x1 - x0, y1 - y0, z1 - z0]
        gmsh.model.remove()
        return out
    finally:
        if mine:
            gmsh.finalize()


def _gmsh_entities(path) -> tuple[int, int]:
    import gmsh

    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("probe")
        gmsh.model.occ.importShapes(str(path))
        gmsh.model.occ.synchronize()
        out = (len(gmsh.model.getEntities(3)), len(gmsh.model.getEntities(2)))
        gmsh.model.remove()
        return out
    finally:
        if mine:
            gmsh.finalize()


def _source(tmp_path, declared, unit, *, iges=False):
    name = "part.igs" if iges else "part.step"
    geom = materialized(tmp_path / f"src-{declared}{'i' if iges else ''}", unit=unit,
                        filename=name)
    (write_iges if iges else write_step_of_units)(geom.local_path, declared)
    return geom


# measured import semantics

@pytest.mark.parametrize("declared,unit,_expected", CASES)
def test_gmsh_import_normalises_to_the_occ_system_unit(declared, unit, _expected, tmp_path):
    step = write_step(tmp_path / f"{declared}.step", declared)   # all the same physical box

    assert max(_gmsh_bbox(step)) == pytest.approx(SIDE, rel=1e-6), (
        f"gmsh imported the shared 10 mm box declared {declared} at a different size; the "
        "bundle's single conversion depends on that normalisation")


# the staged B-rep is metres

@pytest.mark.parametrize("declared,unit,expected", CASES)
def test_the_staged_brep_gmsh_meshes_is_in_metres(declared, unit, expected, tmp_path):
    ws = tmp_path / f"ws-{declared}"
    prepare_surface(_source(tmp_path, declared, unit), ws / "input.stl", engine="gmsh")

    staged = ws / "geometry.step"
    assert staged.exists(), "gmsh staged no B-rep for its driver"
    # 1e-4 absorbs TWO STEP text round-trips (the fixture's own write, then gmsh's write of the
    # scaled model). The smallest unit ratio in the vocabulary is 25.4x, so a real scaling error
    # misses this by five orders of magnitude.
    assert max(_gmsh_bbox(staged)) == pytest.approx(expected, rel=1e-4), (
        f"a {SIDE}-unit {declared} solid must reach gmsh as {expected} m")


@pytest.mark.parametrize("declared,unit,expected", CASES)
def test_the_preview_surface_agrees_with_the_brep(declared, unit, expected, tmp_path):
    ws = tmp_path / f"ws-{declared}"
    surface = prepare_surface(_source(tmp_path, declared, unit), ws / "input.stl", engine="gmsh")

    brep_extent = max(_gmsh_bbox(ws / "geometry.step"))
    preview_extent = max(_extent(surface.path))
    assert preview_extent == pytest.approx(brep_extent, rel=1e-3)
    assert preview_extent == pytest.approx(expected, rel=1e-3)


def test_scaling_preserves_entity_topology(tmp_path):
    ws = tmp_path / "ws"
    src = _source(tmp_path, "INCH", LengthUnit.inch)
    before = _gmsh_entities(Path(src.local_path))

    prepare_surface(src, ws / "input.stl", engine="gmsh")

    assert _gmsh_entities(ws / "geometry.step") == before == (1, 6)


def test_iges_reaches_gmsh_in_metres(tmp_path):
    ws = tmp_path / "ws-igs"
    prepare_surface(_source(tmp_path, "MM", LengthUnit.millimetre, iges=True),
                    ws / "input.stl", engine="gmsh")

    assert max(_gmsh_bbox(ws / "geometry.step")) == pytest.approx(SIDE * 1e-3, rel=1e-4)


def test_the_brep_is_not_scaled_twice(tmp_path):
    ws = tmp_path / "ws"
    prepare_surface(_source(tmp_path, "INCH", LengthUnit.inch), ws / "input.stl", engine="gmsh")

    got = max(_gmsh_bbox(ws / "geometry.step"))
    expected = SIDE * 0.0254
    assert got == pytest.approx(expected, rel=1e-4)
    assert got > expected * 0.5, f"{got} vs {expected}: the factor looks applied twice"


def test_a_mesh_generated_from_the_staged_brep_has_metre_node_bounds(tmp_path):
    import gmsh

    ws = tmp_path / "ws"
    prepare_surface(_source(tmp_path, "MM", LengthUnit.millimetre), ws / "input.stl",
                    engine="gmsh")

    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("mesh")
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", SIDE * 1e-3 / 4)
        gmsh.model.mesh.generate(3)
        _tags, coords, _p = gmsh.model.mesh.getNodes()
        gmsh.model.remove()
    finally:
        gmsh.finalize()

    xs = coords[0::3]
    assert xs.size > 0, "gmsh generated no nodes"
    assert (max(xs) - min(xs)) == pytest.approx(SIDE * 1e-3, rel=1e-3)


def test_the_bundle_refuses_to_stage_without_typed_coordinate_state(tmp_path):
    from meshpipeline.engines.gmsh import gmsh_runner as R

    src = write_step(tmp_path / "x.step", "MM")
    with pytest.raises(ValueError, match="coordinate state"):
        R.tessellate_to_stl(src, tmp_path / "ws" / "input.stl", prepared=None)
