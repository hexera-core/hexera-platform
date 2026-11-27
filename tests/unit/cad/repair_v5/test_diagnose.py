"""v5 diagnosis: the class rule on hand-built sites, and the AI description on a fake route. No kernel, no model."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.seq import CHECKER_FAILED
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.verdict.valid import HARD_CODES, SOFT_CODES
from meshpipeline.cad.repair_v5.describe_ai import describe_unknown
from meshpipeline.cad.repair_v5.diagnose import PRIORITY, classify_site, facet_sag_mm, order
from meshpipeline.cad.repair_v5.loop import CLASS_REPAIRS, table_key


def _site(kind: str, failures: list[dict], entities=("F1", "F2"), **graph) -> Site:
    return Site("s1", kind, list(entities), [(0.0, 0.0, 0.0)], sorted({f["mesher"] for f in failures}), None,
                {"failures": failures, **graph}, None)


def _fail(kind: str, mesher: str = "gmsh_default") -> dict:
    return {"mesher": mesher, "error_kind": kind, "tag": "surface 3", "message": ""}


def _defect(i: str, cls: str | None, kind: str = "mesher", code: str = "x") -> Defect:
    return Defect(i, kind, code, (), cls=cls)  # type: ignore[arg-type]


SHAFT_IN_BORE = {"a": "F1", "b": "F2", "gap_mm": 0.127, "surface_a": "cylinder", "surface_b": "cylinder",
                 "radius_a_mm": 3.048, "radius_b_mm": 3.175, "axis_angle_deg": 0.0, "axis_offset_mm": 0.0,
                 "split_aligned_coaxial": True, "solid_a": 1, "solid_b": 2}


def test_shaft_in_bore_is_close_faces_with_sag_measured():
    cls, rule, cause = classify_site(_site("self_overlap", [_fail("overlapping_facets")]), pairs=[SHAFT_IN_BORE],
                                     h_mm=2.0, hard=[], min_sicn={})
    assert cls == "close_faces", rule
    n, sag = facet_sag_mm(3.048, 2.0)
    assert cause["segments_at_h"] == n and cause["sag_at_h_mm"] == round(sag, 6)
    assert cause["gap_over_sag"] < 1.0           # the facets' sag swallows the gap: why they cross
    assert cause["segments_needed"] > n and cause["split_aligned_coaxial"] and not cause["same_body"]


def test_a_thin_wall_in_one_body_is_close_faces_and_a_contact_is_surface_crossing():
    wall = {"a": "F1", "b": "F2", "gap_mm": 0.05, "surface_a": "plane", "surface_b": "plane", "solid_a": 3,
            "solid_b": 3}
    assert classify_site(_site("intersection", [_fail("other", "gmsh_prod")]), pairs=[wall], h_mm=2.0, hard=[],
                         min_sicn={})[0] == "close_faces"
    contact = {"a": "F1", "b": "F2", "gap_mm": 0.0, "surface_a": "plane", "surface_b": "bsplinesurface"}
    cls, _rule, cause = classify_site(_site("intersection", [_fail("edge_through_face", "gmsh_prod")]),
                                      pairs=[contact], h_mm=2.0, hard=[], min_sicn={})
    assert cls == "surface_crossing" and cause["contacts"] == 1


def test_layer1_overlap_carries_volume_and_a_failed_boolean_is_the_checkers():
    hard = [{"code": "body_overlap", "entities": ["B1", "B2"], "detail": {"volume": 12.5, "thickness": 0.4}}]
    cls, _rule, cause = classify_site(_site("overlap", [], entities=("B1", "B2")), pairs=[], h_mm=2.0, hard=hard,
                                      min_sicn={})
    assert cls == "overlap_self_intersection" and cause["overlap_volume_mm3"] == 12.5
    failed = [{"code": "body_overlap", "entities": ["B1", "B2"], "detail": {"boolean_failed": True}}]
    assert classify_site(_site("overlap", [], entities=("B1", "B2")), pairs=[], h_mm=2.0, hard=failed,
                         min_sicn={})[0] == CHECKER_FAILED


@pytest.mark.parametrize("code", [*HARD_CODES, *SOFT_CODES])
def test_every_code_layer1_can_emit_has_a_class_or_is_the_checkers(code: str):
    """`verdict.valid.HARD_CODES` + `SOFT_CODES` are every code valid/valid_kernel emits; none may be `unknown`."""
    hard = [{"code": code, "entities": [], "detail": {}}]
    cls, rule, _cause = classify_site(_site("invalid", []), pairs=[], h_mm=1.0, hard=hard, min_sicn={})
    assert cls in (*PRIORITY, CHECKER_FAILED) and cls != "unknown", rule


def test_mesher_kinds_and_what_nobody_can_locate():
    def cls_of(kind: str, mesher: str = "gmsh_default") -> str:
        return classify_site(_site("proximity", [_fail(kind, mesher)]), pairs=[], h_mm=1.0, hard=[],
                             min_sicn={})[0]
    assert cls_of("periodic_surface") == "closed_periodic_face" and cls_of("no_volume") == "open_shell_volume"
    assert cls_of("inverted_elements") == "low_quality"
    assert cls_of("gmsh_crash") == CHECKER_FAILED                   # a crash with nothing located is not a defect
    assert cls_of("netgen_meshing_failed", "netgen_occ") == "unknown"
    assert cls_of("other", "gmsh_prod") == "unknown"                # HXT `other` with no measured close pair


def test_order_puts_validity_first_then_the_mesh_tier_cheapest_class_first():
    # CRS v5 r2: inside the mesh tier the cheap low_quality class goes before the slow close_faces one
    got = order([_defect("a", "unknown"), _defect("b", "low_quality"), _defect("c", "close_faces"),
                 _defect("d", "unknown", "validity")])
    assert [d.id for d in got] == ["d", "b", "c", "a"]
    # the validity tier keeps the class priority (overlap before the cheaper invalid_brep_tolerance)
    v = order([_defect("x", "invalid_brep_tolerance", "validity"), _defect("y", "overlap_self_intersection", "validity")])
    assert [d.id for d in v] == ["y", "x"]


def test_table_key_picks_the_code_row_inside_a_class_and_the_closed_shell_row():
    assert table_key(_defect("a", "open_shell_volume", "validity", "non_positive_volume")) == \
        "open_shell_volume:non_positive_volume"
    closed = Defect("a", "mesher", "no_volume", (), cls="open_shell_volume", cause={"free_loops": 0})
    assert table_key(closed) == "open_shell_volume:closed" and CLASS_REPAIRS[table_key(closed)][1] == ("make_volume",)
    assert table_key(_defect("a", "unknown", code="netgen_meshing_failed")) == "unknown:netgen_meshing_failed"


def test_ai_describes_unknowns_only_never_changes_a_class_and_never_raises():
    ds = [_defect("S@F1", "unknown", code="a"), _defect("S@F1", "unknown", code="b"), _defect("S@F2", "low_quality")]
    seen = {}

    async def fake(messages, tools, **kw):
        seen["ids"] = [d["id"] for d in json.loads(messages[1]["content"].split("\n", 1)[1])]
        call = SimpleNamespace(name="describe_defects", arguments=json.dumps({"defects": [
            {"id": "D1", "description": "a seam"}, {"id": "D2", "description": "a pole"}]}))
        return SimpleNamespace(reply=SimpleNamespace(failure_marker="", tool_calls=[call]))

    out = describe_unknown(ds, ledger=None, file_id="f", route="openai:test", call=fake)
    assert seen["ids"] == ["D1", "D2"]          # positional: the two share a site id
    assert [d.ai and d.ai.get("description") for d in out] == ["a seam", "a pole", None]
    assert [d.cls for d in out] == ["unknown", "unknown", "low_quality"]

    async def refused(*a, **k):
        raise RuntimeError("the ledger refused the v5_describe call")

    out = describe_unknown(ds[:1], ledger=None, file_id="f", route="r", call=refused)
    assert "refused" in out[0].ai["error"] and out[0].cls == "unknown"
