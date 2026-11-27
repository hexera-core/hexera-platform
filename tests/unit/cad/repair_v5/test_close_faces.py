"""v5 step 7, close_faces: the facet-crossing predictor's triangle test, the sag arithmetic, the whole-part scan's
sweep and time cap, and the diagnosis wiring (scan once per file, the predictor confirms, the fast local check).
No kernel and no gmsh: the kernel calls are faked."""

from __future__ import annotations

import math
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import sites as sites_mod
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v5 import close_faces as CF
from meshpipeline.cad.repair_v5 import diagnose as D

T = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))


def test_a_triangle_piercing_another_crosses_and_a_parallel_one_a_gap_away_does_not() -> None:
    assert CF.tri_tri_intersect(T, ((0.2, 0.2, -1.0), (0.2, 0.2, 1.0), (0.9, 0.9, 0.0)))
    assert not CF.tri_tri_intersect(T, ((0.0, 0.0, 0.1), (1.0, 0.0, 0.1), (0.0, 1.0, 0.1)))
    assert CF.tri_tri_intersect(T, ((0.0, 0.0, 0.1), (1.0, 0.0, 0.1), (0.0, 1.0, 0.1)), eps=0.2)


def test_coplanar_triangles_cross_only_when_they_overlap() -> None:
    assert CF.tri_tri_intersect(T, ((0.3, 0.3, 0.0), (2.0, 0.3, 0.0), (0.3, 2.0, 0.0)))
    assert not CF.tri_tri_intersect(T, ((0.8, 0.8, 0.0), (2.0, 0.8, 0.0), (0.8, 2.0, 0.0)))


def _polygon_band(r: float, n: int, turn: float, z: float = 1.0) -> list[CF.Tri]:
    """A cylinder of radius `r` cut in `n` facets (two triangles each), its first vertex at angle `turn`."""
    pts = [(r * math.cos(turn + 2 * math.pi * k / n), r * math.sin(turn + 2 * math.pi * k / n)) for k in range(n)]
    out = []
    for k in range(n):
        (x0, y0), (x1, y1) = pts[k], pts[(k + 1) % n]
        out += [((x0, y0, 0.0), (x1, y1, 0.0), (x1, y1, z)), ((x0, y0, 0.0), (x1, y1, z), (x0, y0, z))]
    return out


def test_shaft_in_bore_facets_cross_when_misaligned_and_nest_when_aligned_or_fine() -> None:
    bore, shaft = 1.0, 0.96          # 0.04 r apart: f9501251fb30's ratio
    assert CF.count_crossings(_polygon_band(shaft, 6, math.pi / 6), _polygon_band(bore, 6, 0.0)) > 0
    assert CF.count_crossings(_polygon_band(shaft, 6, 0.0), _polygon_band(bore, 6, 0.0)) == 0    # shared angles
    n = CF.segments_for_gap(bore, bore - shaft)
    assert CF.count_crossings(_polygon_band(shaft, n, math.pi / n), _polygon_band(bore, n, 0.0)) == 0
    assert CF.count_crossings(_polygon_band(shaft, 6, math.pi / 6), _polygon_band(bore, 6, 0.0), cap=3) == 3


def test_segments_for_gap_keeps_each_sag_under_half_the_gap() -> None:
    n = CF.segments_for_gap(3.175, 0.254)
    assert CF.sag_mm(3.175, n) < 0.127 <= CF.sag_mm(3.175, n - 1)
    assert CF.segments_for_gap(0.0, 1.0) is None and CF.segments_for_gap(1.0, 0.0) is None


def test_the_scan_sweeps_disjoint_neighbours_and_stops_at_its_time_cap() -> None:
    boxes = [(0, 0, 0, 1, 1, 1), (0.5, 0, 0, 2, 1, 1), (10, 10, 10, 11, 11, 11), (0.9, 0.9, 0.9, 1.5, 1.5, 1.5)]
    shared = [{1}, {2}, {3}, {1, -7}]                      # face 4 shares an edge with face 1
    got = [(a, b) for _s, a, b in CF.candidate_pairs(boxes, shared)]
    assert sorted(got) == [(1, 2), (2, 4)]
    ticks = iter(range(100))
    found, examined, truncated = CF.examine([(0.0, i, i + 1) for i in range(50)], lambda a, b: {"a": a},
                                            budget_s=5, clock=lambda: next(ticks))
    assert truncated and examined == 5 and len(found) == 5
    found, examined, truncated = CF.examine([(0.0, 1, 2)], lambda a, b: None, budget_s=5, clock=lambda: 0.0)
    assert (found, examined, truncated) == ([], 1, False)


# --- diagnosis: a failure naming no face gets the whole-part scan (once per file), the predictor confirms ---------


class _WS:
    brep_path, original, part_diag_mm = "/fake/w.brep", "/fake/o.brep", 100.0

    def __init__(self) -> None:
        self.indices = {"F1": 1, "F2": 2, "F3": 3, "B1": 1, "B2": 2}
        self.ids = dict.fromkeys(self.indices, "fp")


