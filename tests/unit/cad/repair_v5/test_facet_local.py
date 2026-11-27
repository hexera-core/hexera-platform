"""facet_crossing_local (CRS v5 round 2): the triangle sweep, the relation, the cause kind -> rule seed, the class
rule and the loop's row. Pure: no OCCT, no gmsh."""

from __future__ import annotations

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v5 import facet_local as FL
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5.diagnose import PRIORITY, order

T1 = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))


def test_the_sweep_counts_a_fold_a_duplicate_and_a_true_crossing_but_not_neighbours() -> None:
    flat = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.5, -1.0, 0.0))        # across the shared edge: a neighbour
    fold = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.3, 0.5, 0.0))         # same side of the shared edge: folded
    square = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))       # a right-angle edge
    pierce = ((0.2, 0.2, -1.0), (0.2, 0.2, 1.0), (0.3, 0.25, 0.0))     # through T1, no shared node
    assert FL.self_crossings([T1, flat], [(1, 2, 3), (1, 2, 4)], [0, 0])[0] == {}
    assert FL.self_crossings([T1, square], [(1, 2, 3), (1, 2, 4)], [0, 1])[0] == {}
    assert FL.self_crossings([T1, fold], [(1, 2, 3), (1, 2, 4)], [0, 1])[0] == {(0, 1): 1}
    assert FL.self_crossings([T1, T1], [(1, 2, 3), (1, 2, 3)], [2, 1])[0] == {(1, 2): 1}
    assert FL.self_crossings([T1, pierce], [(1, 2, 3), (4, 5, 6)], [3, 3])[0] == {(3, 3): 1}
    # neither triangle on a focus owner: not counted
    assert FL.self_crossings([T1, pierce], [(1, 2, 3), (4, 5, 6)], [3, 3], focus={7})[0] == {}


def test_relation() -> None:
    kw = {"shared_edge": False, "shared_vertex": False, "gap_mm": 1.0, "tol_mm": 1e-3}
    assert FL.relation_of(4, 4, **kw) == "same_face"
    assert FL.relation_of(4, 5, **{**kw, "shared_edge": True}) == "adjacent"
    assert FL.relation_of(4, 5, **{**kw, "shared_vertex": True}) == "touching"
    assert FL.relation_of(4, 5, **{**kw, "gap_mm": 1e-4}) == "touching"
    assert FL.relation_of(4, 5, **kw) == "apart"


def test_the_cause_picks_the_rule_seed() -> None:
    h = 10.0
    cyl = {"type": "Cylinder", "closed": False, "deg_per_h": 120.0, "span_deg": 180.0, "width_mm": 8.0}
    assert FL.cause_kind({"a": "F1", "b": "F1", "relation": "same_face"}, {"F1": cyl}, h) == ("curved_face", ["F1"])
    closed = {**cyl, "closed": True}
    assert FL.cause_kind({"a": "F1", "b": "F1", "relation": "same_face"}, {"F1": closed}, h)[0] == "closed_face"
    spline = {"type": "BSplineSurface", "canonical_gap_mm": 1e-4, "width_mm": 8.0}
    assert FL.cause_kind({"a": "F1", "b": "F2", "relation": "adjacent", "edge_angle_deg": 90.0},
                         {"F1": cyl, "F2": spline}, h) == ("spline_analytic", ["F2"])
    assert FL.cause_kind({"a": "F1", "b": "F2", "relation": "adjacent", "edge_angle_deg": 2.0},
                         {"F1": cyl, "F2": cyl}, h)[0] == "near_tangent_edge"
    assert FL.cause_kind({"a": "F1", "b": "F2", "relation": "adjacent", "edge_angle_deg": 178.0}, {}, h)[0] == "knife_edge"
    strip = {"type": "Plane", "width_mm": 0.5}
    assert FL.cause_kind({"a": "F3", "b": "F3", "relation": "same_face"}, {"F3": strip}, h) == ("sliver", ["F3"])
    # a narrow CURVED face that is not long (a small hole) is curved, not a sliver
    hole = {"type": "Cylinder", "closed": True, "width_mm": 0.7, "aspect": 3.0, "deg_per_h": 900.0}
    assert FL.face_kind(hole, h) == "closed_face"
    assert FL.cause_kind({"a": "F1", "b": "F2", "relation": "touching"}, {}, h) == ("touching", ["F1", "F2"])
    assert FL.cause_kind({"a": "F4", "b": "F4", "relation": "same_face"}, {"F4": {"type": "Plane"}}, h)[0] is None
    # every kind has its row, and the row's seed is the cause's repair
    assert set(FL.KIND_REPAIRS) >= {"curved_face", "closed_face", "c1_break", "bad_param", "spline_analytic",
                                    "near_tangent_edge", "knife_edge", "sliver", "touching"}
    assert FL.KIND_REPAIRS["spline_analytic"][0] == ("to_canonical",)
    assert FL.KIND_REPAIRS["bad_param"][0] == ("reapproximate",)
    assert FL.KIND_REPAIRS["c1_break"][0] == ("split_continuity",)
    # fix G: the fine angular split leads the curvature rows, the knife cut the knife row
    assert FL.KIND_REPAIRS["near_tangent_edge"][0] == ("split_turn",)
    assert FL.KIND_REPAIRS["closed_face"][0] == FL.KIND_REPAIRS["curved_face"][0] == ("split_turn",)
    assert FL.KIND_REPAIRS["knife_edge"][:2] == (("cut_knife",), ("split_turn",))
    assert all(("defeature",) in row for row in FL.KIND_REPAIRS.values())


