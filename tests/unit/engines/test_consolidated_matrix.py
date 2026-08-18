# Responsibility: Verify every supported input format prepares to its true physical size, across the engine matrix.
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPControl")

from tests._geometry_support import materialized  # noqa: E402
from tests.cad_fixtures import (  # noqa: E402
    corrupt_step_unit,
    write_iges,
    write_step_of_units,
)

from meshpipeline.cad.staging import prepare_surface  # noqa: E402
from meshpipeline.cad.stl_io import read_stl_triangles  # noqa: E402
from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis  # noqa: E402

#: Raw coordinate span per family - deliberately DIFFERENT per unit, so a row cannot pass by
#: coincidence the way a single shared physical size allowed.
RAW = {LengthUnit.metre: 2.0, LengthUnit.millimetre: 7.0,
       LengthUnit.centimetre: 3.0, LengthUnit.inch: 5.0}
FACTOR = {LengthUnit.metre: 1.0, LengthUnit.millimetre: 1e-3,
          LengthUnit.centimetre: 1e-2, LengthUnit.inch: 0.0254}
DECL = {LengthUnit.metre: "M", LengthUnit.millimetre: "MM",
        LengthUnit.centimetre: "CM", LengthUnit.inch: "INCH"}
TOL = 1e-4          # STL is float32; CAD adds a text round trip. A real error is >= 25.4x.

def _span_stl(path) -> float:
    pts = [v for tri in read_stl_triangles(Path(path)) for v in tri]
    return max(p[0] for p in pts) - min(p[0] for p in pts)


def _span_vtp(path) -> float:
    import pyvista as pv
    b = pv.read(str(path)).bounds
    return b[1] - b[0]


def _stl(tmp_path, unit, *, binary):
    import pyvista as pv
    geom = materialized(tmp_path / f"stl-{unit.value}-{binary}", unit=unit,
                        basis=ResolutionBasis.user_confirmed, filename="body.stl")
    side = RAW[unit]
    pv.Cube(x_length=side, y_length=side, z_length=side).triangulate().save(
        str(geom.local_path), binary=binary)
    return geom


def _vtp(tmp_path, unit):
    import pyvista as pv
    geom = materialized(tmp_path / f"vtp-{unit.value}", unit=unit,
                        basis=ResolutionBasis.user_confirmed, filename="lumen.vtp")
    side = RAW[unit]
    m = pv.Cylinder(radius=side / 4, height=side, direction=(1, 0, 0),
                    resolution=16).triangulate().extract_surface()
    m.cell_data["CellEntityIds"] = list(range(m.n_cells))
    m.save(str(geom.local_path))
    return geom


# surfaces

def measure_stl(root, unit, *, binary) -> dict:
    expected = RAW[unit] * FACTOR[unit]
    geom = _stl(root, unit, binary=binary)
    surface = prepare_surface(geom, root / f"ws-stl-{unit.value}-{binary}" / "input.stl")
    return {"format": f"STL {'binary' if binary else 'ascii'}", "unit": unit.value,
            "evidence": "none (unitless)", "override": "user confirmed", "raw": RAW[unit],
            "factor": FACTOR[unit], "expected_m": expected,
            "prepared_m": _span_stl(surface.path), "engines": "cfmesh, snappy"}


@pytest.mark.parametrize("binary", [False, True], ids=["ascii", "binary"])
@pytest.mark.parametrize("unit", list(RAW))
def test_stl_prepares_to_its_true_physical_size(unit, binary, tmp_path):
    row = measure_stl(tmp_path, unit, binary=binary)
    assert row["prepared_m"] == pytest.approx(row["expected_m"], rel=TOL)


def measure_vtp(root, unit) -> dict:
    import pyvista as pv

    from meshpipeline.engines.vmtk import vmtk_runner as R

    expected = RAW[unit] * FACTOR[unit]
    geom = _vtp(root, unit)
    ws = root / f"ws-vtp-{unit.value}"
    ws.mkdir(parents=True)
    R.tessellate_to_stl(Path(geom.path), ws / "input.stl", prepared=geom.prepared)
    got = _span_vtp(ws / "lumen.vtp")

    before, after = pv.read(str(geom.path)), pv.read(str(ws / "lumen.vtp"))
    assert (after.n_points, after.n_cells) == (before.n_points, before.n_cells)
    assert list(after.cell_data["CellEntityIds"]) == list(before.cell_data["CellEntityIds"])
    return {"format": "VTP", "unit": unit.value, "evidence": "none (unitless)",
            "override": "user confirmed", "raw": RAW[unit], "factor": FACTOR[unit],
            "expected_m": expected, "prepared_m": got, "engines": "vmtk",
            "cells": after.n_cells, "points": after.n_points}


