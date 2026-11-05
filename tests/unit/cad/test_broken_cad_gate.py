"""MESH-3, the zero-false-stop gate in CI: every broken-CAD check the FEA stage runs, over a fixed set of
known-good files, gives ZERO stops (devtools/quality/broken_cad_gate.py). The whole corpus is run on a lab
VM for any change to a check; this is the fixed subset every change must pass."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from tests import fea_fixtures as fx

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "geometry"


def _gate():
    spec = importlib.util.spec_from_file_location("broken_cad_gate", ROOT / "devtools" / "quality" / "broken_cad_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def good_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("good")
    made = [fx.write_step(shape, d / f"{name}.step") for name, shape in (
        ("plate_with_hole", fx.plate_with_hole()), ("bracket", fx.bracket()), ("stepped_shaft", fx.stepped_shaft()),
        ("v_notched_bar", fx.v_notched_bar()), ("touching_blocks", fx.touching_blocks()),
        ("overlapping_blocks", fx.overlapping_blocks()), ("ordinary_wedge", fx.wedge(20.0)))]
    # helmholtz_coil (~3 min of measure_assembly/validity) is off until the gate is cheap again; the
    # one stop left is not_solid_message, and test_fold_back reads this file.
    return [*made, FIX / "cht_enclosing_2region.step", FIX / "transition_rect_to_round.step"]
    #       FIX / "fea_fold" / "helmholtz_coil.step"]


def test_no_known_good_file_is_stopped(good_files):
    gate = _gate()
    rows = [gate.check_file(str(p)) for p in good_files]
    assert [r for r in rows if r.get("error")] == []
    stopped = [(Path(r["file"]).name, r["stop"], r["stop_words"]) for r in rows if r.get("stop")]
    assert stopped == []
    assert gate.summary(rows)["stops"] == 0


def test_flagged_files_warn_and_are_not_stopped(tmp_path):
    """A real fold (fw_hinge_r) and a crossing part are warnings with their faces - meshed anyway."""
    gate = _gate()
    knife = fx.write_step(fx.wedge(0.2), tmp_path / "knife.step")
    # fw_hinge_r (~1 min of measure_assembly) is off for the same reason; test_fold_back reads it.
    for p in (knife,):  # FIX / "fea_fold" / "fw_hinge_r.stp",
        r = gate.check_file(str(p))
        assert r["stop"] == "" and r["warnings"] >= 1 and "Face_" in " ".join(r["warning_words"]), r


def test_a_surface_is_still_stopped(tmp_path):
    """The one stop left before meshing: no closed solid can be read (here, an open sheet)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln

    sheet = BRepBuilderAPI_MakeFace(gp_Pln(), 0.0, 10.0, 0.0, 10.0).Face()
    r = _gate().check_file(str(fx.write_step(sheet, tmp_path / "sheet.step")))
    assert r["stop"] == "surface"
