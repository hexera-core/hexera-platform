# Responsibility: Verify failure-site detection and the proposer's observation, hermetically: how
#                 located failures cluster into sites, which warnings attach, what the model is told.
# Boundaries: hermetic - a scripted pool stands in for the kernel workers (boxes are a table);
#             MemberResult and Located are the real panel classes, the Verdict the real one.
#             The geometry itself is proved in tests/native/test_native_repair_v4_sites.py.
from __future__ import annotations

import json
from typing import Any

from meshpipeline.cad.repair_v4.sites import OBSERVATION_MAX_BYTES, find_sites, observation
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.verdict.assemble import Verdict
from meshpipeline.cad.repair_v4.verdict.panel import Located, MemberResult
from meshpipeline.cad.repair_v4.verdict.valid import Finding, ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Workspace

H = 1.0


def _real_verdict(valid: ValidReport, panel: list[MemberResult], faithful: Any = None) -> Verdict:
    """The real Verdict; the production class has no defaults on purpose (a defaulted `clean` would let
    forgetting it read as unclean), so the test says them."""
    return Verdict(valid, panel, faithful, False, "cheap")


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=0.0)


class ScriptedPool:
    """Boxes by stable-id; face i is the unit cube at x = 10 * i unless overridden."""

    def __init__(self, ws: Workspace, boxes: dict[str, list[float]], *, locate=None, fail=(),
                 prox=(), distance=None, loops=()) -> None:
        self.ws, self.boxes, self.locate, self.fail = ws, boxes, locate or {}, set(fail)
        self.loops = list(loops)  # [(kernel edge indices, span mm)] the free loops `sites_free_loops` reports
        self.prox, self.distance = list(prox), distance or (lambda a, b: 0.4)
        self.calls: list[str] = []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append(fn)
        if fn in self.fail:
            return CallResult(False, None, "RuntimeError: scripted", "error", 0.0)
        if fn == "sites_geometry":
            geometry = {}
            for entity in payload["entities"]:
                box = self.boxes[entity["id"]]
                geometry[entity["id"]] = {
                    "box": box,
                    "centroid": [(box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2],
                }
            return _ok({"geometry": geometry, "diagonal": 100.0})
        if fn == "sites_locate_points":
            found = [self.locate.get(tuple(p), []) for p in payload["points"]]
            return _ok({"faces": found, "dists": [[0.25 * (k + 1) for k in range(len(f))] for f in found]})
        if fn == "sites_proximity":
            return _ok({"pairs": [{"a": a, "b": b, "dist": d, "point_a": [1.0, 2.0, 3.0],
                                   "point_b": [1.0, 2.5, 3.0]} for a, b, d in self.prox]})
        if fn == "sites_distance":
            return _ok({"distances": [
                {"dist": self.distance(p["a"]["id"], p["b"]["id"]), "adjacent": False,
                 "point_a": [0, 0, 0], "point_b": [0, 0, 0.4]}
                for p in payload["pairs"]]})
        if fn == "sites_graph":
            return _ok({"graphs": {
                s["id"]: {"nodes": [{"id": f"{e['type'][0].upper()}{e['index']}", "surface": "plane",
                                     "area": 1.0, "min_width": 1.0, "angular_span_deg": None, "hop": 0}
                                    for e in s["seeds"]], "edges": [], "truncated": False}
                for s in payload["sites"]}})
        if fn == "sites_free_loops":
            return _ok({"loops": [{"edges": list(e), "span": span} for e, span in self.loops], "open_wires": 0,
                        "diagonal": 100.0})
        raise AssertionError(f"unscripted kernel call {fn}")


def _ws(n_faces: int = 12, *, shift: int = 0) -> Workspace:
    """Stable id F<i> sits at kernel index i + shift."""
    ids = {f"F{i}": f"fp{i}" for i in range(1, n_faces + 1)}
    ids["B1"] = "fpB"
    ids.update({f"E{i}": f"fpE{i}" for i in range(1, 9)})
    indices = {f"F{i}": i + shift for i in range(1, n_faces + 1)}
    indices.update({f"E{i}": i for i in range(1, 9)})
    indices["B1"] = 1
    return Workspace(brep_path="x.brep", ids=ids, labels={"F1": {"name": "bracket top"}}, history=[],
                     original="x.brep", indices=indices)


def _boxes(ws: Workspace) -> dict[str, list[float]]:
    out = {}
    for sid in ws.ids:
        i = int(sid[1:])
        out[sid] = [10.0 * i, 0.0, 0.0, 10.0 * i + 1.0, 1.0, 1.0]
    return out


def _valid(*, hard=(), soft=()) -> ValidReport:
    return ValidReport(not hard, list(hard), list(soft), "mm", 1, True)


def _verdict(*members: MemberResult, hard=(), soft=()) -> Verdict:
    return _real_verdict(_valid(hard=hard, soft=soft), list(members))


def _fail(name: str, kind: str, *located: Located) -> MemberResult:
    return MemberResult(name, "fail", kind, "mesher said no", list(located))


def _at(ws: Workspace, entity: str) -> tuple[float, float, float]:
    i = int(entity[1:])
    return (10.0 * i + 0.5, 0.5, 0.5)


def test_soft_findings_outside_sites_ignored() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    far = Finding("small_face", ["F9"], {"threshold": 1e-6})
    nowhere = Finding("large_tolerance", [], {"max_tolerance": 0.5})
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "s1")), soft=[far, nowhere])
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert len(sites) == 1
    text = json.dumps(observation(ws, verdict, sites, {"usd": 1.0}))
    assert "F9" not in text and "large_tolerance" not in text
    assert "F9" not in sites[0].entities
    assert sites[0].graph.get("soft", []) == []


