# Responsibility: Verify a prepared surface reports the same physical facts whatever unit it was authored in.
from __future__ import annotations

import math
from pathlib import Path

import pytest
from tests._geometry_support import materialized

from meshpipeline.cad.analysis import AmbiguousSurfaceUnits, analyze_surface
from meshpipeline.cad.prepared_surface import PreparedSurface, SurfaceAlreadyPrepared
from meshpipeline.cad.staging import prepare_surface
from meshpipeline.contracts.geometry_units import LengthUnit

#: One cube edge, in whatever unit the source is declared to be.
EDGE = 10.0

#: What a 10-unit cube must measure in metres once interpreted.
EXPECTED_METRES = {
    LengthUnit.metre: 10.0,
    LengthUnit.millimetre: 0.01,
    LengthUnit.centimetre: 0.1,
    LengthUnit.inch: 0.254,
}


def _cube_stl(path: Path, edge: float = EDGE, name: str = "body") -> Path:
    v = [(0, 0, 0), (edge, 0, 0), (edge, edge, 0), (0, edge, 0),
         (0, 0, edge), (edge, 0, edge), (edge, edge, edge), (0, edge, edge)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 5, 1), (0, 4, 5),
             (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    out = [f"solid {name}"]
    for a, b, c in faces:
        out.append("  facet normal 0 0 0\n    outer loop")
        for idx in (a, b, c):
            # full double precision: at 9 significant figures an inch fixture
            # round-trips to 9.9999999898 m and the tolerance would be hiding
            # the fixture's own truncation rather than measuring the conversion
            out.append("      vertex {:.17g} {:.17g} {:.17g}".format(*v[idx]))
        out.append("    endloop\n  endfacet")
    out.append(f"endsolid {name}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))
    return path


def _bounds(stl: Path) -> tuple[list[float], list[float]]:
    from meshpipeline.cad.stl_io import read_stl_triangles

    tris = read_stl_triangles(Path(stl))
    pts = [v for t in tris for v in t]
    return ([min(p[i] for p in pts) for i in range(3)],
            [max(p[i] for p in pts) for i in range(3)])


def _extent(stl: Path) -> list[float]:
    lo, hi = _bounds(stl)
    return [hi[i] - lo[i] for i in range(3)]


def _source(tmp_path, unit: LengthUnit, *, edge: float = EDGE):
    geom = materialized(tmp_path / f"src-{unit.value}", unit=unit, filename="body.stl")
    _cube_stl(Path(geom.local_path), edge)
    return geom


# the conversion itself

@pytest.mark.parametrize("unit", list(EXPECTED_METRES))
def test_a_ten_unit_cube_is_prepared_at_its_true_physical_size(unit, tmp_path):
    expected = EXPECTED_METRES[unit]
    geom = _source(tmp_path, unit)
    assert _extent(Path(geom.local_path)) == pytest.approx([EDGE] * 3), "fixture is not 10 units"

    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl")

    assert _extent(surface.path) == pytest.approx([expected] * 3, rel=1e-6), (
        f"a {EDGE} {unit.value} cube must be {expected} m")
    assert surface.unit == "m"
    assert surface.scale_applied == pytest.approx(expected / EDGE)


def test_confirmed_metres_keeps_the_users_exact_bytes(tmp_path):
    geom = _source(tmp_path, LengthUnit.metre)
    original = Path(geom.local_path).read_bytes()

    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl")

    assert surface.path.read_bytes() == original
    assert surface.scale_applied == 1.0


@pytest.mark.parametrize("unit", [LengthUnit.millimetre, LengthUnit.centimetre, LengthUnit.inch])
def test_scaling_preserves_topology_and_solid_naming(unit, tmp_path):
    from meshpipeline.cad.stl_io import read_stl_solids, read_stl_triangles

    geom = _source(tmp_path, unit)
    before_solids = read_stl_solids(Path(geom.local_path))
    before = read_stl_triangles(Path(geom.local_path))

    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl")

    after_solids = read_stl_solids(surface.path)
    after = read_stl_triangles(surface.path)
    assert list(after_solids) == list(before_solids) == ["body"]
    assert len(after) == len(before) == 12
    factor = surface.scale_applied
    for tri_before, tri_after in zip(before, after):
        for v_before, v_after in zip(tri_before, tri_after):
            for c_before, c_after in zip(v_before, v_after):
                assert c_after == pytest.approx(c_before * factor, rel=1e-6, abs=1e-12)


def test_preparation_is_exactly_once(tmp_path):
    geom = _source(tmp_path, LengthUnit.millimetre)
    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl")

    with pytest.raises(SurfaceAlreadyPrepared):
        surface.refuse_second_normalisation()

    # and preparing the SOURCE again is idempotent - it reads the source, never its own output
    again = prepare_surface(geom, tmp_path / "ws2" / "input.stl")
    assert _extent(again.path) == pytest.approx(_extent(surface.path), rel=1e-6)


# generic physical analysis

@pytest.mark.parametrize("unit", list(EXPECTED_METRES))
def test_analysis_reports_the_same_physical_facts_whatever_the_source_unit(unit, tmp_path):
    edge = EDGE * (EXPECTED_METRES[LengthUnit.metre] / EXPECTED_METRES[unit])
    geom = _source(tmp_path, unit, edge=edge)          # every one is physically 10 m
    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl")

    a = analyze_surface(surface)

    assert a["extent"] == pytest.approx([10.0] * 3, rel=1e-9)
    assert a["diag"] == pytest.approx(math.sqrt(3) * 10.0, rel=1e-9)
    assert a["surface_area"] == pytest.approx(6 * 100.0, rel=1e-9)


def test_changing_only_the_interpretation_changes_physical_facts_by_that_ratio(tmp_path):
    mm = prepare_surface(_source(tmp_path, LengthUnit.millimetre), tmp_path / "a" / "input.stl")
    cm = prepare_surface(_source(tmp_path, LengthUnit.centimetre), tmp_path / "b" / "input.stl")

    a_mm, a_cm = analyze_surface(mm), analyze_surface(cm)
    assert a_cm["diag"] / a_mm["diag"] == pytest.approx(10.0, rel=1e-6)
    assert a_cm["surface_area"] / a_mm["surface_area"] == pytest.approx(100.0, rel=1e-6)


def test_rematerialising_in_another_workspace_changes_nothing_physical(tmp_path):
    geom = _source(tmp_path, LengthUnit.inch)
    one = analyze_surface(prepare_surface(geom, tmp_path / "ws-a" / "input.stl"))
    two = analyze_surface(prepare_surface(geom, tmp_path / "ws-b" / "input.stl"))

    assert one["extent"] == pytest.approx(two["extent"], rel=1e-6)
    assert one["diag"] == pytest.approx(two["diag"], rel=1e-6)


def test_dimensionless_quantities_do_not_move_with_the_unit(tmp_path):
    mm = analyze_surface(prepare_surface(_source(tmp_path, LengthUnit.millimetre),
                                         tmp_path / "a" / "input.stl"))
    m = analyze_surface(prepare_surface(_source(tmp_path, LengthUnit.metre),
                                        tmp_path / "b" / "input.stl"))

    assert mm["n_triangles"] == m["n_triangles"]
    # aspect ratio of a cube is 1 whatever the unit
    assert max(mm["extent"]) / min(mm["extent"]) == pytest.approx(
        max(m["extent"]) / min(m["extent"]), rel=1e-12)


def test_raw_coordinates_cannot_reach_physical_analysis(tmp_path):
    stl = _cube_stl(tmp_path / "loose.stl")

    for ambiguous in (stl, str(stl), None):
        with pytest.raises(AmbiguousSurfaceUnits):
            analyze_surface(ambiguous)


def test_refinement_recommendations_are_physical(tmp_path):
    from meshpipeline.cad.analysis import recommend_refinement

    recs = {}
    for unit in (LengthUnit.metre, LengthUnit.millimetre, LengthUnit.inch):
        edge = EDGE * (EXPECTED_METRES[LengthUnit.metre] / EXPECTED_METRES[unit])
        surface = prepare_surface(_source(tmp_path, unit, edge=edge),
                                  tmp_path / f"ws-{unit.value}" / "input.stl")
        recs[unit] = recommend_refinement(analyze_surface(surface), max_cells=4_000_000)

    baseline = recs[LengthUnit.metre]
    for unit, rec in recs.items():
        assert rec["min_feature"] == pytest.approx(baseline["min_feature"], rel=1e-6), unit
        assert rec["surface_area"] == pytest.approx(baseline["surface_area"], rel=1e-6), unit
        # levels are dimensionless: the same physical object earns the same refinement
        assert rec["surface_level"] == baseline["surface_level"], unit


def test_a_prepared_surface_records_its_provenance(tmp_path):
    geom = _source(tmp_path, LengthUnit.millimetre)
    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl")

    assert isinstance(surface, PreparedSurface)
    assert surface.source_id == geom.ref.source_id
    assert surface.interpretation_id == geom.interpretation.interpretation_id
    assert surface.origin.value == "source_file"       # an STL is raw file coordinates
    assert surface.consumed.current_unit is LengthUnit.millimetre
