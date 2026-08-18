# Responsibility: Verify a user override beats a declaration, applied once, with no default for unitless input.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.STEPControl")

from tests._geometry_support import materialized  # noqa: E402
from tests.cad_fixtures import corrupt_step_unit, write_step_of_units  # noqa: E402

from meshpipeline.cad.staging import prepare_surface  # noqa: E402
from meshpipeline.cad.stl_io import read_stl_triangles  # noqa: E402
from meshpipeline.cad.unit_evidence import parser_applied_unit  # noqa: E402
from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis  # noqa: E402

RAW = 10.0          # raw coordinate span written into every CAD fixture


def _span(path) -> float:
    pts = [v for tri in read_stl_triangles(path) for v in tri]
    return max(p[0] for p in pts) - min(p[0] for p in pts)


def _cad(tmp_path, name, declared, unit, basis, *, corrupt=None):
    geom = materialized(tmp_path / name, unit=unit, basis=basis, filename="part.step")
    write_step_of_units(geom.local_path, declared, RAW)
    if corrupt:
        broken = corrupt_step_unit(geom.local_path, tmp_path / name / "broken.step", corrupt)
        broken.replace(geom.local_path)
    return geom


# what OCC already applied

@pytest.mark.parametrize("declared,expected", [
    ("MM", LengthUnit.millimetre), ("CM", LengthUnit.centimetre),
    ("INCH", LengthUnit.inch), ("M", LengthUnit.metre),
])
def test_the_parser_applied_unit_is_read_from_opencascade(declared, expected, tmp_path):
    assert parser_applied_unit(write_step_of_units(tmp_path / "p.step", declared, RAW)) is expected


@pytest.mark.parametrize("how", ["malformed", "missing"])
def test_unusable_unit_metadata_reports_occs_own_fallback(how, tmp_path):
    good = write_step_of_units(tmp_path / "g.step", "MM", RAW)
    assert parser_applied_unit(corrupt_step_unit(good, tmp_path / f"{how}.step", how)) \
        is LengthUnit.metre


# precedence

@pytest.mark.parametrize("declared,unit,expected_m", [
    ("MM", LengthUnit.millimetre, RAW * 1e-3),
    ("CM", LengthUnit.centimetre, RAW * 1e-2),
    ("INCH", LengthUnit.inch, RAW * 0.0254),
    ("M", LengthUnit.metre, RAW * 1.0),
])
def test_a_trusted_declaration_is_used_automatically(declared, unit, expected_m, tmp_path):
    geom = _cad(tmp_path, f"t{declared}", declared, unit, ResolutionBasis.file_declared)

    surface = prepare_surface(geom, tmp_path / f"t{declared}" / "ws" / "input.stl",
                              engine="cfmesh")

    assert _span(surface.path) == pytest.approx(expected_m, rel=1e-4)


@pytest.mark.parametrize("declared,confirmed,expected_m", [
    # the file says millimetres and is well formed; the user says it is really inches
    ("MM", LengthUnit.inch, RAW * 0.0254),
    # the file says inches; the user says millimetres
    ("INCH", LengthUnit.millimetre, RAW * 1e-3),
    # the file says metres; the user says centimetres
    ("M", LengthUnit.centimetre, RAW * 1e-2),
])
def test_a_user_override_beats_a_well_formed_declaration(declared, confirmed, expected_m,
                                                          tmp_path):
    geom = _cad(tmp_path, f"o{declared}", declared, confirmed, ResolutionBasis.user_confirmed)

    surface = prepare_surface(geom, tmp_path / f"o{declared}" / "ws" / "input.stl",
                              engine="cfmesh")

    assert _span(surface.path) == pytest.approx(expected_m, rel=1e-4), (
        f"a file declaring {declared}, overridden to {confirmed.value}, must measure {expected_m} m")


@pytest.mark.parametrize("how", ["malformed", "missing"])
def test_a_confirmed_unit_rescues_unusable_metadata(how, tmp_path):
    geom = _cad(tmp_path, f"c{how}", "MM", LengthUnit.millimetre,
                ResolutionBasis.user_confirmed, corrupt=how)

    surface = prepare_surface(geom, tmp_path / f"c{how}" / "ws" / "input.stl", engine="cfmesh")

    assert _span(surface.path) == pytest.approx(RAW * 1e-3, rel=1e-4), (
        "a confirmed millimetre reading must place the part at 0.01 m however OCC read the file")


def test_the_override_is_applied_once_not_twice(tmp_path):
    geom = _cad(tmp_path, "once", "MM", LengthUnit.inch, ResolutionBasis.user_confirmed)

    span = _span(prepare_surface(geom, tmp_path / "once" / "ws" / "input.stl",
                                 engine="cfmesh").path)

    assert span == pytest.approx(RAW * 0.0254, rel=1e-4)
    # both factors applied would give 10 * 1e-3 * 0.0254; neither is the answer
    assert span > RAW * 0.0254 * 1e-2, f"{span} looks like a double conversion"


def test_no_heuristic_default_exists_for_unitless_input(tmp_path):
    from meshpipeline.cad.staging import prepare_surface as prep

    geom = materialized(tmp_path / "stl", unit=LengthUnit.millimetre, filename="body.stl")
    # the guard is upstream: geometry with no interpretation cannot be built at all
    with pytest.raises(ValueError, match="verified execution geometry"):
        prep(None, tmp_path / "ws" / "input.stl")