def test_soft_finding_inside_a_site_box_attaches() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F5"] = [10.0, 0.0, 0.0, 10.5, 0.5, 0.5]  # sits on F1's box
    pool = ScriptedPool(ws, boxes)
    near = Finding("small_face", ["F5"], {"threshold": 1e-6})
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "s1")), soft=[near])
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert [item["entity"] for item in site.graph["soft"]] == ["F5"]
    assert "F5" not in site.entities  # a warning is context, not a failing entity


def test_site_merges_same_failure_across_meshers() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F2"] = [10.0, 1.5, 0.0, 11.0, 2.5, 1.0]  # 0.5 from F1, within 2h
    boxes["F3"] = [10.0, 3.0, 0.0, 11.0, 4.0, 1.0]  # 0.5 from F2
    pool = ScriptedPool(ws, boxes)
    verdict = _verdict(
        _fail("gmsh", "overlapping_facets", Located("F1", None, "surf 1"), Located("F2", None, "surf 2")),
        _fail("netgen", "overlapping_facets", Located("F2", None, "face 7"), Located("F3", None, "face 8")),
    )
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert len(sites) == 1
    assert sorted(sites[0].entities) == ["F1", "F2", "F3"]
    assert sites[0].meshers_failing == ["gmsh", "netgen"]
    assert sites[0].kind == "self_overlap"


def test_distant_failures_are_separate_sites() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "edge_through_face", Located("F1", None, "a"), Located("F8", None, "b")))
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert sorted(tuple(s.entities) for s in sites) == [("F1",), ("F8",)]
    assert {s.kind for s in sites} == {"intersection"}


def test_failure_without_location_is_one_coarse_whole_body_site_not_dropped() -> None:
    """Lane C5: an `unlocated` site gave the search nothing to aim at (8 of 13 conclusive smoke failures). A failure
    with no place is now a COARSE site on the whole body, with the reason, so the part-wide sweeps can run."""
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "other"), MemberResult("netgen", "crashed", "crash", "boom"))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "invalid" and site.entities == ["B1"] and site.meshers_failing == ["gmsh", "netgen"]
    assert site.graph["coarse"]["members"] == ["gmsh", "netgen"] and "without saying where" in site.graph["coarse"]["reason"]


def test_an_unlocated_site_is_observable_by_the_proposer() -> None:
    """`unmeasured_pairs` is a count; a site that was never clustered used to carry `[]` there and `observation`
    crashed in `int()`, so the AI proposer never put a request on the wire for a part whose only site was this one."""
    ws = _ws()
    verdict = _verdict(_fail("gmsh", "other"))
    sites = find_sites(ScriptedPool(ws, _boxes(ws)), ws, verdict, h_mm=H)
    assert sites[0].graph["unmeasured_pairs"] == 0
    assert observation(ws, verdict, sites, {})["unmeasured_pairs"] == 0


def test_timeout_without_location_is_a_timeout_site() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(MemberResult("gmsh", "timeout", "timeout", "budget"))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "timeout"


def test_a_point_without_an_entity_is_resolved_to_faces_through_the_kernel() -> None:
    ws = _ws()
    point = (20.5, 0.5, 0.5)
    pool = ScriptedPool(ws, _boxes(ws), locate={point: [2]})  # kernel index 2 is F2
    verdict = _verdict(_fail("gmsh", "edge_through_face", Located(None, point, "plc")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.entities == ["F2"]
    assert site.xyz == [point]


def test_unresolvable_point_still_makes_a_point_site() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "edge_through_face", Located(None, (500.0, 0.0, 0.0), "plc")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.entities == [] and site.xyz == [(500.0, 0.0, 0.0)] and site.kind == "intersection"


def test_hard_findings_are_resolved_through_workspace_indices() -> None:
    ws = _ws(shift=100)  # kernel index 105 is stable F5: a bare "F5" would name the wrong face
    pool = ScriptedPool(ws, _boxes(ws))
    finding = Finding("self_intersection", ["F105"], {})
    verdict = _verdict(MemberResult("gmsh", "pass"), hard=[finding])
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.entities == ["F5"] and site.kind == "self_overlap" and site.meshers_failing == ["valid"]


def test_clean_verdict_has_no_sites() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    assert find_sites(pool, ws, _verdict(MemberResult("gmsh", "pass")), h_mm=H) == []
    assert pool.calls == []  # nothing to locate, nothing asked of the kernel


def test_dead_entity_is_not_reported() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "other", Located("F99", None, "ghost")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert "F99" not in site.entities and site.graph.get("coarse")


def test_kernel_failure_degrades_to_sites_without_geometry() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws), fail=("sites_geometry", "sites_graph", "sites_distance"))
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", (10.5, 0.5, 0.5), "a")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.entities == ["F1"] and site.dist_over_h is None and site.graph.get("error")


def test_dist_over_h_comes_from_the_kernel_distance() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F2"] = [10.0, 1.5, 0.0, 11.0, 2.5, 1.0]
    pool = ScriptedPool(ws, boxes)
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "a"), Located("F2", None, "b")))
    (site,) = find_sites(pool, ws, verdict, h_mm=2.0)
    assert site.dist_over_h == 0.2  # 0.4 mm over h = 2


def test_observation_carries_what_the_proposer_needs() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "s1")))
    sites = find_sites(pool, ws, verdict, h_mm=H)
    sites[0].view_png = b"\x89PNG-not-json"
    obs = observation(ws, verdict, sites, {"usd_left": 0.8})
    assert obs["budget"] == {"usd_left": 0.8}
    (entry,) = obs["sites"]
    assert entry["entities"] == ["F1"] and entry["kind"] == "self_overlap"
    assert entry["labels"] == {"F1": "bracket top"}
    assert entry["meshers_failing"] == ["gmsh"] and "graph" in entry
    assert obs["panel"][0]["name"] == "gmsh" and obs["panel"][0]["status"] == "fail"
    assert "PNG" not in json.dumps(obs)  # pixels are not sent as JSON


