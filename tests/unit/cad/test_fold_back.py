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


def _facts(shape):
    from meshpipeline.cad import suspect_defects as SD
    from meshpipeline.cad.fea_measure import FACT_KEY, measure_cad

    rec = measure_cad(shape, mm_per_unit=1.0)
    rec["suspects"] = SD.find(shape, rec, check_overlaps=False)
    return {FACT_KEY: rec, "solids": 1, "source_format": "step"}


def test_a_fold_is_a_warning_on_the_stage_and_names_its_faces():
    """MESH-1: a readable solid is meshed; the stage warns, it never stops."""
    from meshpipeline.application.fea_check import TRY_ANYWAY, blocker, warnings

    facts = _facts(fx.wedge(0.2))
    assert blocker(facts) == ("", "", "")
    said = " ".join(warnings(facts))
    assert "paper-thin fold" in said and "Face_" in said and TRY_ANYWAY in said
    assert warnings(_facts(fx.wedge(20.0))) == []


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


def test_a_real_fold_is_found_and_confirmed_by_two_measures():
    """fw_hinge_r: two faces on top of each other beside an edge (0.00 mm at 1 mm in) - and OpenCASCADE's
    Boolean self-interference check on that solid names them too: confirmed (MESH-1)."""
    from meshpipeline.cad import suspect_defects as SD
    from meshpipeline.cad.scout import read_cad

    shape = read_cad(_FIX / "fw_hinge_r.stp")
    res = fold_back_edges(shape)
    assert len(res["folds"]) == 1 and res["folds"][0]["gap_mm"] < 0.025
    rec = {"fold_back": [{**f, "faces": [f"Face_{k}" for k in f["faces"]]} for f in res["folds"]]}
    sus = SD.find(shape, rec, check_overlaps=False)
    assert len(sus) == 1 and sus[0]["kind"] == "fold" and sus[0]["confirmed"] is True


def test_a_knife_wedge_is_a_fold_and_its_gap_is_tiny():
    f = fold_back_edges(fx.wedge(0.2))["folds"]
    assert f and f[0]["gap_mm"] is not None and f[0]["gap_mm"] < 0.01
