# Responsibility: Verify MESH-24: a CAD edge far shorter (a face far narrower) than the elements beside it gets elements of its own size there, within a budget - the slivers it pinned are gone, the record says what was sized, and a part with nothing short meshes to the same bytes.
# Boundaries: small parts built in the test (tests/fea_fixtures.py), planned and meshed in-process; the corpus counts are the lab's (demo dry run 2 Nov, ~/demofix_lab).
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter as A  # noqa: E402
from meshpipeline.engines.gmsh import fea_short as SHORT  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    """The upload read in mm, written in metres (STEP and gmsh's B-rep), planned as the driver plans it."""
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    record, surface, diag = FD.measure_staged(ws)
    FD.write_plan(ws, FD.make_plan(record, surface, diag))
    return ws


def _copy(src: Path, dst: Path) -> Path:
    import shutil

    shutil.copytree(src, dst)
    return dst


def _q(ws: Path) -> dict:
    return json.loads((ws / "quality.json").read_text())


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------- MESH-24 ----
@pytest.fixture(scope="module")
def runout(tmp_path_factory) -> tuple[Path, Path]:
    """The runout block meshed with MESH-24 and, for comparison, with its short edges as drawn."""
    ws = _stage(fx.runout_block(), tmp_path_factory.mktemp("runout") / "ws")
    on = _copy(ws, tmp_path_factory.mktemp("runout_on") / "ws")
    assert A.mesh(on) == 0
    off = _copy(ws, tmp_path_factory.mktemp("runout_off") / "ws")
    keep = SHORT.SHORT_SHARE
    SHORT.SHORT_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        SHORT.SHORT_SHARE = keep
    return on, off


def test_a_short_edge_gets_elements_of_its_own_length_and_the_slivers_go(runout):
    on, off = runout
    q_on, q_off = _q(on), _q(off)
    rec = q_on["fea_short_edges"]
    # the two 0.02 mm edges and the 0.02 mm wide face between them
    assert rec["rule"] == "MESH-24" and rec["edges"] == 2 and rec["faces"] == 1 and rec["left"] == 0
    assert rec["narrowest_mm"] == pytest.approx(0.02, rel=1e-2)
    assert rec["shortest_mm"] == pytest.approx(0.02, rel=1e-3)
    assert "fea_short_edges" not in q_off
    # sized, the elements at the short edges are well shaped: every figure better than as drawn
    assert q_on["fea_max_aspect_ratio"] < q_off["fea_max_aspect_ratio"] and q_on["fea_max_aspect_ratio"] <= 20
    assert q_on["fea_min_dihedral_deg"] > q_off["fea_min_dihedral_deg"] and q_on["fea_min_dihedral_deg"] >= 5
    assert q_on["fea_max_dihedral_deg"] <= 170 and q_on["fea_min_scaled_jacobian"] > q_off["fea_min_scaled_jacobian"]
    assert not q_on["fatal"]


def test_the_added_elements_stay_within_the_budget(runout, monkeypatch):
    on, _off = runout
    plan = json.loads((on / "fea_plan.json").read_text())
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(on / "geometry.brep"))
        gmsh.model.occ.synchronize()
        full = SHORT.plan_short(gmsh, plan)
        assert len(full["edges"]) == 3 and full["added"] <= full["budget"]
        monkeypatch.setattr(SHORT, "BUDGET_FLOOR", 0)
        monkeypatch.setattr(SHORT, "BUDGET_SHARE", 0.0)
        none = SHORT.plan_short(gmsh, plan)
        assert none["edges"] == [] and none["left"] == 3
    finally:
        gmsh.finalize()
    # a finer edge field adds more elements, and none where the base already is the edge's size
    assert SHORT.added_elements(1e-5, 1e-3, 1.3, 4.5, 1e-5) > SHORT.added_elements(1e-4, 1e-3, 1.3, 4.5, 1e-4) > 0
    assert SHORT.added_elements(1e-3, 1e-3, 1.3, 4.5, 1e-3) == 0


def test_a_part_with_no_short_edge_meshes_to_the_same_bytes(tmp_path):
    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    off = _copy(ws, tmp_path / "off")
    assert A.mesh(ws) == 0
    keep = SHORT.SHORT_SHARE
    SHORT.SHORT_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        SHORT.SHORT_SHARE = keep
    assert "fea_short_edges" not in _q(ws)
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        assert _sha(ws / name) == _sha(off / name)