def test_observation_drops_a_site_whose_entities_are_no_longer_live() -> None:
    ws = _ws()
    stale = Site("S9", "invalid", ["F404"], [], ["gmsh"], None, {}, None)
    verdict = _verdict(MemberResult("gmsh", "pass"))
    obs = observation(ws, verdict, [stale], {})
    assert obs["sites"] == [] and obs["stale_sites"] == ["S9"]


def test_observation_is_json_serialisable_and_under_60kb() -> None:
    ws = _ws()
    # far past a realistic part: 150 sites, each with 80 entities, a 60-node graph and 30 warnings.
    sites = []
    for n in range(150):
        graph = {
            "k": 2,
            "nodes": [{"id": f"F{j}", "surface": "bspline", "area": 123.456789, "min_width": 0.123456,
                       "angular_span_deg": 270.123456, "hop": j % 3} for j in range(60)],
            "edges": [{"faces": [f"F{j}", f"F{j + 1}"], "edge": f"E{j}", "continuity": "C0",
                       "dihedral_deg": 91.123456, "tolerance": 0.001, "gap": 0.0002} for j in range(90)],
            "soft": [{"code": "small_edge", "entity": f"E{j}", "detail": {"threshold": 0.001}}
                     for j in range(30)],
        }
        sites.append(Site(f"S{n}", "proximity", [f"F{1 + j % 12}" for j in range(80)],
                          [(float(j), 0.5, 0.25) for j in range(20)], ["gmsh", "netgen"], 0.3, graph, None))
    verdict = _verdict(*[_fail(f"m{i}", "other") for i in range(6)])
    obs = observation(ws, verdict, sites, {"usd_left": 0.5})
    blob = json.dumps(obs)
    assert len(blob.encode()) < OBSERVATION_MAX_BYTES <= 60_000
    assert obs["sites"], "trimming must keep the best sites, not empty the list"
    assert obs["sites_omitted"] > 0


def test_a_realistic_observation_is_far_under_the_cap() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "edge_through_face", *[Located(f"F{i}", None, "t") for i in range(1, 11)]))
    obs = observation(ws, verdict, find_sites(pool, ws, verdict, h_mm=H), {})
    assert len(json.dumps(obs)) < 15_000


def test_observation_states_the_element_size_at_top_level() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "s1")))
    sites = find_sites(pool, ws, verdict, h_mm=2.5)
    assert observation(ws, verdict, sites, {})["h_mm"] == 2.5
    assert observation(ws, verdict, [], {}, h_mm=4.0)["h_mm"] == 4.0


def test_boxes_within_2h_but_exactly_far_apart_are_not_one_site() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F2"] = [10.0, 1.5, 0.0, 11.0, 2.5, 1.0]  # boxes 0.5 apart ...
    pool = ScriptedPool(ws, boxes, distance=lambda a, b: 40.0)  # ... but the faces are 40 mm apart
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "a"), Located("F2", None, "b")))
    assert sorted(tuple(s.entities) for s in find_sites(pool, ws, verdict, h_mm=H)) == [("F1",), ("F2",)]


def test_proximity_pairs_cannot_chain_two_failures_into_one_site() -> None:
    ws = _ws()
    # F1 and F8 fail, 70 mm apart; a close pair (F4, F5) lies between them. The pair is context, not a node.
    pool = ScriptedPool(ws, _boxes(ws), prox=[(4, 5, 0.3)])
    verdict = _verdict(_fail("gmsh", "other", Located("F1", None, "a"), Located("F8", None, "b")))
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert sorted(tuple(s.entities) for s in sites) == [("F1",), ("F8",)]


def test_proximity_pair_touching_a_failure_attaches_with_its_clash_points() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F2"] = [10.0, 1.5, 0.0, 11.0, 2.5, 1.0]
    pool = ScriptedPool(ws, boxes, prox=[(1, 2, 0.3)])
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "surf 1")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.entities == ["F1", "F2"] and site.dist_over_h == 0.3
    (clash,) = site.graph["clashes"]
    assert {clash["a"], clash["b"]} == {"F1", "F2"} and clash["point_a"] == [1.0, 2.0, 3.0]
    obs = observation(ws, verdict, [site], {})
    assert obs["sites"][0]["clashes"][0]["point_b"] == [1.0, 2.5, 3.0]


def test_site_ids_do_not_change_when_other_sites_appear_or_vanish() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    one = find_sites(pool, ws, _verdict(_fail("gmsh", "other", Located("F5", None, "a"))), h_mm=H)
    more = find_sites(pool, ws, _verdict(
        _fail("gmsh", "other", Located("F5", None, "a"), Located("F2", None, "b")),
        _fail("netgen", "edge_through_face", Located("F9", None, "c"))), h_mm=H)
    ids = {tuple(s.entities): s.id for s in more}
    assert one[0].id == ids[("F5",)] == "S@F5"
    assert ids[("F9",)] == "S@F9" and ids[("F2",)] == "S@F2"


