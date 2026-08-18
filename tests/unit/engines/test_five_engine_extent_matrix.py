# Responsibility: Verify every compatible engine receives the same geometry at the same physical size, per format.
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPControl")

from tests._geometry_support import materialized  # noqa: E402
from tests.cad_fixtures import write_iges, write_step, write_step_of_units  # noqa: E402
from tests.unit.cad.test_surface_family_metres import _cube_stl, _extent  # noqa: E402

from meshpipeline.cad.staging import prepare_surface  # noqa: E402
from meshpipeline.contracts.geometry_units import LengthUnit  # noqa: E402

EDGE = 10.0

#: unit -> what a 10-coordinate-unit object measures in metres. The four required invariants.
METRES = {
    LengthUnit.metre: 10.0,
    LengthUnit.millimetre: 0.01,
    LengthUnit.centimetre: 0.1,
    LengthUnit.inch: 0.254,
}

SURFACE_ENGINES = ("cfmesh", "snappy")
ALL_ENGINES = ("cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk")


# fixtures

def _stl_source(tmp_path, unit):
    geom = materialized(tmp_path / f"stl-{unit.value}", unit=unit, filename="body.stl")
    _cube_stl(Path(geom.local_path), EDGE)
    return geom


def _cad_source(tmp_path, unit, declared, *, iges=False):
    name = "part.igs" if iges else "part.step"
    geom = materialized(tmp_path / f"cad-{declared}{'i' if iges else ''}", unit=unit,
                        filename=name)
    (write_iges if iges else write_step_of_units)(geom.local_path, declared)
    return geom


def _vtp_source(tmp_path, unit):
    import numpy as np
    import pyvista as pv

    geom = materialized(tmp_path / f"vtp-{unit.value}", unit=unit, filename="lumen.vtp")
    mesh = pv.Cylinder(radius=EDGE / 4, height=EDGE, direction=(1, 0, 0),
                       resolution=16).triangulate().extract_surface()
    mesh.point_data["Radius"] = np.zeros(mesh.n_points)
    mesh.save(str(geom.local_path))
    return geom


# per-engine boundary

def _cfmesh_native(tmp_path, geom, monkeypatch) -> float:
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws, angle, bashrc: "geom.fms")
    ws = tmp_path / "cf"
    surface = prepare_surface(geom, ws / "input.stl", engine="cfmesh")
    out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                           wall_patch="body", contract_patches=[{"name": "body", "type": "wall"}],
                           args={}, cell_budget=8_000_000)
    assert out.get("success"), out
    return max(_extent(surface.path))


def _snappy_native(tmp_path, geom) -> float:
    from meshpipeline.engines.snappy import snappy_runner as R
    ws = tmp_path / "sn"
    surface = prepare_surface(geom, ws / "input.stl", engine="snappy")
    span = max(_extent(surface.path))
    prep = R.prepare_surface(ws, geometry_file="input.stl", domain_min=[-span] * 3,
                             domain_max=[span * 2] * 3, wall_patch="body",
                             farfield_patch="farfield")
    return max(_extent(ws / prep["surface_file"]))


def _gmsh_native(tmp_path, geom) -> float:
    import gmsh
    ws = tmp_path / "gm"
    prepare_surface(geom, ws / "input.stl", engine="gmsh")
    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("m")
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        gmsh.model.remove()
        return max(x1 - x0, y1 - y0, z1 - z0)
    finally:
        if mine:
            gmsh.finalize()


def _vmtk_native(tmp_path, geom) -> float:
    import pyvista as pv

    from meshpipeline.engines.vmtk import vmtk_runner as R
    ws = tmp_path / "vm"
    ws.mkdir(parents=True, exist_ok=True)
    R.tessellate_to_stl(Path(geom.path), ws / "input.stl", prepared=geom.prepared)
    b = pv.read(str(ws / "lumen.vtp")).bounds
    return b[1] - b[0]


# the matrix

@pytest.mark.parametrize("unit", list(METRES))
@pytest.mark.parametrize("engine", SURFACE_ENGINES)
def test_stl_reaches_the_surface_engines_at_its_physical_size(engine, unit, tmp_path,
                                                              monkeypatch):
    expected = METRES[unit]
    geom = _stl_source(tmp_path, unit)
    got = (_cfmesh_native(tmp_path, geom, monkeypatch) if engine == "cfmesh"
           else _snappy_native(tmp_path, geom))
    assert got == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("declared,unit", [("MM", LengthUnit.millimetre),
                                           ("CM", LengthUnit.centimetre),
                                           ("INCH", LengthUnit.inch),
                                           ("M", LengthUnit.metre)])
@pytest.mark.parametrize("engine", ("cfmesh", "snappy", "gmsh"))
def test_step_reaches_every_cad_capable_engine_at_its_physical_size(engine, declared, unit,
                                                                    tmp_path, monkeypatch):
    expected = METRES[unit]
    geom = _cad_source(tmp_path, unit, declared)
    got = {"cfmesh": lambda: _cfmesh_native(tmp_path, geom, monkeypatch),
           "snappy": lambda: _snappy_native(tmp_path, geom),
           "gmsh": lambda: _gmsh_native(tmp_path, geom)}[engine]()
    assert got == pytest.approx(expected, rel=1e-4)


@pytest.mark.parametrize("engine", ("cfmesh", "snappy", "gmsh"))
def test_iges_reaches_every_cad_capable_engine_at_its_physical_size(engine, tmp_path,
                                                                    monkeypatch):
    geom = _cad_source(tmp_path, LengthUnit.millimetre, "MM", iges=True)
    got = {"cfmesh": lambda: _cfmesh_native(tmp_path, geom, monkeypatch),
           "snappy": lambda: _snappy_native(tmp_path, geom),
           "gmsh": lambda: _gmsh_native(tmp_path, geom)}[engine]()
    assert got == pytest.approx(METRES[LengthUnit.millimetre], rel=1e-4)