@pytest.mark.parametrize("unit", list(RAW))
def test_vtp_prepares_to_its_true_physical_size(unit, tmp_path):
    row = measure_vtp(tmp_path, unit)
    assert row["prepared_m"] == pytest.approx(row["expected_m"], rel=TOL)


# CAD

def measure_step(root, unit) -> dict:
    expected = RAW[unit] * FACTOR[unit]
    geom = materialized(root / f"step-{unit.value}", unit=unit,
                        basis=ResolutionBasis.file_declared, filename="part.step")
    write_step_of_units(geom.local_path, DECL[unit], RAW[unit])
    surface = prepare_surface(geom, root / f"ws-step-{unit.value}" / "input.stl", engine="cfmesh")
    return {"format": "STEP", "unit": unit.value, "evidence": f"declared {DECL[unit]}",
            "override": "none", "raw": RAW[unit], "factor": FACTOR[unit],
            "expected_m": expected, "prepared_m": _span_stl(surface.path),
            "engines": "cfmesh, snappy, gmsh"}


@pytest.mark.parametrize("unit", list(RAW))
def test_step_prepares_to_its_true_physical_size(unit, tmp_path):
    row = measure_step(tmp_path, unit)
    assert row["prepared_m"] == pytest.approx(row["expected_m"], rel=TOL)


def measure_iges(root) -> dict:
    geom = materialized(root / "iges", unit=LengthUnit.millimetre,
                        basis=ResolutionBasis.file_declared, filename="part.igs")
    write_iges(geom.local_path, "MM")
    surface = prepare_surface(geom, root / "ws-iges" / "input.stl", engine="cfmesh")
    return {"format": "IGES", "unit": "mm", "evidence": "declared MM", "override": "none",
            "raw": 10.0, "factor": 1e-3, "expected_m": 0.01,
            "prepared_m": _span_stl(surface.path), "engines": "cfmesh, snappy, gmsh"}


def test_iges_prepares_to_its_true_physical_size(tmp_path):
    row = measure_iges(tmp_path)
    assert row["prepared_m"] == pytest.approx(0.01, rel=TOL)


def measure_override(root) -> dict:
    raw = 5.0
    geom = materialized(root / "ovr", unit=LengthUnit.inch,
                        basis=ResolutionBasis.user_confirmed, filename="part.step")
    write_step_of_units(geom.local_path, "MM", raw)
    got = _span_stl(prepare_surface(geom, root / "ws-ovr" / "input.stl", engine="cfmesh").path)
    return {"format": "STEP", "unit": "in", "evidence": "declared MM (well formed)",
            "override": "user confirmed inch", "raw": raw, "factor": 0.0254,
            "expected_m": raw * 0.0254, "prepared_m": got, "engines": "cfmesh, snappy, gmsh"}


def test_a_well_formed_declaration_overridden_by_the_user(tmp_path):
    row = measure_override(tmp_path)
    assert row["prepared_m"] == pytest.approx(row["expected_m"], rel=TOL)


def measure_unusable_metadata(root, how) -> dict:
    raw = 7.0
    geom = materialized(root / f"bad-{how}", unit=LengthUnit.millimetre,
                        basis=ResolutionBasis.user_confirmed, filename="part.step")
    write_step_of_units(geom.local_path, "MM", raw)
    corrupt_step_unit(geom.local_path, root / f"bad-{how}" / "b.step", how).replace(
        geom.local_path)
    got = _span_stl(prepare_surface(geom, root / f"ws-bad-{how}" / "input.stl",
                                    engine="cfmesh").path)
    return {"format": "STEP", "unit": "mm", "evidence": f"{how} (OCC fell back to metre)",
            "override": "user confirmed mm", "raw": raw, "factor": 1e-3,
            "expected_m": raw * 1e-3, "prepared_m": got, "engines": "cfmesh, snappy, gmsh"}


@pytest.mark.parametrize("how", ["malformed", "missing"])
def test_unusable_cad_metadata_with_confirmation(how, tmp_path):
    row = measure_unusable_metadata(tmp_path, how)
    assert row["prepared_m"] == pytest.approx(row["expected_m"], rel=TOL)


def measure_assembly(root) -> dict:
    from tests.unit.engines.test_multiregion_assembly_metres import (
        _assembly_step,
        _prepared,
    )

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as MR

    unit = LengthUnit.millimetre
    step = _assembly_step(root / "asm.step", "MM")
    solids = MR.read_assembly_solids(step, root / "out-asm", prepared=_prepared(unit))
    span = max(s["bbox_max"][0] for s in solids) - min(s["bbox_min"][0] for s in solids)
    assert len(solids) == 2
    return {"format": "STEP assembly", "unit": "mm", "evidence": "declared MM",
            "override": "none", "raw": 20.0, "factor": 1e-3, "expected_m": 0.02,
            "prepared_m": span, "engines": "snappy_multiregion", "solids": len(solids)}