def test_a_stale_entity_with_a_point_falls_back_to_the_point() -> None:
    ws = _ws()
    point = (20.5, 0.5, 0.5)
    pool = ScriptedPool(ws, _boxes(ws), locate={point: [2]})
    verdict = _verdict(_fail("gmsh", "edge_through_face", Located("F404", point, "plc")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.entities == ["F2"]


def test_each_site_carries_its_own_message_tag_and_point() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    first = MemberResult("gmsh", "fail", "edge_through_face", "PLC: segment and a facet intersect",
                         [Located("F1", (10.5, 0.5, 0.5), "surface 12")])
    second = MemberResult("gmsh", "fail", "overlapping_facets", "overlapping facets 80 81",
                          [Located("F8", (80.5, 0.5, 0.5), "surface 80")])
    verdict = _verdict(first, second)
    by_entity = {tuple(s.entities): s for s in find_sites(pool, ws, verdict, h_mm=H)}
    obs = {e["id"]: e for e in observation(ws, verdict, list(by_entity.values()), {})["sites"]}
    one, eight = obs["S@F1"], obs["S@F8"]
    assert one["failures"][0]["message"].startswith("PLC") and one["failures"][0]["tag"] == "surface 12"
    assert eight["failures"][0]["message"].startswith("overlapping")
    assert one["points"][0] == {"xyz": [10.5, 0.5, 0.5], "entity": "F1", "mesher": "gmsh", "tag": "surface 12"}


def test_oversize_base_is_flagged_and_caller_fields_are_bounded() -> None:
    ws = _ws()
    huge = {"blob": "x" * 500_000}
    members = [MemberResult(f"m{i}", "fail", "other", "m" * 400, []) for i in range(900)]
    verdict = _real_verdict(_valid(), members, huge)
    obs = observation(ws, verdict, [], {"note": "y" * 500_000})
    assert obs["oversize"] is True and obs["trimmed"] is True
    assert len(json.dumps(obs).encode()) < 60_000
    small = observation(ws, _verdict(MemberResult("gmsh", "pass")), [], {"usd": 1})
    assert "oversize" not in small and small["trimmed"] is False
    assert "truncated" in observation(ws, _real_verdict(_valid(), [], huge), [], {})["faithful"]


def test_unknown_error_kind_is_pinned_to_invalid() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("netgen", "some_new_kind", Located("F1", None, "a")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "invalid"


def _all_overlapping(ws: Workspace) -> dict[str, list[float]]:
    return {sid: [0.0, 0.0, 0.0, 100.0, 100.0, 100.0] for sid in ws.ids}


def test_dense_region_does_not_form_a_mega_site() -> None:
    """300 failing faces whose boxes all overlap (big planes) but whose faces are 40 mm apart: the
    box gap alone would chain them into one 300-entity site, the v3 314-fingerprint region."""
    from meshpipeline.cad.repair_v4 import sites as sites_module

    ws = _ws(300)
    pool = ScriptedPool(ws, _all_overlapping(ws), distance=lambda a, b: 40.0)
    verdict = _verdict(_fail("gmsh", "overlapping_facets",
                             *[Located(f"F{i}", None, f"s{i}") for i in range(1, 301)]))
    found = find_sites(pool, ws, verdict, h_mm=H)
    assert len(found) == 300 and max(len(s.entities) for s in found) == 1
    # 44,850 candidate pairs, far past the cap: the rest were left UNmerged, and the sites say so
    assert sum(1 for s in found if s.graph["unmeasured_pairs"] > 0) > 250
    measured = pool.calls.count("sites_distance")
    assert 1 < measured <= sites_module.MAX_CLUSTER_PAIRS // sites_module.CLUSTER_CHUNK + 1


def test_unmeasured_pairs_are_not_merged_when_the_kernel_does_not_answer() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F2"] = [10.0, 1.5, 0.0, 11.0, 2.5, 1.0]  # within 2h by box
    verdict = _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "a"), Located("F2", None, "b")))
    down = ScriptedPool(ws, boxes, fail=("sites_distance",))
    sites = find_sites(down, ws, verdict, h_mm=H)
    assert sorted(tuple(s.entities) for s in sites) == [("F1",), ("F2",)]
    assert [s.graph["unmeasured_pairs"] for s in sites] == [1, 1]
    # a distance the kernel could not compute (None) is also not a licence to merge
    blank = ScriptedPool(ws, boxes, distance=lambda a, b: None)
    assert len(find_sites(blank, ws, verdict, h_mm=H)) == 2
    obs = observation(ws, verdict, sites, {})
    assert obs["sites"][0]["graph"]["unmeasured_pairs"] == 1


def test_nearest_pairs_are_measured_first_when_the_cap_bites(monkeypatch) -> None:
    from meshpipeline.cad.repair_v4 import sites as sites_module

    monkeypatch.setattr(sites_module, "MAX_CLUSTER_PAIRS", 5)
    monkeypatch.setattr(sites_module, "CLUSTER_CHUNK", 5)
    ws = _ws()
    boxes = {f"F{i}": [0.0, 0.0, 0.0, 100.0 + 10 * i, 100.0, 100.0] for i in range(1, 7)}
    boxes["F7"] = [48.0, 48.0, 48.0, 49.0, 49.0, 49.0]
    boxes["F8"] = [49.5, 48.0, 48.0, 50.0, 49.0, 49.0]
    boxes.update({k: [500.0, 0, 0, 501.0, 1, 1] for k in ("B1", "F9", "F10", "F11", "F12")})
    pool = ScriptedPool(ws, boxes, distance=lambda a, b: 0.5 if {a, b} == {"F7", "F8"} else 40.0)
    verdict = _verdict(_fail("gmsh", "other", *[Located(f"F{i}", None, "t") for i in range(1, 9)]))
    merged = [tuple(s.entities) for s in find_sites(pool, ws, verdict, h_mm=H) if len(s.entities) > 1]
    assert merged == [("F7", "F8")]  # the true neighbours were among the 5 measured of 28 candidates


