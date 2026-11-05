# Responsibility: Prove the snap-grid mesh carries an ECXML model's nanometre layers honestly: a thin-layer resistance becomes its own interface pair with the solver entries for both OpenFOAM dialects, the mesh is the file's model, the viewer keeps nanometre layers apart far from the origin, and the results read in nm / um / mm.
from __future__ import annotations

import re

import numpy as np
import pytest
from tests.unit.cad.ecxml_models import Ecxml

from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid import results as R
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml
from meshpipeline.engines.snapgrid.native import interface_faces
from meshpipeline.engines.snapgrid.thin_layers import INCLUDE_NAME
from meshpipeline.engines.snapgrid.viewer import VIEWER_NAME, snapgrid_viewer


def _materials(doc: Ecxml) -> Ecxml:
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390))
    doc.material("Oxide", 2200, 730, 0.9, ("isotropic", 1.4))
    return doc


def _stack(t: float, off: float = 0.0, *, domain: bool = False, heated_film: bool = False):
    doc = _materials(Ecxml(f"Stack {t:g}", places=None))
    if domain:
        doc.domain((off - 0.002, off - 0.002, off - 0.002), (0.014, 0.014, 0.005))
    doc.block("Base", (off, off, off), (0.01, 0.01, 0.0005), "Si")
    doc.block("Film", (off, off, off + 0.0005), (0.01, 0.01, t), "Oxide",
              0.5 if heated_film else 0.0)
    doc.block("Cap", (off, off, off + 0.0005 + t), (0.01, 0.01, 0.0005), "Cu", 10.0)
    return doc