PAIR = {"a": 1, "b": 2, "solid_a": 1, "solid_b": 2, "gap_mm": 0.1, "surface_a": "cylinder",
        "surface_b": "cylinder", "radius_a_mm": 2.4, "radius_b_mm": 2.5, "axis_angle_deg": 0.0,
        "axis_offset_mm": 0.0, "split_aligned_coaxial": True}


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def call(pool: Any, fn: str, payload: dict) -> dict | None:
        calls.append((fn, payload))
        if fn == "v5_close_scan":
            return {"pairs": [PAIR], "truncated": False, "examined": 3}
        if fn == "v5_facet_cross":
            return {"pairs": [{"by_member": {"gmsh_default": 12, "gmsh_prod": 0}} for _ in payload["pairs"]]}
        return None
    monkeypatch.setattr(sites_mod, "_call", call)
    D._SCAN_CACHE.clear()
    D._PREDICT_CACHE.clear()
    return calls


def test_an_unlocated_netgen_failure_is_classed_from_one_whole_part_scan_per_file(kernel: list) -> None:
    from meshpipeline.cad.repair_v4.local_verify import Defect

    ws = _WS()
    pairs, info = D.whole_part_pairs(None, ws, h_mm=10.0, file_key="part.step")
    again, _ = D.whole_part_pairs(None, ws, h_mm=10.0, file_key="part.step")
    assert [fn for fn, _ in kernel] == ["v5_close_scan"] and pairs == again
    scan = kernel[0][1]
    assert scan["budget_s"] == D.SCAN_BUDGET_S and scan["max_pairs"] == D.SCAN_MAX_PAIRS
    assert scan["max_gap_mm"] == min(D.GAP_REACH_H * 10.0, D.SCAN_GAP_OF_DIAG * 100.0)
    assert (pairs[0]["a"], pairs[0]["b"], pairs[0]["body_a"]) == ("F1", "F2", "B1")
    got = D.predicted(None, ws, pairs, h_mm=10.0)
    assert got[0]["cross"] == {"gmsh_default": 12, "gmsh_prod": 0}
    site = Site("S@u", "unlocated", [], [], ["netgen_occ"], None,
                {"failures": [{"mesher": "netgen_occ", "error_kind": "netgen_meshing_failed"}]}, None)
    cls, rule, cause = D.classify_site(site, pairs=got, h_mm=10.0, hard=[], min_sicn={})
    assert cls == "close_faces" and "facets cross at gmsh_default" in rule
    assert cause["aim"] == ["F1", "F2"] and cause["face_pairs"] == [["F1", "F2"]] and cause["coaxial"]
    assert cause["aligned_faces"] == ["F1", "F2"] and cause["predicted_before"]["gmsh_default"] == 12
    d = Defect("S@u", "mesher", "netgen_meshing_failed", (), member="netgen_occ", cls=cls, cause=cause)
    from meshpipeline.cad.repair_v4 import seq
    from meshpipeline.cad.repair_v5 import loop
    assert loop.table_key(d) == "close_faces:coaxial"
    assert seq.params_for("split_aligned", d) == (["F1", "F2"], {"segments": 16, "scope": "targets"})
    assert seq.params_for("split_by_proximity", d)[1] == {"face_pairs": [["F1", "F2"]], "target_ratio": 0.25}


def test_a_close_pair_the_predictor_finds_no_crossing_on_is_not_close_faces() -> None:
    quiet = {**PAIR, "a": "F1", "b": "F2", "cross": {"gmsh_default": 0, "gmsh_prod": 0}}
    site = Site("s", "self_overlap", ["F1", "F2"], [], ["gmsh_default"], None,
                {"failures": [{"mesher": "gmsh_default", "error_kind": "overlapping_facets"}]}, None)
    assert D.classify_site(site, pairs=[quiet], h_mm=10.0, hard=[], min_sicn={})[0] != "close_faces"
    unmeasured = {k: v for k, v in quiet.items() if k != "cross"}     # no answer: the geometry decides
    assert D.classify_site(site, pairs=[unmeasured], h_mm=10.0, hard=[], min_sicn={})[0] == "close_faces"


def test_the_fast_local_check_reports_still_there_without_meshing_while_facets_cross(kernel: list) -> None:
    """The predictor on what the pair became (each face carried through the split's history to its pieces)."""
    from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult
    from meshpipeline.cad.repair_v5 import loop

    before, after = _WS(), _WS()
    before.history, after.history = [], [type("Diff", (), {"modified": {"F1": ["F3"]}, "generated": {},
                                                           "removed": []})()]
    after.indices = {"F2": 2, "F3": 3, "B1": 1, "B2": 2}
    d = Defect("S@x", "mesher", "overlapping_facets", ("F1",), member="gmsh_default", cls="close_faces",
               cause={"face_pairs": [["F1", "F2"]], "predicted_before": {"gmsh_default": 12}})
    meshed: list = []
    check = loop.predicting(lambda *a, **k: meshed.append(1) or LocalResult(True, [], {}, 0.0))
    got = check(None, before, after, d, None, timeout_s=1.0)
    assert not got.gone and not meshed and "facets still cross" in got.detail["fast_check"]
    assert kernel[-1][1]["pairs"] == [[[3], [2]]]                 # F1's piece F3 against F2
    assert got.detail["predicted_before"] == {"gmsh_default": 12} and got.detail["predicted_after"]["gmsh_default"] == 12


def test_triangles_sharing_a_mesh_node_touch_and_are_never_counted_as_crossing() -> None:
    a = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]
    b = [((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0))]          # meets `a` only at the shared corner
    assert CF.count_crossings(a, b) == 1
    assert CF.count_crossings(a, b, nodes_a=[(1, 2, 3)], nodes_b=[(1, 4, 5)]) == 0