def test_site_id_ignores_proximity_context_faces() -> None:
    ws = _ws()
    boxes = _boxes(ws)
    pool = ScriptedPool(ws, boxes, prox=[(2, 5, 0.3)])  # F2 is lower than the failing F5
    verdict = _verdict(_fail("gmsh", "other", Located("F5", None, "a")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert "F2" in site.entities and site.id == "S@F5"


def test_registry_carries_an_id_across_an_op_that_replaces_its_anchor() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry
    from meshpipeline.cad.repair_v4.workspace import Diff

    registry = SiteRegistry()
    ws1 = _ws()
    near = _boxes(ws1)
    near["F6"] = [50.0, 1.5, 0.0, 51.0, 2.5, 1.0]  # 0.5 from F5: one site
    pool = ScriptedPool(ws1, near)
    first = find_sites(pool, ws1, _verdict(_fail("gmsh", "other", Located("F5", None, "a"),
                                                 Located("F6", None, "b"))), h_mm=H, registry=registry)
    (site1,) = first
    assert site1.id == "S@F5" and not site1.graph.get("id_carried")

    # an op replaced F5 by F20 (progress); the failure is still there, now on F20 and F6
    ws2 = _ws(24)
    ws2.history = [Diff("split", {}, {"F5": ["F20"]}, {}, [], [], [], 0.0)]
    boxes2 = _boxes(ws2)
    boxes2["F20"] = near["F5"]
    boxes2["F6"] = near["F6"]
    pool2 = ScriptedPool(ws2, boxes2)
    second = find_sites(pool2, ws2, _verdict(_fail("gmsh", "other", Located("F20", None, "a"),
                                                   Located("F6", None, "b"))), h_mm=H, registry=registry)
    (site2,) = second
    assert site2.id == "S@F5" and site2.graph["id_carried"] is True  # a fresh anchor would be S@F6
    assert observation(ws2, _verdict(), second, {})["sites"][0]["id_carried"] is True
    # without the registry the same round is renamed: that is the hole it closes
    (plain,) = find_sites(pool2, ws2, _verdict(_fail("gmsh", "other", Located("F20", None, "a"),
                                                     Located("F6", None, "b"))), h_mm=H)
    assert plain.id == "S@F6"


def _round(registry, ws, failing: dict[str, list[str]], boxes=None):
    """One search round: `failing` maps a site anchor to the entities failing together (near each other)."""
    boxes = dict(boxes or _boxes(ws))
    located = []
    for n, entities in enumerate(failing.values()):
        for k, e in enumerate(entities):
            boxes[e] = [10.0 * (n + 1) * 7, 2.0 * k, 0.0, 10.0 * (n + 1) * 7 + 1, 2.0 * k + 1, 1.0]
            located.append(Located(e, None, "t"))
    pool = ScriptedPool(ws, boxes)
    return {tuple(s.entities): s for s in find_sites(
        pool, ws, _verdict(_fail("gmsh", "other", *located)), h_mm=H, registry=registry)}


def test_registry_merge_keeps_one_id_and_reports_the_other() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry

    registry, ws = SiteRegistry(), _ws(30)
    first = _round(registry, ws, {"x": ["F9", "F10"], "y": ["F20", "F21"]})
    assert {s.id for s in first.values()} == {"S@F9", "S@F20"}
    # an op's effect: the two failures are now one site (all four faces within 2h of each other)
    boxes = _boxes(ws)
    for k, e in enumerate(["F9", "F10", "F20", "F21"]):
        boxes[e] = [0.0, 1.5 * k, 0.0, 1.0, 1.5 * k + 1.0, 1.0]
    pool = ScriptedPool(ws, boxes)
    located = [Located(e, None, "t") for e in ("F9", "F10", "F20", "F21")]
    (merged,) = find_sites(pool, ws, _verdict(_fail("gmsh", "other", *located)), h_mm=H, registry=registry)
    assert merged.id == "S@F9"  # tie broken by natural id order, never by string order (S@F10 < S@F9)
    assert merged.graph["was"] == ["S@F20"]  # the other predecessor is named, not dropped
    assert observation(ws, _verdict(), [merged], {})["sites"][0]["was"] == ["S@F20"]


def test_registry_split_gives_the_splinter_a_fresh_id_that_names_its_origin() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry

    registry, ws = SiteRegistry(), _ws(30)
    boxes = _boxes(ws)
    for k, e in enumerate(["F4", "F5", "F6", "F7"]):
        boxes[e] = [0.0, 1.5 * k, 0.0, 1.0, 1.5 * k + 1.0, 1.0]
    located = [Located(e, None, "t") for e in ("F4", "F5", "F6", "F7")]
    pool = ScriptedPool(ws, boxes)
    (whole,) = find_sites(pool, ws, _verdict(_fail("gmsh", "other", *located)), h_mm=H, registry=registry)
    assert whole.id == "S@F4"
    after = _round(registry, ws, {"a": ["F4", "F5", "F6"], "b": ["F7"]})
    big, splinter = after[("F4", "F5", "F6")], after[("F7",)]
    assert big.id == "S@F4" and "was" not in big.graph
    assert splinter.id == "S@F7" and splinter.graph["was"] == ["S@F4"]  # visible, not silent


def test_registry_partial_overlap_on_a_shared_big_face_is_a_new_site() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry

    registry, ws = SiteRegistry(), _ws(30)
    boxes = _boxes(ws)
    for k, e in enumerate(["F3", "F11"]):
        boxes[e] = [0.0, 1.5 * k, 0.0, 1.0, 1.5 * k + 1.0, 1.0]
    pool = ScriptedPool(ws, boxes)
    (old,) = find_sites(pool, ws, _verdict(_fail("gmsh", "other", Located("F3", None, "a"),
                                                 Located("F11", None, "a"))), h_mm=H, registry=registry)
    assert old.id == "S@F3"
    # a DIFFERENT defect now touches the same big face F3 (one shared entity of four): not the old site
    boxes2 = _boxes(ws)
    for k, e in enumerate(["F3", "F14", "F15", "F16"]):
        boxes2[e] = [0.0, 1.5 * k, 0.0, 1.0, 1.5 * k + 1.0, 1.0]
    pool2 = ScriptedPool(ws, boxes2)
    located = [Located(e, None, "b") for e in ("F3", "F14", "F15", "F16")]
    (new,) = find_sites(pool2, ws, _verdict(_fail("gmsh", "other", *located)), h_mm=H, registry=registry)
    assert new.graph.get("was") == ["S@F3"] and not new.graph.get("id_carried")
    assert new.id == "S@F3-2"  # the old id is NOT handed to a new defect: it is retired
    assert registry.retired == ["S@F3"]


def test_registry_refuses_a_diverging_or_rolled_back_history() -> None:
    import pytest

    from meshpipeline.cad.repair_v4.sites import LineageMismatch, SiteRegistry
    from meshpipeline.cad.repair_v4.workspace import Diff

    registry, ws = SiteRegistry(), _ws()
    _round(registry, ws, {"x": ["F5"]})
    followed = _ws()
    followed.history = [Diff("split", {}, {"F1": ["F2"]}, {}, [], [], [], 0.0)]
    _round(registry, followed, {"x": ["F5"]})  # a continuation of the empty history: fine
    branch = _ws()
    branch.history = [Diff("sew", {}, {}, {}, [], [], [], 0.0)]  # a different branch, same start
    with pytest.raises(LineageMismatch, match="history"):
        _round(registry, branch, {"x": ["F5"]})
    with pytest.raises(LineageMismatch, match="history"):
        _round(registry, _ws(), {"x": ["F5"]})  # shorter than what was seen: a rollback


def test_registry_refuses_a_workspace_with_another_original() -> None:
    import pytest

    from meshpipeline.cad.repair_v4.sites import LineageMismatch, SiteRegistry

    registry = SiteRegistry()
    _round(registry, _ws(), {"x": ["F5"]})  # empty history on both sides: only `original` differs
    alien = _ws()
    alien.original = "elsewhere.brep"
    with pytest.raises(LineageMismatch, match="elsewhere.brep"):
        _round(registry, alien, {"x": ["F5"]})


def test_entityless_sites_keep_their_ids_across_three_rounds() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry

    registry, ws = SiteRegistry(), _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "edge_through_face", Located(None, (500.0, 0.0, 0.0), "plc")),
                       MemberResult("netgen", "fail", "other", "no luck", []))
    rounds = [find_sites(pool, ws, verdict, h_mm=H, registry=registry) for _ in range(3)]
    for found in rounds:
        assert sorted(s.id for s in found) == sorted(s.id for s in rounds[0])
        assert all("was" not in s.graph and not s.graph.get("id_carried") for s in found)
    ids = [s.id for s in rounds[0]]
    assert any(i.startswith("S@p") for i in ids) and "S@coarse-invalid" in ids
    assert not any(i.endswith(("-2", "-3")) for r in rounds for i in (s.id for s in r))


