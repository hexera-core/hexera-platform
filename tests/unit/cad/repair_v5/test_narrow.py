"""v5 round 2 fix E: the narrow-region detector aims a low_quality defect at its worst elements (pure halves)."""

from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult
from meshpipeline.cad.repair_v5 import loop as L
from meshpipeline.cad.repair_v5 import narrow as N

REV = {("F", 1): "F1", ("F", 2): "F2", ("F", 3): "F3", ("E", 7): "E7", ("E", 8): "E8", ("E", 9): "E9"}
H = 10.0  # e = 1 mm


def _defect(**cause) -> Defect:
    return Defect(id="S@F1", kind="mesher", code="low_quality", entities=("F1", "F2", "F3"), member="gmsh_prod",
                  cls="low_quality", rule="low_quality at site S@F1; measured small_face: face F3 ...",
                  cause={"kind": "small_face", "aim": ["F3"], "neighbour": "F9", "h_mm": H, **cause})


def test_worst_points_are_the_members_node_points_only():
    site = SimpleNamespace(graph={"points": [
        {"xyz": [1, 2, 3], "mesher": "gmsh_prod", "tag": "nodes 1,2,3,4"},
        {"xyz": [9, 9, 9], "mesher": "gmsh_alt", "tag": "nodes 5,6,7,8"},
        {"xyz": [5, 5, 5], "mesher": "gmsh_prod", "tag": "printed point"},
        {"xyz": None, "mesher": "gmsh_prod", "tag": "nodes 1"}]})
    assert N.worst_points(site, "gmsh_prod") == [(1.0, 2.0, 3.0)]


def test_the_nearest_candidate_sets_the_kind_and_its_kind_is_aimed_nearest_first():
    facts = {"e_mm": 1.0, "points": [{
        "faces": [{"face": 1, "dist_mm": 0.0, "area_mm2": 50.0, "width_mm": 0.3, "rs_width_mm": 0.4, "solid": 1,
                   "neighbours": [{"face": 2, "shared_mm": 5.0, "dihedral_deg": 2.0}]},
                  {"face": 3, "dist_mm": 2.5, "area_mm2": 0.1, "width_mm": 0.1, "rs_width_mm": 0.2}],
        "edges": [{"edge": 7, "dist_mm": 0.4, "length_mm": 0.5, "faces": [1, 2]}],
        "edge_pairs": [], "face_pairs": []}]}
    region = N.region_of(facts, REV, h_mm=H)
    assert region is not None and region.kind == "sliver" and region.aim == ("F1",)
    assert region.params["neighbour"] == "F2" and region.params["max_dev_mm"] == 1.0
    # the globally smallest face (F3) is farther from the worst element: it is not the cause
    assert "F3" not in region.aim


def test_a_narrow_edge_pair_aims_the_face_both_edges_bound_plus_the_short_edges():
    facts = {"e_mm": 1.0, "points": [{
        "faces": [{"face": 2, "dist_mm": 0.2, "area_mm2": 40.0, "width_mm": 3.0, "rs_width_mm": 4.0}],
        "edges": [{"edge": 8, "dist_mm": 0.1, "length_mm": 9.0, "faces": [2]},
                  {"edge": 9, "dist_mm": 0.3, "length_mm": 9.0, "faces": [2]},
                  {"edge": 7, "dist_mm": 2.0, "length_mm": 0.6, "faces": [2, 3]}],
        "edge_pairs": [{"a": 8, "b": 9, "gap_mm": 0.2, "faces_a": [2], "faces_b": [2], "common": [2]}],
        "face_pairs": []}]}
    region = N.region_of(facts, REV, h_mm=H)
    assert region is not None and region.kind == N.NARROW
    assert region.aim == ("F2", "E7")
    assert region.params["feature_deg"] == 180.0 and region.params["max_length_mm"] > 0.6


def test_nothing_near_the_worst_elements_is_no_region():
    facts = {"e_mm": 1.0, "points": [{"faces": [{"face": 1, "dist_mm": 0.1, "area_mm2": 99.0, "width_mm": 5.0,
                                                 "rs_width_mm": 6.0}], "edges": [], "edge_pairs": [],
                                      "face_pairs": []}]}
    assert N.region_of(facts, REV, h_mm=H) is None
    assert N.region_of(None, REV, h_mm=H) is None


def test_attach_replaces_the_old_fault_wholesale():
    region = N.Region("small_edge", ("E7",), {"max_length_mm": 0.6, "max_width_mm": 0.6}, "an edge", 0.1,
                      {"e_mm": 1.0})
    (out,) = N.attach_regions([_defect()], {"S@F1": ([(0, 0, 0)], region)})
    assert out.cause["kind"] == "small_edge" and out.cause["aim"] == ["E7"]
    assert "neighbour" not in out.cause and out.cause["worst_points"] == [[0, 0, 0]]
    assert L.table_key(out) == "low_quality:small_edge"
    (kept,) = N.attach_regions([_defect()], {"S@F1": ([(0, 0, 0)], None)})
    assert kept.cause["aim"] == ["F3"] and "nothing small" in kept.rule


def _detail(**kw):
    return {"member_status": "fail", "member_error_kind": "low_quality", "region_passed_before": False,
            "member_min_sicn": 0.005, "region_min_sicn_before": 0.001, "member_points": [[50.0, 0, 0]],
            "region_points_before": [[0.0, 0, 0]], **kw}


def test_moved_needs_a_better_sicn_and_the_worst_elements_elsewhere():
    d = _defect(worst_points=[[0, 0, 0]], fault={"e_mm": 1.0})
    assert N.moved(d, _detail())
    assert N.moved(d, _detail(member_min_sicn=0.0005)) is None          # worse
    assert N.moved(d, _detail(member_points=[[1.0, 0, 0]])) is None    # still there (within 2 e)
    assert N.moved(d, _detail(region_passed_before=True)) is None
    assert N.moved(d, _detail(member_error_kind="overlapping_facets")) is None


def test_moving_turns_a_moved_defect_gone_and_says_so():
    d = _defect(worst_points=[[0, 0, 0]], fault={"e_mm": 1.0})

    def base(pool, before, after, defect, touched=None, **kw):
        return LocalResult(False, [], _detail(), 1.0)

    local = L.moving(base)(None, None, None, d)
    assert local.gone and "moved" in local.detail["moved"] and "moved" in local.line()
    other = Defect(id="S@F1", kind="mesher", code="overlapping_facets", entities=("F1",), member="gmsh_default",
                   cls="close_faces")
    assert not L.moving(base)(None, None, None, other).gone
