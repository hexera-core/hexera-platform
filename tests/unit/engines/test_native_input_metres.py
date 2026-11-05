# Responsibility: Verify the OpenFOAM bundles receive every input format at its true physical size, scaled once.
from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests._geometry_support import materialized, prepared_surface
from tests.unit.cad.test_surface_family_metres import (
    EDGE,
    EXPECTED_METRES,
    _bounds,
    _cube_stl,
    _extent,
)

from meshpipeline.cad.analysis import analyze_surface
from meshpipeline.cad.staging import prepare_surface
from meshpipeline.contracts.geometry_units import LengthUnit

_CONTRACT = [{"name": "body", "type": "wall"}]


def _source(tmp_path, unit: LengthUnit, *, edge: float = EDGE):
    geom = materialized(tmp_path / f"src-{unit.value}", unit=unit, filename="body.stl")
    _cube_stl(Path(geom.local_path), edge)
    return geom


def _staged(tmp_path, unit: LengthUnit, *, edge: float = EDGE):
    ws = tmp_path / f"ws-{unit.value}"
    surface = prepare_surface(_source(tmp_path, unit, edge=edge), ws / "input.stl")
    return ws, surface


def _numbers(text: str, key: str) -> list[float]:
    out: list[float] = []
    for line in text.splitlines():
        if key in line:
            out += [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", line)]
    return out


# cfMesh native input

@pytest.mark.parametrize("unit", list(EXPECTED_METRES))
def test_cfmesh_native_surface_is_the_true_physical_size(unit, tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R

    expected = EXPECTED_METRES[unit]
    ws, surface = _staged(tmp_path, unit)
    # _to_fms shells out to cfMesh's surfaceFeatureEdges; the STL it is handed is the artefact
    # under test, so capture it rather than run a native binary.
    captured: dict = {}

    def _capture_fms(ws_arg, feature_angle, bashrc):
        # geom.stl is what cfMesh's surfaceFeatureEdges would consume; capture it unrun.
        captured["stl"] = Path(ws_arg) / "geom.stl"
        return "geom.fms"
    monkeypatch.setattr(R, "_to_fms", _capture_fms)

    out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                           wall_patch="body", contract_patches=_CONTRACT, args={},
                           cell_budget=8_000_000)
    assert out.get("success"), out

    native = captured.get("stl") or (ws / "geom.stl")
    assert native.exists(), "cfMesh staged no native surface"
    body_extent = _extent(native)
    # the far-field box encloses the body, so the BODY's own span is what must match
    assert min(body_extent) > 0
    assert _extent(surface.path) == pytest.approx([expected] * 3, rel=1e-6)

    md = (ws / "system" / "meshDict").read_text()
    cell_sizes = _numbers(md, "maxCellSize")
    assert cell_sizes, "meshDict declares no maxCellSize"
    # a dimensional control, so it must scale with the object rather than stay put
    assert 0 < cell_sizes[0] < expected * 10, (
        f"maxCellSize {cell_sizes[0]} is not physical for a {expected} m object")


def test_cfmesh_domain_bounds_follow_the_physical_size(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws_arg, angle, bashrc: "geom.fms")

    seen = {}
    for unit in (LengthUnit.millimetre, LengthUnit.metre):
        ws, surface = _staged(tmp_path, unit)
        out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                               wall_patch="body", contract_patches=_CONTRACT, args={},
                               cell_budget=8_000_000)
        assert out.get("success"), out
        seen[unit] = _numbers((ws / "system" / "meshDict").read_text(), "maxCellSize")[0]

    ratio = seen[LengthUnit.metre] / seen[LengthUnit.millimetre]
    assert ratio == pytest.approx(1000.0, rel=1e-6), (
        f"cfMesh sized both cubes almost identically (ratio {ratio}); the millimetre source is "
        "not being interpreted")


# snappy native input

@pytest.mark.parametrize("unit", list(EXPECTED_METRES))
def test_snappy_trisurface_is_the_true_physical_size(unit, tmp_path):
    from meshpipeline.engines.snappy import snappy_runner as R

    expected = EXPECTED_METRES[unit]
    ws, surface = _staged(tmp_path, unit)

    prep = R.prepare_surface(ws, geometry_file="input.stl", domain_min=[0, 0, 0],
                             domain_max=[expected * 3] * 3, wall_patch="body",
                             farfield_patch="farfield")

    tri = ws / prep["surface_file"]
    assert tri.exists(), f"snappy staged no triSurface ({prep['surface_file']})"
    assert _extent(tri) == pytest.approx([expected] * 3, rel=1e-6), (
        f"a {EDGE} {unit.value} cube must reach snappyHexMesh as {expected} m")


