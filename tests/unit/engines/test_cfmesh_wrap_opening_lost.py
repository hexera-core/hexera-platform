# Responsibility: Pin LAD-cfd-14 - on the wrap rung, an opening the wrap could only place on a wall farther than its own size from
# where it is drawn, at a place the user stated, ends the attempt once in words that name it - never a port binding the builder
# repeats (abc 99768: outlet_3 alone in its passage, placed 42.2 mm away; configure refused 59 times on the location hint).
from __future__ import annotations

from meshpipeline.engines.cfmesh import cfmesh_runner as R
from meshpipeline.engines.preflight import read_refusal

DECL = [{"name": "outlet_1", "type": "inlet", "diameter_mm": 21.58, "near_mm": [0.0, 0.01, -26.03]},
        {"name": "outlet_3", "type": "outlet", "diameter_mm": 13.82, "near_mm": [0.0, 63.21, -15.24]},
        {"name": "wall", "type": "wall"}]


def _wrapped(moved_m: float, centre=(0.0, 0.06321, -0.01524)) -> dict:
    return {"wrapped": True, "facts": {"wrap": {"openings_moved": [
        {"opening": "outlet_1", "moved_m": moved_m, "centre_m": [0.0, 0.0255, 0.006],
         "drawn_centre_m": list(centre), "drawn_area_m2": 1.5025e-4}]}}}


def test_an_opening_placed_far_from_where_the_user_put_it_ends_the_attempt_naming_it(tmp_path):
    out = R.wrap_opening_lost(tmp_path, _wrapped(0.0422), DECL)
    assert out is not None and out["success"] is False
    assert "outlet_3" in out["error"] and "42.2 mm" in out["error"] and "13.8 mm" in out["error"]
    assert "do NOT reconfigure" in out["next"]
    ref = read_refusal(tmp_path)
    assert ref is not None and ref.gate == "staging" and ref.facts["rule"] == "LAD-cfd-14"
    assert ref.facts["deterministic"] is True and "outlet_3" in ref.facts["reason"]
    # a configure called again says it again, without wrapping the same fluid a second time (2.7 min on 99768)
    again = R.opening_lost_said(tmp_path)
    assert again is not None and again["error"] == out["error"] and again["success"] is False


def test_a_workspace_with_no_such_record_stages_as_before(tmp_path):
    assert R.opening_lost_said(tmp_path) is None


def test_a_placement_within_its_own_size_or_with_no_stated_place_stands_as_before(tmp_path):
    assert R.wrap_opening_lost(tmp_path, _wrapped(0.005), DECL) is None              # within 13.8 mm: the same opening
    assert R.wrap_opening_lost(tmp_path, _wrapped(0.0422, centre=(0.05, 0.0, 0.0)), DECL) is None  # no location there
    assert R.wrap_opening_lost(tmp_path, {**_wrapped(0.0422), "wrapped": False}, DECL) is None    # not the wrap rung
    assert read_refusal(tmp_path) is None