def test_split_tie_between_equal_halves_is_deterministic() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry

    outcomes = set()
    for _ in range(4):
        registry, ws = SiteRegistry(), _ws(30)
        boxes = _boxes(ws)
        for k, e in enumerate(["F4", "F5", "F6", "F7"]):
            boxes[e] = [0.0, 1.5 * k, 0.0, 1.0, 1.5 * k + 1.0, 1.0]
        located = [Located(e, None, "t") for e in ("F4", "F5", "F6", "F7")]
        find_sites(ScriptedPool(ws, boxes), ws, _verdict(_fail("gmsh", "other", *located)), h_mm=H,
                   registry=registry)
        after = _round(registry, ws, {"a": ["F4", "F5"], "b": ["F6", "F7"]})
        outcomes.add(tuple(sorted((s.id, tuple(s.entities), tuple(s.graph.get("was", [])))
                                  for s in after.values())))
    assert len(outcomes) == 1
    (only,) = outcomes
    assert ("S@F4", ("F4", "F5"), ()) in only  # the half holding the anchor keeps the id


def test_a_point_resolved_to_faces_is_measured_like_any_face() -> None:
    """`sites_locate_points` adds the faces under a point as ordinary entity nodes, so their merging
    is decided by exact distance: a point can never chain faces through a big box."""
    ws = _ws()
    point = (20.5, 0.5, 0.5)
    pool = ScriptedPool(ws, _all_overlapping(ws), locate={point: [2]}, distance=lambda a, b: 40.0)
    verdict = _verdict(_fail("gmsh", "other", Located("F1", None, "a"), Located(None, point, "plc")))
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert sorted(tuple(s.entities) for s in sites) == [("F1",), ("F2",)]
    assert pool.calls.count("sites_distance") >= 1


def test_a_located_point_carries_its_distance_to_the_nearest_face_in_the_site_row() -> None:
    """Lane D28: the site's `points` row says how far the mesher's point lies from the nearest face, and which faces
    it lies on; a point no face is within h of says None (it is its own site)."""
    ws = _ws()
    on, off = (20.5, 0.5, 0.5), (500.0, 500.0, 500.0)
    pool = ScriptedPool(ws, _all_overlapping(ws), locate={on: [2, 3]}, distance=lambda a, b: 40.0)
    verdict = _verdict(_fail("gmsh", "other", Located(None, on, "plc"), Located(None, off, "far")))
    rows = [p for s in find_sites(pool, ws, verdict, h_mm=H) for p in s.graph["points"]]
    by = {tuple(r["xyz"]): r for r in rows}
    assert by[on]["nearest_face_mm"] == 0.25 and by[on]["faces_at"] == ["F2", "F3"]
    assert by[off]["nearest_face_mm"] is None and by[off]["faces_at"] == []


def test_a_point_never_chains_entities_through_a_large_faces_box() -> None:
    ws = _ws()
    boxes = _all_overlapping(ws)  # every face box is huge and contains the point
    pool = ScriptedPool(ws, boxes, distance=lambda a, b: 40.0)
    point = (50.0, 50.0, 50.0)
    verdict = _verdict(_fail("gmsh", "other", Located("F1", None, "a"), Located("F2", None, "b"),
                             Located(None, point, "plc")))
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert sorted(len(s.entities) for s in sites) == [0, 1, 1]  # the point is its own site
    assert all(s.graph["unmeasured_pairs"] > 0 for s in sites)
    assert observation(ws, verdict, sites, {})["unmeasured_pairs"] > 0


def test_two_mesher_points_close_together_are_one_site() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "other", Located(None, (500.0, 0.0, 0.0), "a"),
                             Located(None, (501.0, 0.0, 0.0), "b")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert len(site.xyz) == 2


