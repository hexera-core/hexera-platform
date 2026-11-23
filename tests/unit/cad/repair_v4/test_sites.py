# Responsibility: Verify failure-site detection and the proposer's observation, hermetically: how
#                 located failures cluster into sites, which warnings attach, what the model is told.
# Boundaries: hermetic - a scripted pool stands in for the kernel workers (boxes are a table), and
#             Verdict/MemberResult/Located are local stand-ins with the contract's field names.
#             The geometry itself is proved in tests/native/test_native_repair_v4_sites.py.
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.sites import OBSERVATION_MAX_BYTES, find_sites, observation
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.verdict.valid import Finding, ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Workspace

H = 1.0


@dataclass(frozen=True)
class Located:
    entity: str | None
    xyz: tuple[float, float, float] | None
    mesher_tag: str


@dataclass(frozen=True)
class MemberResult:
    name: str
    status: str
    error_kind: str | None = None
    message: str = ""
    located: list[Located] = field(default_factory=list)
    min_sicn: float | None = None
    elements: int | None = None
    seconds: float = 0.0


@dataclass(frozen=True)
class Verdict:
    valid: ValidReport
    panel: list[MemberResult]
    faithful: Any = None
    clean: bool = False
    tier: str = "fail"


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=0.0)


class ScriptedPool:
    """Boxes by stable-id; face i is the unit cube at x = 10 * i unless overridden."""

    def __init__(self, ws: Workspace, boxes: dict[str, list[float]], *, locate=None, fail=(),
                 prox=(), distance=None) -> None:
        self.ws, self.boxes, self.locate, self.fail = ws, boxes, locate or {}, set(fail)
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
            return _ok({"faces": [self.locate.get(tuple(p), []) for p in payload["points"]]})
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
        raise AssertionError(f"unscripted kernel call {fn}")


def _ws(n_faces: int = 12, *, shift: int = 0) -> Workspace:
    """Stable id F<i> sits at kernel index i + shift."""
    ids = {f"F{i}": f"fp{i}" for i in range(1, n_faces + 1)}
    ids["B1"] = "fpB"
    indices = {f"F{i}": i + shift for i in range(1, n_faces + 1)}
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
    return Verdict(_valid(hard=hard, soft=soft), list(members))


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


def test_failure_without_location_is_one_unlocated_site_not_dropped() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("gmsh", "other"), MemberResult("netgen", "crashed", "crash", "boom"))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "unlocated" and site.entities == [] and site.meshers_failing == ["gmsh", "netgen"]


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
    assert site.entities == [] and site.kind == "unlocated"


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
    verdict = Verdict(_valid(), members, faithful=huge)
    obs = observation(ws, verdict, [], {"note": "y" * 500_000})
    assert obs["oversize"] is True and obs["trimmed"] is True
    assert len(json.dumps(obs).encode()) < 60_000
    small = observation(ws, _verdict(MemberResult("gmsh", "pass")), [], {"usd": 1})
    assert "oversize" not in small and small["trimmed"] is False
    assert "truncated" in observation(ws, Verdict(_valid(), [], faithful=huge), [], {})["faithful"]


def test_unknown_error_kind_is_pinned_to_invalid() -> None:
    ws = _ws()
    pool = ScriptedPool(ws, _boxes(ws))
    verdict = _verdict(_fail("netgen", "some_new_kind", Located("F1", None, "a")))
    (site,) = find_sites(pool, ws, verdict, h_mm=H)
    assert site.kind == "invalid"
