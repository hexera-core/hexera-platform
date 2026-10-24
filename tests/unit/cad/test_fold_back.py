"""cad/fold_back.py: two faces lying on each other along an edge (car_vs_pole, 24 Oct) are found and
the FEA stage stops on them with the repair way out; a tangent blend or an ordinary wedge never is."""
from __future__ import annotations

from tests import fea_fixtures as fx

from meshpipeline.cad.fold_back import fold_back_edges


def test_a_knife_edge_of_almost_no_angle_is_a_fold():
    folds = fold_back_edges(fx.wedge(0.2))["folds"]
    assert len(folds) == 1 and folds[0]["body"] == 1 and folds[0]["opening_deg"] < 1.0
    assert abs(folds[0]["length_mm"] - 20.0) < 1e-6 and len(folds[0]["faces"]) == 2


def test_an_ordinary_wedge_a_box_and_tangent_blends_are_not():
    assert fold_back_edges(fx.wedge(20.0))["folds"] == []
    assert fold_back_edges(fx._box(0, 0, 0, 10, 20, 30))["folds"] == []
    assert fold_back_edges(fx.bracket(fillet=4.0))["folds"] == []          # fillets meet their faces tangentially
    assert fold_back_edges(fx.plate_with_hole())["folds"] == []


def test_the_stage_stops_on_a_fold_and_names_it():
    from meshpipeline.application.fea_check import WAY_OUT_REPAIR, blocker
    from meshpipeline.cad.fea_measure import FACT_KEY, measure_cad

    rec = measure_cad(fx.wedge(0.2), mm_per_unit=1.0)
    assert rec["fold_back"] and rec["fold_back"][0]["faces"][0].startswith("Face_")
    said, kind, way = blocker({FACT_KEY: rec, "solids": 1})
    assert kind == "fold_back" and way == WAY_OUT_REPAIR
    assert "paper-thin fold" in said and "CAD repair" in said and "20 mm" in said
    clean = measure_cad(fx.wedge(20.0), mm_per_unit=1.0)
    assert blocker({FACT_KEY: clean, "solids": 1})[1] == ""


def test_no_mesh_built_is_said_as_that_never_as_bad_elements():
    from meshpipeline.contracts.failure_cause import _fea_quality
    from meshpipeline.engines.fea_driver import ladder_words

    passes = [{"pass": 1, "built": False, "bad_surfaces": [34, 35]}, {"pass": 2, "built": False, "bad_surfaces": []}]
    assert "built a mesh - the fill could not start" in ladder_words(passes, [{"lever": "LAD-fea-2"}])
    what, nxt = _fea_quality({"fea": True, "built_none": True, "passes": 2})
    assert "could not fill this part" in what and "degenerate" not in what and "sliver or fold" in nxt


# ------------------------------------------- on top of each other, measured (24 Oct, real assemblies) ----
_FIX = __import__("pathlib").Path(__file__).resolve().parents[2] / "fixtures" / "geometry" / "fea_fold"


def test_a_plane_meeting_a_cylinder_tangentially_is_a_cusp_not_a_fold():
    """helmholtz_coil: opposite normals at 8 edges, but the gap opens at once - a tangent cusp, which the
    plan sets aside (PLAN-fea-10); the stage goes on."""
    from meshpipeline.cad.scout import read_cad

    res = fold_back_edges(read_cad(_FIX / "helmholtz_coil.step"))
    assert res["folds"] == [] and len(res["cusps"]) == 8
    assert all(c["gap_mm"] > 0.025 for c in res["cusps"])


def test_a_real_fold_still_stops_the_stage():
    """fw_hinge_r: two faces on top of each other beside an edge (0.00 mm at 1 mm in)."""
    from meshpipeline.application.fea_check import blocker
    from meshpipeline.cad.fea_measure import FACT_KEY
    from meshpipeline.cad.scout import read_cad

    res = fold_back_edges(read_cad(_FIX / "fw_hinge_r.stp"))
    assert len(res["folds"]) == 1 and res["folds"][0]["gap_mm"] < 0.025
    rec = {"fold_back": [{**f, "faces": [f"Face_{k}" for k in f["faces"]]} for f in res["folds"]]}
    assert blocker({FACT_KEY: rec, "solids": 1})[1] == "fold_back"


def test_a_knife_wedge_is_a_fold_and_its_gap_is_tiny():
    f = fold_back_edges(fx.wedge(0.2))["folds"]
    assert f and f[0]["gap_mm"] is not None and f[0]["gap_mm"] < 0.01