def test_registry_does_not_hand_a_carried_id_to_an_unrelated_new_site() -> None:
    from meshpipeline.cad.repair_v4.sites import SiteRegistry

    registry = SiteRegistry()
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    find_sites(pool, ws, _verdict(_fail("gmsh", "other", Located("F5", None, "a"))), h_mm=H, registry=registry)
    again = find_sites(pool, ws, _verdict(_fail("gmsh", "other", Located("F5", None, "a"),
                                                Located("F9", None, "b"))), h_mm=H, registry=registry)
    assert sorted(s.id for s in again) == ["S@F5", "S@F9"]


def test_hard_finding_that_cannot_be_placed_keeps_its_kind() -> None:
    """An open shell is reported against the shell `H1`, which has no stable id. It is still an OPEN site
    (whole part, no entities), not an `unlocated` one: the det sweep proposes nothing for `unlocated`."""
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(MemberResult("gmsh", "pass"), hard=[Finding("open_shell", ["H1"], {"free_edges": 4})])
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "open" and site.entities == [] and site.meshers_failing == ["valid"]
    assert site.id == "S@open"


def test_an_open_site_carries_the_free_loops_as_stable_edge_ids_and_keeps_its_identity() -> None:
    """An open finding names a shell, so the site has no edges for `fill_loop` to take. The kernel's free loops are put on
    the site's graph, smallest first, as stable ids; the site's entities and id are untouched."""
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws), loops=[((5, 6, 7, 8), 40.0), ((1, 2, 3, 4), 4.0)])
    verdict = _verdict(MemberResult("gmsh", "pass"), hard=[Finding("open_shell", ["H1"], {"free_edges": 8})])
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.id == "S@open" and site.entities == []
    assert site.graph["free_loops"] == [{"edges": ["E1", "E2", "E3", "E4"], "span_mm": 4.0},
                                        {"edges": ["E5", "E6", "E7", "E8"], "span_mm": 40.0}]
    assert site.graph["diagonal_mm"] == 100.0
    seen = observation(ws, verdict, [site], {"usd": 1.0})["sites"][0]["graph"]
    assert seen["free_loops"][0]["edges"] == ["E1", "E2", "E3", "E4"]  # the proposer is told which edges to name


def test_no_free_loop_call_is_made_for_a_site_that_is_not_open() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    find_sites(pool, ws, _verdict(_fail("gmsh", "overlapping_facets", Located("F1", None, "s1"))), h_mm=H)
    assert "sites_free_loops" not in pool.calls


def test_a_failed_free_loop_call_degrades_the_site_to_no_loops_not_to_no_site() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws), fail=("sites_free_loops",))
    verdict = _verdict(MemberResult("gmsh", "pass"), hard=[Finding("open_shell", ["H1"], {"free_edges": 4})])
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "open" and site.graph["free_loops"] == []


def test_a_whole_part_site_records_which_finding_put_it_there() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(MemberResult("gmsh", "pass"), hard=[Finding("no_solids", [], {"free_shells": 1})])
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "invalid" and site.graph["hard_codes"] == ["no_solids"]


def test_a_located_fingerprint_is_translated_to_the_live_face_it_names() -> None:
    """The REAL gmsh member reports `Located.entity` as the face's FINGERPRINT (`mesh_child._located`,
    `identity.fingerprint_of` over the surface's box), not a stable id. `find_sites` read it as a stable id, found it
    not live, and dropped every surface location (lane C4: the smoke's `open_loop` file lost its open site)."""
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "open_loop", Located(ws.ids["F3"], None, "surface 7")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "open" and site.entities == ["F3"]


def test_a_fingerprint_no_live_face_carries_is_still_unlocated() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    (site,) = find_sites(pool, ws, _verdict(_fail("gmsh", "open_loop", Located("face:0000dead", None, "s"))), h_mm=H)
    assert site.graph.get("coarse") and site.entities == ["B1"]  # no live face: the whole body, never a guessed face


def test_a_fingerprint_one_half_cell_rounding_away_still_names_its_face() -> None:
    """gmsh and the kernel measure the same box to ~1e-12 mm, but a coordinate exactly on a half-cell (3.0 mm on a
    0.000128 grid is 23437.5 cells) rounds either way, so the fingerprints differ. Measured on a filleted box: 2 of 8
    surfaces. The face's own box, rounded both ways at such a coordinate, must still name it."""
    import dataclasses

    from meshpipeline.cad.repair.identity import fingerprint_of

    ws = dataclasses.replace(_ws(), quantum=0.5)
    boxes = _boxes(ws)
    boxes["F3"] = [30.25, 0.0, 0.0, 31.0, 1.0, 1.0]      # x0 = 60.5 cells: on the boundary
    pool = ScriptedPool(ws, boxes)
    gmsh_box = {"bbox_min": [30.25 + 1e-12, 0.0, 0.0], "bbox_max": [31.0, 1.0, 1.0]}
    fp = fingerprint_of("face", gmsh_box, quantum=0.5)
    assert fp != fingerprint_of("face", {"bbox_min": boxes["F3"][:3], "bbox_max": boxes["F3"][3:]}, quantum=0.5)
    (site,) = find_sites(pool, ws, _verdict(_fail("gmsh", "open_loop", Located(fp, None, "surface 3"))), h_mm=H)
    assert site.kind == "open" and site.entities == ["F3"]


def test_a_coarse_site_takes_the_failing_members_kind_and_the_parts_small_entities() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    small = Finding("small_face", ["F4"], {"width": 1e-4})
    (site,) = find_sites(pool, ws, _verdict(_fail("gmsh_prod", "low_quality"), soft=[small]), h_mm=H)
    assert site.kind == "proximity" and site.graph["coarse"]
    assert [x["entity"] for x in site.graph["soft"]] == ["F4"]