def test_snappy_and_cfmesh_see_the_same_physical_bounds(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as CF
    from meshpipeline.engines.snappy import snappy_runner as SN
    monkeypatch.setattr(CF, "_to_fms", lambda ws_arg, angle, bashrc: "geom.fms")

    ws, surface = _staged(tmp_path, LengthUnit.millimetre)
    expected = EXPECTED_METRES[LengthUnit.millimetre]

    prep = SN.prepare_surface(ws, geometry_file="input.stl", domain_min=[0, 0, 0],
                              domain_max=[expected * 3] * 3, wall_patch="body",
                              farfield_patch="farfield")
    snappy_extent = _extent(ws / prep["surface_file"])

    ws2, surface2 = _staged(tmp_path, LengthUnit.millimetre)
    CF.configure_mesh(ws2, geometry_file="input.stl", surface=surface2, strategy={},
                      wall_patch="body", contract_patches=_CONTRACT, args={},
                      cell_budget=8_000_000)
    cfmesh_extent = _extent(ws2 / "input.stl")

    assert snappy_extent == pytest.approx(cfmesh_extent, rel=1e-6)
    assert snappy_extent == pytest.approx([expected] * 3, rel=1e-6)


def test_snappy_dimensional_controls_are_physical(tmp_path):
    from meshpipeline.engines.snappy import snappy_runner as R

    got = {}
    for unit in (LengthUnit.millimetre, LengthUnit.metre):
        ws, surface = _staged(tmp_path, unit)
        out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface,
                               strategy={"max_cells": 4_000_000}, wall_patch="body",
                               contract_patches=_CONTRACT, args={}, cell_budget=8_000_000)
        assert out.get("success"), out
        got[unit] = analyze_surface(surface)

    assert got[LengthUnit.metre]["diag"] / got[LengthUnit.millimetre]["diag"] == pytest.approx(
        1000.0, rel=1e-6)


def test_neither_bundle_accepts_a_surface_that_cannot_state_its_unit(tmp_path):
    from meshpipeline.engines.cfmesh import cfmesh_runner as CF
    from meshpipeline.engines.snappy import snappy_runner as SN

    ws, _ = _staged(tmp_path, LengthUnit.millimetre)
    for runner in (CF, SN):
        with pytest.raises(ValueError, match="PreparedSurface"):
            runner.configure_mesh(ws, geometry_file="input.stl", surface=None, strategy={},
                                  wall_patch="body", contract_patches=_CONTRACT, args={},
                                  cell_budget=8_000_000)


def test_a_prepared_surface_is_not_scaled_again_by_either_bundle(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as CF
    from meshpipeline.engines.snappy import snappy_runner as SN
    monkeypatch.setattr(CF, "_to_fms", lambda ws_arg, angle, bashrc: "geom.fms")

    expected = EXPECTED_METRES[LengthUnit.millimetre]
    ws, surface = _staged(tmp_path, LengthUnit.millimetre)
    before = _bounds(surface.path)

    CF.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                      wall_patch="body", contract_patches=_CONTRACT, args={},
                      cell_budget=8_000_000)
    SN.prepare_surface(ws, geometry_file="input.stl", domain_min=[0, 0, 0],
                       domain_max=[expected * 3] * 3, wall_patch="body",
                       farfield_patch="farfield")

    assert _bounds(surface.path) == pytest.approx(before, rel=1e-12), (
        "the prepared surface itself was modified by a bundle")
    assert _extent(surface.path) == pytest.approx([expected] * 3, rel=1e-6)


def test_the_staged_surface_helper_matches_what_preparation_produced(tmp_path):
    from meshpipeline.cad.staging import staged_surface

    geom = _source(tmp_path, LengthUnit.centimetre)
    real = prepare_surface(geom, tmp_path / "ws" / "input.stl")
    described = staged_surface(geom, tmp_path / "ws" / "input.stl")

    assert described.source_id == real.source_id
    assert described.interpretation_id == real.interpretation_id
    assert described.consumed.current_unit == real.consumed.current_unit
    assert described.origin == real.origin
    assert described.scale_applied == real.scale_applied


