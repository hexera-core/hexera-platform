# Responsibility: Verify the native lumen reaches VMTK in metres, scaled once, with topology and arrays intact.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyvista")

from meshpipeline.contracts.coordinate_state import (  # noqa: E402
    from_occ_transfer,
    from_source_file,
)
from meshpipeline.contracts.geometry_units import (  # noqa: E402
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)
from meshpipeline.engines.vmtk import vmtk_runner as R  # noqa: E402

EDGE = 10.0

CASES = [
    (LengthUnit.metre, EDGE * 1.0),
    (LengthUnit.millimetre, EDGE * 1e-3),
    (LengthUnit.centimetre, EDGE * 1e-2),
    (LengthUnit.inch, EDGE * 0.0254),
]


def _lumen_vtp(path: Path, edge: float = EDGE) -> Path:
    import numpy as np
    import pyvista as pv

    mesh = pv.Cylinder(radius=edge / 4, height=edge, direction=(1, 0, 0),
                       resolution=24).triangulate().extract_surface()
    # the kinds of arrays the vascular path relies on
    mesh.point_data["Radius"] = np.linalg.norm(mesh.points[:, 1:], axis=1)
    mesh.point_data["GroupIds"] = np.zeros(mesh.n_points, dtype=int)
    mesh.cell_data["CellEntityIds"] = np.arange(mesh.n_cells, dtype=int)
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.save(str(path))
    return path


def _prepared(unit: LengthUnit):
    return from_source_file(GeometryInterpretation(
        interpretation_id="i-1", owner_id="o-1", geometry_source_id="s-1",
        unit=unit, scale_to_metres=scale_to_metres(unit),
        basis=ResolutionBasis.user_confirmed))


def _stage(tmp_path, unit: LengthUnit, *, edge: float = EDGE):
    ws = tmp_path / f"ws-{unit.value}"
    ws.mkdir(parents=True, exist_ok=True)
    src = _lumen_vtp(tmp_path / f"src-{unit.value}" / "lumen.vtp", edge)
    R.tessellate_to_stl(src, ws / "input.stl", prepared=_prepared(unit))
    return ws, src


def _read(path):
    import pyvista as pv
    return pv.read(str(path))


def _xspan(mesh) -> float:
    b = mesh.bounds
    return b[1] - b[0]


# the points move, once

@pytest.mark.parametrize("unit,expected", CASES)
def test_the_native_lumen_is_in_metres(unit, expected, tmp_path):
    ws, _src = _stage(tmp_path, unit)

    lumen = _read(ws / "lumen.vtp")
    assert _xspan(lumen) == pytest.approx(expected, rel=1e-6), (
        f"a {EDGE}-{unit.value} vessel must reach vmtk as {expected} m")


@pytest.mark.parametrize("unit,expected", CASES)
def test_the_preview_agrees_with_the_native_lumen(unit, expected, tmp_path):
    ws, _src = _stage(tmp_path, unit)

    assert _xspan(_read(ws / "input.stl")) == pytest.approx(
        _xspan(_read(ws / "lumen.vtp")), rel=1e-9)
    assert _xspan(_read(ws / "input.stl")) == pytest.approx(expected, rel=1e-6)


def test_the_original_upload_is_never_modified(tmp_path):
    ws, src = _stage(tmp_path, LengthUnit.millimetre)

    assert _xspan(_read(src)) == pytest.approx(EDGE, rel=1e-9), (
        "the source VTP was scaled in place - the upload must stay exactly as it arrived")
    assert _xspan(_read(ws / "lumen.vtp")) == pytest.approx(EDGE * 1e-3, rel=1e-6)


def test_scaling_is_not_applied_twice(tmp_path):
    ws, _src = _stage(tmp_path, LengthUnit.inch)

    got = _xspan(_read(ws / "lumen.vtp"))
    expected = EDGE * 0.0254
    assert got == pytest.approx(expected, rel=1e-6)
    assert got > expected * 0.5, f"{got} vs {expected}: the factor looks applied twice"


def test_equivalent_vessels_in_different_units_agree(tmp_path):
    spans = []
    for unit, expected in CASES:
        edge = EDGE * (CASES[0][1] / expected)          # each is physically 10 m
        ws, _ = _stage(tmp_path, unit, edge=edge)
        spans.append(_xspan(_read(ws / "lumen.vtp")))
    for s in spans:
        assert s == pytest.approx(spans[0], rel=1e-6)