def _rows() -> list[dict]:
    return [{"a": "F1", "b": "F1", "relation": "same_face", "by_member": {"gmsh_prod": 9, "gmsh_default": 0}},
            {"a": "F2", "b": "F3", "relation": "adjacent", "edge_angle_deg": 90.0,
             "by_member": {"gmsh_prod": 0, "gmsh_default": 4}},
            {"a": "F5", "b": "F6", "relation": "apart", "by_member": {"gmsh_prod": 50, "gmsh_default": 50}}]


def test_the_cause_is_the_failing_members_worst_local_pair_with_measured_parameters() -> None:
    faces = {"F1": {"type": "Cylinder", "closed": False, "deg_per_h": 200.0, "span_deg": 120.0, "width_mm": 9.0},
             "F2": {"type": "BSplineSurface", "c1_breaks": 2, "c0_breaks": 0, "degree": 3, "width_mm": 9.0}}
    cause = FL.local_cause(_rows(), faces, h_mm=10.0, member="gmsh_default", diag_mm=100.0)
    assert cause is not None and cause["pair"] == ["F2", "F3"] and cause["kind"] == "c1_break"
    assert cause["aim"] == ["F2"] and cause["op_params"]["split_continuity"] == {"order": 2}
    assert cause["predicted_before"] == {"gmsh_default": 4, "gmsh_prod": 9}      # the apart pair is close_faces'
    prod = FL.local_cause(_rows(), faces, h_mm=10.0, member="gmsh_prod", diag_mm=100.0)
    assert prod is not None and prod["kind"] == "curved_face" and prod["op_params"]["split_angle"] == {"max_deg": 60.0}
    only_apart = [r for r in _rows() if r["relation"] == "apart"]
    assert FL.local_cause(only_apart, faces, h_mm=10.0, member=None, diag_mm=100.0) is None


def _mesher(cls: str | None, code: str = "overlapping_facets", **cause: object) -> Defect:
    return Defect("S@F1", "mesher", code, ("F1",), member="gmsh_default", cls=cls, cause=dict(cause), rule="r")


def test_the_class_takes_unconfirmed_crossings_only_and_keys_the_row_by_cause() -> None:
    cause = FL.local_cause(_rows(), {}, h_mm=10.0, member="gmsh_default", diag_mm=100.0)
    for taken in (_mesher("unknown", "other"), _mesher("surface_crossing", "edge_through_face"),
                  _mesher("close_faces", crossing_pairs=0), _mesher("checker_failed", "gmsh_crash")):
        got = FL.classify_local(taken, cause)
        assert got.cls == FL.CLASS and got.cause["was"] == taken.cls, taken
    for kept in (_mesher("close_faces", crossing_pairs=2), _mesher("low_quality", "low_quality"),
                 _mesher("close_faces", crossing_pairs=2, gap_mm=0.006),
                 Defect("V", "validity", "self_intersection", ("B1",), cls="overlap_self_intersection")):
        assert FL.classify_local(kept, cause) is kept
    assert FL.classify_local(_mesher("unknown", "other"), None).cls == "unknown"
    local = FL.classify_local(_mesher("unknown", "other"), cause)
    assert loop.table_key(local) == "facet_crossing_local" and local.cause["kind"] == "unexplained"
    knife = FL.classify_local(_mesher("unknown", "other"), {**cause, "kind": "knife_edge"})
    assert loop.table_key(knife) == "facet_crossing_local:knife_edge"
    assert loop.CLASS_REPAIRS[loop.table_key(knife)][0] == ("cut_knife",)
    # the class is on the list, after close_faces and before surface_crossing
    assert PRIORITY.index("close_faces") < PRIORITY.index(FL.CLASS) < PRIORITY.index("surface_crossing")
    assert [d.cls for d in order([_mesher("surface_crossing"), local])] == [FL.CLASS, "surface_crossing"]