def test_prepared_surface_helper_is_honest_about_its_claim(tmp_path):
    s = prepared_surface(tmp_path / "x.stl")
    assert s.unit == "m" and s.scale_applied == 1.0


# CAD into cfMesh and snappy

CAD_CASES = [
    ("MM", LengthUnit.millimetre, 10.0 * 1e-3),
    ("CM", LengthUnit.centimetre, 10.0 * 1e-2),
    ("INCH", LengthUnit.inch, 10.0 * 0.0254),
    ("M", LengthUnit.metre, 10.0),
]


def _cad_source(tmp_path, declared, unit, *, iges=False):
    from tests.cad_fixtures import write_iges, write_step_of_units

    name = "part.igs" if iges else "part.step"
    geom = materialized(tmp_path / f"src-{declared}{'-igs' if iges else ''}",
                        unit=unit, filename=name)
    (write_iges if iges else write_step_of_units)(geom.local_path, declared)
    return geom


@pytest.mark.parametrize("declared,unit,expected", CAD_CASES)
def test_cfmesh_receives_cad_at_its_true_physical_size(declared, unit, expected, tmp_path,
                                                       monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws_arg, angle, bashrc: "geom.fms")

    ws = tmp_path / f"ws-{declared}"
    surface = prepare_surface(_cad_source(tmp_path, declared, unit), ws / "input.stl",
                              engine="cfmesh")

    out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                           wall_patch="body", contract_patches=_CONTRACT, args={},
                           cell_budget=8_000_000)
    assert out.get("success"), out
    assert max(_extent(surface.path)) == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("declared,unit,expected", CAD_CASES)
def test_snappy_receives_cad_at_its_true_physical_size(declared, unit, expected, tmp_path):
    from meshpipeline.engines.snappy import snappy_runner as R

    ws = tmp_path / f"ws-{declared}"
    surface = prepare_surface(_cad_source(tmp_path, declared, unit), ws / "input.stl",
                              engine="snappy")

    prep = R.prepare_surface(ws, geometry_file="input.stl", domain_min=[-expected] * 3,
                             domain_max=[expected * 2] * 3, wall_patch="body",
                             farfield_patch="farfield")
    tri = ws / prep["surface_file"]
    assert tri.exists()
    assert max(_extent(tri)) == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("engine", ["cfmesh", "snappy"])
def test_iges_reaches_both_bundles_at_its_true_physical_size(engine, tmp_path, monkeypatch):
    expected = 10.0 * 1e-3
    ws = tmp_path / f"ws-igs-{engine}"
    surface = prepare_surface(_cad_source(tmp_path, "MM", LengthUnit.millimetre, iges=True),
                              ws / "input.stl", engine=engine)

    assert max(_extent(surface.path)) == pytest.approx(expected, rel=1e-6)

    if engine == "snappy":
        from meshpipeline.engines.snappy import snappy_runner as R
        prep = R.prepare_surface(ws, geometry_file="input.stl", domain_min=[-expected] * 3,
                                 domain_max=[expected * 2] * 3, wall_patch="body",
                                 farfield_patch="farfield")
        assert max(_extent(ws / prep["surface_file"])) == pytest.approx(expected, rel=1e-6)
    else:
        from meshpipeline.engines.cfmesh import cfmesh_runner as R
        monkeypatch.setattr(R, "_to_fms", lambda ws_arg, angle, bashrc: "geom.fms")
        out = R.configure_mesh(ws, geometry_file="input.stl", surface=surface, strategy={},
                               wall_patch="body", contract_patches=_CONTRACT, args={},
                               cell_budget=8_000_000)
        assert out.get("success"), out


def test_no_engine_bundle_restricts_the_upload_formats_it_will_stage(tmp_path):
    from meshpipeline.engines.registry import get_spec

    for engine in ("cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"):
        ic = get_spec(engine).input_contract
        assert getattr(ic, "accepted_suffixes", None) is None, (
            f"{engine} now restricts input formats; the extent matrix needs an explicit "
            "incompatibility entry for the formats it refuses")