def _mesh(tmp_path, doc: Ecxml, dialect: str = "foundation", **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "model.ecxml"
    src.write_bytes(doc.xml())
    return mesh_ecxml(src, tmp_path / "case", G.GridPlan(max_cells=100_000), binary=False,
                      dialect=dialect, **kw)


def _boundary(case, region) -> dict[str, int]:
    text = (case / "constant" / region / "polyMesh" / "boundary").read_text()
    return {m.group(1): int(m.group(2)) for m in re.finditer(
        r"\n\s*([A-Za-z0-9_]+)\s*\n\s*\{[^}]*?nFaces\s+(\d+);", text)}


@pytest.mark.parametrize("dialect", ["foundation", "esi"])
def test_a_ten_nanometre_film_is_a_thin_layer_interface_with_solver_entries(tmp_path, dialect):
    result = _mesh(tmp_path, _stack(1e-8), dialect)
    case, rep = result.case, result.report
    assert rep["solid_regions"] == ["Base", "Cap"]
    (row,) = rep["thin_layer_interfaces"]
    assert row["patch_a"] == "Base_to_Cap_via_Film" and row["patch_b"] == "Cap_to_Base_via_Film"
    assert row["area_m2"] == pytest.approx(1e-4, rel=1e-9)          # the film's whole footprint
    assert row["R_layers_m2K_W"] == pytest.approx(1e-8 / 1.4)
    # the copper that grew into its place adds 10 nm / 390: taken off
    assert row["R_applied_m2K_W"] == pytest.approx(1e-8 / 1.4 - 1e-8 / 390)
    # every face between Base and Cap is over the film: the pair carries all of them
    assert _boundary(case, "Base") == {**_boundary(case, "Base"), "Base_to_Cap_via_Film": row["faces"]}
    assert "Base_to_Cap" not in _boundary(case, "Base")
    assert interface_faces(rep["region_meshes"]["Base"]["interfaces"], "Base", "Cap") == row["faces"]
    for region, patch in (("Base", row["patch_a"]), ("Cap", row["patch_b"])):
        text = (case / "constant" / region / INCLUDE_NAME).read_text()
        assert patch in text
        typ = "coupledTemperature" if dialect == "foundation" else \
            "compressible::turbulentTemperatureRadCoupledMixed"
        assert f"type            {typ};" in text
        t = float(re.search(r"thicknessLayers \(([^)]+)\)", text).group(1))
        k = float(re.search(r"kappaLayers     \(([^)]+)\)", text).group(1))
        assert t / k == pytest.approx(row["R_applied_m2K_W"], rel=1e-12)
    assert rep["thin_layer_files"] == [f"constant/Base/{INCLUDE_NAME}", f"constant/Cap/{INCLUDE_NAME}"]
    assert any(ln.startswith("Thin-layer interfaces: 1 interface patch pair") for ln in rep["build_report"])
    side = result.sidecar
    assert side["thin_layers"][0]["name"] == "Film" and side["thin_layer_interfaces"] == rep["thin_layer_interfaces"]


def test_a_film_laid_into_part_of_an_interface_splits_it_exactly(tmp_path):
    """Flotherm-style: a film laid into the cap's lower face (later in the file, so it wins
    there). Without it the cap fills its place; its resistance sits on its own footprint."""
    doc = _materials(Ecxml("Partial film", places=None))
    doc.block("Base", (0, 0, 0), (0.01, 0.01, 0.0005), "Si")
    doc.block("Cap", (0, 0, 0.0005), (0.01, 0.01, 0.0005), "Cu", 10.0)
    doc.block("Film", (0.002, 0.002, 0.0005), (0.004, 0.004, 1e-8), "Oxide")
    result = _mesh(tmp_path, doc)
    (layer,) = result.report["thin_layer_resistances"]
    assert layer["host"] == "Cap" and layer["grown_above_m"] == pytest.approx(1e-8)
    (row,) = result.report["thin_layer_interfaces"]
    assert row["R_applied_m2K_W"] == pytest.approx(1e-8 / 1.4 - 1e-8 / 390)
    assert row["area_m2"] == pytest.approx(16e-6, rel=1e-9)
    faces = _boundary(result.case, "Base")
    assert faces["Base_to_Cap"] > 0 and faces["Base_to_Cap_via_Film"] == row["faces"]


def test_meshing_every_layer_keeps_the_film_as_cells(tmp_path):
    result = _mesh(tmp_path, _stack(1e-8), thin_layers=False)
    rep = result.report
    assert "Film" in rep["solid_regions"] and not rep["thin_layer_interfaces"]
    thin = next(r for r in rep["thin_layers"] if r["part"] == "Film")
    assert thin["thickness_m"] == pytest.approx(1e-8, rel=1e-6) and thin["cells_across"] >= 2


@pytest.mark.parametrize("off", [0.05, 5.0])
def test_a_meshed_hundred_nanometre_film_far_from_the_origin_stays_apart_in_the_viewer(
        tmp_path, off):
    """float32 holds 7 digits: at 5 m a drawn coordinate moves in 0.5 um steps, so the film's two
    faces would land on one plane. The viewer draws relative to the model's own centre."""
    result = _mesh(tmp_path, _stack(1e-7, off, domain=True, heated_film=True))
    assert "Film" in result.report["solid_regions"]
    case = result.case
    with np.load(case / VIEWER_NAME) as z:
        names = z["names"].tolist()
        k = names.index("Film")
        pts = z[f"p{k}"]
        origin = z["origin"]
    assert pts.dtype == np.float32
    assert float(pts[:, 2].max() - pts[:, 2].min()) == pytest.approx(1e-7, rel=1e-3)
    assert np.allclose(origin, off + np.array([0.005, 0.005, 0.0005]), atol=0.003)
    resp = snapgrid_viewer(case, roles={}, units="m")
    assert resp["origin_m"] == pytest.approx(list(origin))


def test_results_say_a_film_in_nanometres_and_list_the_resistances(tmp_path):
    meshed = _mesh(tmp_path / "a", _stack(1e-7, domain=True, heated_film=True))
    rows = R.part_rows(meshed.report, meshed.sidecar)
    film = next(r for r in rows if r["name"] == "Film")
    assert film["thinnest_text"] == "100 nm" and film["thinnest_mm"] == pytest.approx(1e-4)
    assert "100 nm" in " ".join(R.quality_lines(meshed.report, rows))
    resist = _mesh(tmp_path / "b", _stack(1e-8))
    (layer,) = R.thin_layer_rows(resist.report)
    assert layer["thickness_text"] == "10 nm" and layer["patches"] == ["Base_to_Cap_via_Film"]
    assert any("thin-layer thermal resistance" in q
               for q in R.quality_lines(resist.report, R.part_rows(resist.report, resist.sidecar)))