def test_the_cause_parameters_reach_the_op() -> None:
    d = _mesher(FL.CLASS, kind="curved_face", aim=["F1"], op_params={"split_angle": {"max_deg": 45.0}})
    assert seq.params_for("split_angle", d) == (["F1"], {"max_deg": 45.0})
    assert seq.params_for("reapproximate", d)[1] == seq.DEFAULT_PARAMS["reapproximate"]


def test_split_turn_angle_is_the_sag_of_the_local_feature_and_aims_every_curved_face() -> None:
    # a tube (f = R): 28.7 deg, snapped down to 90/4; a 20 deg lens arc: half its span (two pieces at least)
    assert FL.turn_deg({"min_radius_mm": 2.83, "width_mm": 16.7}) == 22.5
    assert FL.turn_deg({"min_radius_mm": 5.61, "width_mm": 1.9, "span_deg": 20.0}) == 10.0
    assert FL.turn_deg({"type": "Plane"}) is None
    assert FL.turn_deg({"min_radius_mm": 1.0, "width_mm": 1e-6}) == FL.TURN_DEG_MIN
    rows = [{"a": "F1", "b": "F2", "relation": "adjacent", "edge_angle_deg": 1.0, "by_member": {"gmsh_default": 3}},
            {"a": "F2", "b": "F7", "relation": "adjacent", "edge_angle_deg": 60.0, "by_member": {"gmsh_default": 1}}]
    pipe = {"type": "Cylinder", "closed": False, "min_radius_mm": 2.54, "deg_per_h": 900.0, "span_deg": 180.0,
            "width_mm": 5.4}
    faces = {"F1": pipe, "F2": pipe, "F7": {**pipe, "span_deg": 90.0}}
    cause = FL.local_cause(rows, faces, h_mm=40.9, member="gmsh_default", diag_mm=400.0)
    assert cause is not None and cause["kind"] == "near_tangent_edge"
    assert cause["turn_faces"] == ["F2", "F1", "F7"] and cause["op_params"]["split_turn"] == {"max_deg": 22.5}
    assert "cut_knife" not in cause["op_params"]
    d = _mesher(FL.CLASS, **cause)
    assert seq.params_for("split_turn", d) == (["F2", "F1", "F7"], {"max_deg": 22.5})
    assert seq.params_for("split_turn", d, live={"F7"})[0] == ["F7"]      # a gone face is not aimed


def test_cut_knife_is_sized_by_the_fidelity_bound_and_half_an_element() -> None:
    rows = [{"a": "F13", "b": "F14", "relation": "adjacent", "edge_angle_deg": 179.9,
             "by_member": {"gmsh_default": 2}}]
    flat = {"type": "Plane", "width_mm": 3.0, "aspect": 5.0}
    cause = FL.local_cause(rows, {"F13": flat, "F14": flat}, h_mm=14.5, member="gmsh_default", diag_mm=145.0)
    assert cause is not None and cause["kind"] == "knife_edge" and cause["turn_faces"] == []
    assert cause["op_params"]["cut_knife"] == {"width_mm": 0.1, "max_reach_mm": 7.25}
    d = _mesher(FL.CLASS, **cause)
    assert seq.params_for("cut_knife", d) == (["F13", "F14"], {"width_mm": 0.1, "max_reach_mm": 7.25})
    assert seq.aim_reason("cut_knife", d) is None
    try:
        seq.params_for("split_turn", d)
    except seq.NoParameters:
        pass
    else:
        raise AssertionError("split_turn has no measured angle on two planes")


def test_the_new_ops_are_v5_class_only_and_bounded() -> None:
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    from meshpipeline.cad.repair_v4.proposer import offered_ops

    for name in ("split_turn", "cut_knife"):
        assert getattr(REGISTRY[name], "v5_class_only", False) and name not in offered_ops()
    # below split_angle's 30 deg floor, never unbounded
    assert REGISTRY["split_turn"].params_schema["properties"]["max_deg"]["minimum"] < 30
    assert REGISTRY["cut_knife"].params_schema["properties"]["width_mm"]["maximum"] == 0.1