# everything else stays put

@pytest.mark.parametrize("unit,_expected", CASES)
def test_topology_and_arrays_survive_the_conversion(unit, _expected, tmp_path):
    import numpy as np

    ws, src = _stage(tmp_path, unit)
    before, after = _read(src), _read(ws / "lumen.vtp")

    assert after.n_points == before.n_points
    assert after.n_cells == before.n_cells
    assert np.array_equal(after.faces, before.faces), "connectivity changed"

    assert set(after.point_data) >= {"Radius", "GroupIds"}
    assert set(after.cell_data) >= {"CellEntityIds"}
    # array VALUES are carried through unchanged - scaling a coordinate is not a licence to
    # rewrite a field. That is the CORRECT rule here, not a deferral: see
    # `test_the_bundle_consumes_no_dimensional_array_from_the_upload` - every dimensional array in
    # the vmtk chain is generated FROM the metre lumen, so scaling an upload array would either
    # corrupt an identifier or double-apply the factor.
    assert np.array_equal(after.point_data["GroupIds"], before.point_data["GroupIds"])
    assert np.array_equal(after.cell_data["CellEntityIds"], before.cell_data["CellEntityIds"])


def test_the_lumen_stays_a_closed_surface(tmp_path):
    ws, src = _stage(tmp_path, LengthUnit.centimetre)
    before, after = _read(src), _read(ws / "lumen.vtp")

    assert after.extract_surface().n_open_edges == before.extract_surface().n_open_edges


# what it refuses

def test_staging_without_coordinate_state_is_refused(tmp_path):
    src = _lumen_vtp(tmp_path / "src" / "lumen.vtp")

    with pytest.raises(ValueError, match="coordinate state"):
        R.tessellate_to_stl(src, tmp_path / "ws" / "input.stl", prepared=None)


# array semantics, established

def test_the_bundle_consumes_no_dimensional_array_from_the_upload():
    from pathlib import Path as _P

    runner = _P(R.__file__).read_text()
    viewer = (_P(R.__file__).parent / "viewer_surface.py").read_text()

    # dimensional arrays are named as vmtk OUTPUTS being piped onward, never read off the upload
    assert "-edgelengtharray" in runner and "DistanceToCenterlines" in runner
    assert "-useradius" in runner
    # the one upload-side array the bundle names is an identifier
    assert "CellEntityIds" in viewer
    for dimensional in ("Radius", "MaximumInscribedSphereRadius", "Thickness", "Area", "Volume"):
        assert f'"{dimensional}"' not in viewer, (
            f"{dimensional} is now read from the upload; it is a dimensional quantity and needs an "
            "explicit scale decision rather than the preserve rule this test documents")


def test_an_identifier_array_is_never_scaled(tmp_path):
    import numpy as np

    ws, src = _stage(tmp_path, LengthUnit.inch)
    before, after = _read(src), _read(ws / "lumen.vtp")

    assert np.array_equal(after.cell_data["CellEntityIds"], before.cell_data["CellEntityIds"])
    assert after.cell_data["CellEntityIds"].dtype == before.cell_data["CellEntityIds"].dtype


def test_generated_distance_arrays_would_already_be_metres(tmp_path):
    expected_radius = (EDGE / 4) * 0.0254          # the fixture's radius, in metres
    ws, _src = _stage(tmp_path, LengthUnit.inch)

    lumen = _read(ws / "lumen.vtp")
    b = lumen.bounds
    assert (b[3] - b[2]) / 2 == pytest.approx(expected_radius, rel=1e-6), (
        "the lumen vmtk would measure is not in metres, so every distance array it generated "
        "would inherit the wrong scale")


# CAD into vmtk, numerically

CAD_CASES = [
    ("MM", LengthUnit.millimetre, ResolutionBasis.file_declared, 10.0 * 1e-3),
    ("CM", LengthUnit.centimetre, ResolutionBasis.file_declared, 10.0 * 1e-2),
    ("INCH", LengthUnit.inch, ResolutionBasis.file_declared, 10.0 * 0.0254),
    ("M", LengthUnit.metre, ResolutionBasis.user_confirmed, 10.0),
]