def test_a_layer1_finding_that_names_only_a_body_is_marked_coarse() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    (site,) = find_sites(pool, ws, _verdict(hard=[Finding("self_intersection", ["S1"], {})]), h_mm=H)
    assert site.kind == "self_overlap" and site.entities == ["B1"] and site.graph["coarse"]


def test_an_unlocated_member_beside_a_located_one_still_gets_its_coarse_site() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh_prod", "overlapping_facets", Located("F1", None, "s1")), _fail("netgen", "other"))
    sites = find_sites(pool, ws, verdict, h_mm=H)
    assert sorted(s.kind for s in sites) == ["invalid", "self_overlap"]
    assert [s.graph["coarse"]["members"] for s in sites if s.graph.get("coarse")] == [["netgen"]]


def test_an_unbounded_input_is_an_unbounded_site_the_trim_can_answer() -> None:
    """Layer 1's `unbounded_geometry` had no site kind, so nothing in the search could aim at it (lane C7)."""
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    (site,) = find_sites(pool, ws, _verdict(hard=[Finding("unbounded_geometry", [], {"diagonal_mm": 2e100})]), h_mm=H)
    assert site.kind == "unbounded" and site.graph["hard_codes"] == ["unbounded_geometry"]


def test_mesher_faces_are_only_the_faces_a_mesher_located_not_the_proximity_or_layer1_context() -> None:
    """D16: site-targeted defeature aims at `graph["mesher_faces"]`: F1, which gmsh named; not F2, a proximity
    neighbour, and not F3, which only layer 1 named."""
    ws = _ws()
    boxes = _boxes(ws)
    boxes["F2"] = [10.0, 1.5, 0.0, 11.0, 2.5, 1.0]
    boxes["F3"] = [10.0, 3.0, 0.0, 11.0, 4.0, 1.0]
    pool = ScriptedPool(ws, boxes, prox=[(1, 2, 0.3)])
    verdict = _verdict(_fail("gmsh", "other", Located("F1", None, "surf 1")),
                       hard=[Finding("self_intersection", ["F3"], {})])
    sites = find_sites(pool, ws, verdict, h_mm=H)
    site = next(s for s in sites if "F1" in s.entities)
    assert {"F1", "F2"} <= set(site.entities)
    assert site.graph["mesher_faces"] == ["F1"]


def test_a_located_box_is_named_on_the_workspaces_grid_not_the_meshers() -> None:
    """D17: a member fingerprints a face on ITS grid (quantum_for of its own diagonal); the workspace names faces on
    its own, 2x different when the diagonals straddle a power of two. With the box, the face is still found."""
    import dataclasses

    from meshpipeline.cad.repair.identity import fingerprint_of

    base = _ws()
    boxes = _boxes(base)
    q = 1.28e-4

    def fp(sid: str, quantum: float) -> str:
        b = boxes[sid]
        return fingerprint_of("face", {"bbox_min": b[:3], "bbox_max": b[3:]}, quantum=quantum)

    ids = {**base.ids, **{f"F{i}": fp(f"F{i}", q) for i in range(1, 13)}}
    ws = dataclasses.replace(base, ids=ids, quantum=q)
    pool = ScriptedPool(ws, boxes)
    wrong_grid = fp("F5", q / 2)                    # what the mesher wrote: the right box, the wrong grid
    assert wrong_grid not in ids.values()
    box = tuple(boxes["F5"])
    (site,) = find_sites(pool, ws, _verdict(_fail("gmsh_prod", "other", Located(wrong_grid, None, "surface 9", box))),
                         h_mm=H)
    assert site.entities == ["F5"] and site.graph["mesher_faces"] == ["F5"]
    # without the box the old behaviour stands: the wrong-grid name places nothing
    (blind,) = find_sites(pool, ws, _verdict(_fail("gmsh_prod", "other", Located(wrong_grid, None, "surface 9"))),
                          h_mm=H)
    assert "F5" not in blind.entities

# --- lane C10 (2): body ops only where the evidence names bodies -----------------------------------------------------

BODY_OPS = {"conformalise", "fuse", "remove_duplicate_faces"}


def _propose_for(ws: Workspace, sites: list) -> list:
    from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState

    state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=sites, depth=0, parent=None,
                        via=None)
    return DetGenerator(diagonal=lambda w: 100.0).propose(state, 50)


def _two_bodies() -> Workspace:
    ws = _ws()
    ids = {**ws.ids, "B2": "fpB2"}
    return Workspace(brep_path=ws.brep_path, ids=ids, labels=ws.labels, history=[], original=ws.original,
                     indices={**ws.indices, "B2": 2})


def test_the_v2_case_a_valid_two_body_part_with_an_unlocated_plc_gets_no_body_op() -> None:
    """dev abc/0051_00514513 (v2, lane B13): valid, two bodies, gmsh_default fails PLC with no point. C5 made that a
    coarse whole-part 'intersection' site naming every body, and the sweep fragmented them: 11 ops, all no-ops."""
    ws = _two_bodies()
    pool = ScriptedPool(ws, _boxes(ws))
    (site,) = find_sites(pool, ws, _verdict(_fail("gmsh_default", "edge_through_face")), h_mm=H)
    assert site.kind == "intersection" and site.graph["coarse"]["whole_part"] is True
    assert not BODY_OPS & {c.op for c in _propose_for(ws, [site])}


def test_a_layer1_overlap_naming_its_bodies_keeps_the_body_ops() -> None:
    ws = _two_bodies()
    boxes = _boxes(ws)
    boxes["B2"] = [10.5, 0.0, 0.0, 11.5, 1.0, 1.0]  # overlapping bodies sit together: one site
    pool = ScriptedPool(ws, boxes)
    (site,) = find_sites(pool, ws, _verdict(hard=[Finding("body_overlap", ["S1", "S2"], {})]), h_mm=H)
    assert site.kind == "overlap" and site.graph["coarse"]["whole_part"] is False
    ops = {c.op for c in _propose_for(ws, [site])}
    assert {"conformalise", "fuse"} <= ops