@pytest.mark.parametrize("unit", list(METRES))
def test_vtp_reaches_vmtk_at_its_physical_size(unit, tmp_path):
    geom = _vtp_source(tmp_path, unit)
    assert _vmtk_native(tmp_path, geom) == pytest.approx(METRES[unit], rel=1e-6)


@pytest.mark.parametrize("unit", list(METRES))
def test_the_multi_solid_assembly_reaches_multiregion_at_its_physical_size(unit, tmp_path):
    from tests.unit.engines.test_multiregion_assembly_metres import (
        _DECLARED,
        _assembly_step,
        _prepared,
    )

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as MR

    step = _assembly_step(tmp_path / f"asm-{unit.value}.step", _DECLARED[unit])
    solids = MR.read_assembly_solids(step, tmp_path / f"o-{unit.value}", prepared=_prepared(unit))
    span = max(s["bbox_max"][0] for s in solids) - min(s["bbox_min"][0] for s in solids)
    assert span == pytest.approx(2 * METRES[unit], rel=1e-4)


# cross-engine agreement

def test_every_compatible_engine_sees_the_same_physical_size(tmp_path, monkeypatch):
    geom = _cad_source(tmp_path, LengthUnit.millimetre, "MM")
    seen = {
        "cfmesh": _cfmesh_native(tmp_path, geom, monkeypatch),
        "snappy": _snappy_native(tmp_path, geom),
        "gmsh": _gmsh_native(tmp_path, geom),
    }
    for engine, span in seen.items():
        assert span == pytest.approx(0.01, rel=1e-4), f"{engine} saw {span}, not 0.01 m"
    assert max(seen.values()) - min(seen.values()) < 1e-6, seen


def test_the_four_required_numeric_invariants(tmp_path, monkeypatch):
    for unit, expected in METRES.items():
        geom = _stl_source(tmp_path / unit.value, unit)
        surface = prepare_surface(geom, tmp_path / unit.value / "ws" / "input.stl")
        assert max(_extent(surface.path)) == pytest.approx(expected, rel=1e-6), unit


# publish the matrix

def test_emit_the_measured_matrix(tmp_path, monkeypatch, request):
    rows = []
    for unit in METRES:
        geom = _stl_source(tmp_path / f"s{unit.value}", unit)
        rows.append({"format": "STL", "unit": unit.value, "basis": "user_confirmed",
                     "factor": geom.interpretation.scale_to_metres,
                     "cfmesh": _cfmesh_native(tmp_path / f"s{unit.value}", geom, monkeypatch),
                     "snappy": _snappy_native(tmp_path / f"s{unit.value}", geom),
                     "expected": METRES[unit]})
    for declared, unit in (("MM", LengthUnit.millimetre), ("CM", LengthUnit.centimetre),
                           ("INCH", LengthUnit.inch), ("M", LengthUnit.metre)):
        geom = _cad_source(tmp_path / f"c{declared}", unit, declared)
        rows.append({"format": f"STEP {declared}", "unit": unit.value,
                     "basis": geom.interpretation.basis,
                     "factor": geom.interpretation.scale_to_metres,
                     "cfmesh": _cfmesh_native(tmp_path / f"c{declared}", geom, monkeypatch),
                     "snappy": _snappy_native(tmp_path / f"c{declared}", geom),
                     "gmsh": _gmsh_native(tmp_path / f"c{declared}", geom),
                     "expected": METRES[unit]})
    for unit in METRES:
        geom = _vtp_source(tmp_path / f"v{unit.value}", unit)
        rows.append({"format": "VTP", "unit": unit.value, "basis": "user_confirmed",
                     "factor": geom.interpretation.scale_to_metres,
                     "vmtk": _vmtk_native(tmp_path / f"v{unit.value}", geom),
                     "expected": METRES[unit]})

    dest = Path(os.environ.get("MATRIX_OUT", tmp_path / "matrix.json"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows, indent=2, default=str))
    for row in rows:
        for engine in ALL_ENGINES:
            if engine in row:
                assert row[engine] == pytest.approx(row["expected"], rel=1e-4), row


# explained blanks

def test_gmsh_refuses_a_bare_surface_and_says_so(tmp_path):
    from meshpipeline.engines.gmsh import gmsh_runner as R

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "input.stl").write_text("solid s\nendsolid s\n")

    out = R.inspect_stl(ws)
    assert "geometry.step missing" in out.get("error", "")
    assert "CAD solid" in out["error"], "the refusal must tell the user what to supply"


def test_multiregion_needs_more_than_one_solid(tmp_path):
    from tests.unit.engines.test_multiregion_assembly_metres import _prepared

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as MR

    single = write_step(tmp_path / "one.step", "MM")
    solids = MR.read_assembly_solids(single, tmp_path / "out",
                                     prepared=_prepared(LengthUnit.millimetre))

    assert len(solids) == 1, "a single-solid file yields one region, so there is nothing to join"


def test_vmtk_can_also_consume_cad_and_stl(tmp_path):
    from meshpipeline.engines.vmtk import vmtk_runner as R

    geom = _cad_source(tmp_path, LengthUnit.millimetre, "MM")
    ws = tmp_path / "ws"
    ws.mkdir()
    R.tessellate_to_stl(Path(geom.path), ws / "input.stl", prepared=geom.prepared)

    assert (ws / "lumen.vtp").exists(), "vmtk staged no lumen from CAD"
    assert max(_extent(ws / "input.stl")) == pytest.approx(METRES[LengthUnit.millimetre], rel=1e-4)