def _cad_prepared(unit, basis):
    return from_occ_transfer(GeometryInterpretation(
        interpretation_id="i-cad", owner_id="o-1", geometry_source_id="s-cad",
        unit=unit, scale_to_metres=scale_to_metres(unit), basis=basis))


def _stage_cad(tmp_path, name, writer, declared, unit, basis):
    ws = tmp_path / f"ws-{name}"
    ws.mkdir(parents=True, exist_ok=True)
    src = tmp_path / f"src-{name}" / ("part.igs" if "iges" in name else "part.step")
    src.parent.mkdir(parents=True, exist_ok=True)
    writer(src, declared)   # physically sized by the caller
    R.tessellate_to_stl(src, ws / "input.stl", prepared=_cad_prepared(unit, basis))
    return ws


@pytest.mark.parametrize("declared,unit,basis,expected", CAD_CASES)
def test_step_reaches_the_vmtk_native_lumen_at_its_physical_size(declared, unit, basis, expected,
                                                                  tmp_path):
    from tests.cad_fixtures import write_step_of_units

    ws = _stage_cad(tmp_path, f"step-{declared}", write_step_of_units, declared, unit, basis)

    lumen = _read(ws / "lumen.vtp")
    preview = _read(ws / "input.stl")
    assert _xspan(lumen) == pytest.approx(expected, rel=1e-4), (
        f"a 10-unit {declared} solid must reach vmtk as {expected} m")
    # the native input and the preview come from the same prepared surface
    assert _xspan(preview) == pytest.approx(_xspan(lumen), rel=1e-9)
    # and the lumen is a real surface, not an empty shell
    assert lumen.n_points > 0 and lumen.n_cells > 0


def test_a_malformed_step_still_goes_through_the_occ_output_boundary(tmp_path):
    from tests.cad_fixtures import corrupt_step_unit, write_step

    good = write_step(tmp_path / "good.step", "MM")
    broken = corrupt_step_unit(good, tmp_path / "broken.step", "malformed")
    ws = tmp_path / "ws-broken"
    ws.mkdir(parents=True)
    # the user confirmed inches for a file whose declaration cannot be trusted
    R.tessellate_to_stl(broken, ws / "input.stl",
                        prepared=_cad_prepared(LengthUnit.inch, ResolutionBasis.user_confirmed))

    # OCC read the damaged file as metres (10 m -> 10000 mm); the single post-transfer conversion
    # then yields 10 m. The size is wrong because the FILE is wrong - not because the product
    # scaled twice, which is what this asserts.
    assert _xspan(_read(ws / "lumen.vtp")) == pytest.approx(10.0, rel=1e-4)


def test_iges_reaches_the_vmtk_native_lumen(tmp_path):
    from tests.cad_fixtures import write_iges

    ws = _stage_cad(tmp_path, "iges-MM", write_iges, "MM",
                    LengthUnit.millimetre, ResolutionBasis.file_declared)

    assert _xspan(_read(ws / "lumen.vtp")) == pytest.approx(10.0 * 1e-3, rel=1e-4)


def test_a_confirmed_unit_does_not_re_scale_a_well_formed_cad_file(tmp_path):
    from tests.cad_fixtures import write_iges

    ws = _stage_cad(tmp_path, "iges-conf", write_iges, "MM",
                    LengthUnit.centimetre, ResolutionBasis.user_confirmed)

    # the FILE's declaration decided the size; the confirmed centimetre did not move it
    assert _xspan(_read(ws / "lumen.vtp")) == pytest.approx(10.0 * 1e-3, rel=1e-4)


@pytest.mark.parametrize("declared,unit,basis,expected", CAD_CASES)
def test_cad_staged_lumen_topology_is_sound(declared, unit, basis, expected, tmp_path):
    from tests.cad_fixtures import write_step_of_units

    ws = _stage_cad(tmp_path, f"topo-{declared}", write_step_of_units, declared, unit, basis)
    lumen = _read(ws / "lumen.vtp").extract_surface()

    assert lumen.n_points > 3 and lumen.n_cells > 3
    assert lumen.n_open_edges == 0, "a CAD-derived lumen must be closed for vmtk to fill it"