def test_the_two_solid_assembly_preserves_regions_and_interface(tmp_path):
    row = measure_assembly(tmp_path)
    assert row["prepared_m"] == pytest.approx(0.02, rel=TOL)


def measure_refusal(root) -> dict:
    with pytest.raises(ValueError, match="verified execution geometry"):
        prepare_surface(None, root / "ws-refusal" / "input.stl")
    return {"format": "STL", "unit": "unknown", "evidence": "none (unitless)", "override": "none",
            "raw": float("nan"), "factor": float("nan"), "expected_m": float("nan"),
            "prepared_m": float("nan"), "engines": "-", "outcome": "refused before execution"}


def test_unitless_input_without_confirmation_is_refused(tmp_path):
    assert measure_refusal(tmp_path)["outcome"] == "refused before execution"


# the matrix

#: The exact table this suite claims to have measured: (format, unit, override) -> how many rows.
#: Declared, so a row that stops being produced fails here instead of shrinking the matrix
#: silently, and a row nobody expected cannot appear in it either.
EXPECTED_ROWS: dict[tuple[str, str, str], int] = {
    ("STL ascii", "m", "user confirmed"): 1,
    ("STL ascii", "mm", "user confirmed"): 1,
    ("STL ascii", "cm", "user confirmed"): 1,
    ("STL ascii", "in", "user confirmed"): 1,
    ("STL binary", "m", "user confirmed"): 1,
    ("STL binary", "mm", "user confirmed"): 1,
    ("STL binary", "cm", "user confirmed"): 1,
    ("STL binary", "in", "user confirmed"): 1,
    ("VTP", "m", "user confirmed"): 1,
    ("VTP", "mm", "user confirmed"): 1,
    ("VTP", "cm", "user confirmed"): 1,
    ("VTP", "in", "user confirmed"): 1,
    ("STEP", "m", "none"): 1,
    ("STEP", "mm", "none"): 1,
    ("STEP", "cm", "none"): 1,
    ("STEP", "in", "none"): 1,
    ("IGES", "mm", "none"): 1,
    ("STEP", "in", "user confirmed inch"): 1,
    ("STEP", "mm", "user confirmed mm"): 2,          # malformed + missing metadata
    ("STEP assembly", "mm", "none"): 1,
    ("STL", "unknown", "none"): 1,                   # the refusal row
}

TOTAL_ROWS = sum(EXPECTED_ROWS.values())


def build_matrix(root: Path) -> list[dict]:
    rows: list[dict] = []
    for unit in RAW:
        for binary in (False, True):
            rows.append(measure_stl(root, unit, binary=binary))
    for unit in RAW:
        rows.append(measure_vtp(root, unit))
    for unit in RAW:
        rows.append(measure_step(root, unit))
    rows.append(measure_iges(root))
    rows.append(measure_override(root))
    for how in ("malformed", "missing"):
        rows.append(measure_unusable_metadata(root, how))
    rows.append(measure_assembly(root))
    rows.append(measure_refusal(root))
    return rows


def test_the_measured_matrix_is_exactly_the_declared_one(tmp_path):
    rows = build_matrix(tmp_path)

    counted: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r["format"], r["unit"], r["override"])
        counted[key] = counted.get(key, 0) + 1

    missing = {k: n for k, n in EXPECTED_ROWS.items() if counted.get(k, 0) != n}
    unexpected = {k: n for k, n in counted.items() if k not in EXPECTED_ROWS}
    assert not missing, f"declared rows not measured (or measured the wrong number of times): {missing}"
    assert not unexpected, f"rows measured that the matrix does not declare: {unexpected}"
    assert len(rows) == TOTAL_ROWS == 22, f"expected {TOTAL_ROWS} rows, measured {len(rows)}"

    dest = Path(os.environ["MATRIX_OUT"]) if os.environ.get("MATRIX_OUT") else None
    if dest:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(rows, indent=2, default=str))

    checked = 0
    for r in rows:
        if r["expected_m"] == r["expected_m"]:            # skip the NaN refusal row
            assert r["prepared_m"] == pytest.approx(r["expected_m"], rel=TOL), r
            checked += 1
    assert checked == TOTAL_ROWS - 1, (
        f"only {checked} rows carried a comparable measurement; one refusal row is expected")
