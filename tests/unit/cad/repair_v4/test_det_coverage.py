# Responsibility: The deterministic sweep covers every op the AI proposer can reach, so `search_ai - search_det`
#                 measures the model's JUDGEMENT, not its exclusive access to an op. Pins, per op that used to be
#                 AI-only (`rebuild_face`, `rebuild_edge_as_intersection`, `reapproximate`, `refit_cluster`, and
#                 `fix_poles` / `to_canonical`, which ran only in the pre-search staged healer): the site
#                 kinds and evidence that make DetGenerator propose it, the fixed sweep, that every proposal is inside
#                 the op's own schema, that nothing is proposed without the evidence, and that the op is reached
#                 through `search()` on the search_det arm. Pins the WHOLE registry too: every op the AI can request
#                 is either swept or listed in `DET_UNREACHABLE_OPS` with its reason, so a new op cannot silently
#                 become an AI-only advantage.
# Boundaries: hermetic: fake sites and workspaces; the kernel side is the native tier
#             (tests/native/test_native_repair_v4_det_coverage.py).
from __future__ import annotations

from typing import Any

import pytest
from tests.unit.cad.repair_v4.test_search_wiring import World, make_ws, run, world  # noqa: F401  (world: autouse)

from meshpipeline.cad.repair_v4 import search
from meshpipeline.cad.repair_v4.ops import REGISTRY, validate_call
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Workspace

#: The registry as the real op modules file it, snapshotted at IMPORT time: the autouse `world` fixture adds a fake
#: op to `REGISTRY` for every test in this module, and the coverage claim is about the real ops.
REAL_OPS = frozenset(REGISTRY)

DIAG = 120.0          # mm: bound = min(0.1, 0.001 x 120) = 0.1 mm
BOUND = 0.1
FACES = [f"F{i}" for i in range(1, 9)]
EDGES = [f"E{i}" for i in range(1, 13)]


def _site(kind: str, entities: list[str], **graph: Any) -> Site:
    return Site(id=f"S@{kind}", kind=kind, entities=list(entities), xyz=[], meshers_failing=[], dist_over_h=None,
                graph=dict(graph), view_png=None)  # type: ignore[arg-type]


def _ws(*, solids: tuple[str, ...] = ("B1",)) -> Workspace:
    ids = {**{f: f"fp{f}" for f in FACES}, **{e: f"fp{e}" for e in EDGES}, **{b: f"fp{b}" for b in solids}}
    return Workspace(brep_path="/fake/a.brep", ids=ids, labels={}, history=[], original="/fake/o.brep",
                     indices={sid: i for i, sid in enumerate(ids, start=1)})


def _propose(sites: list[Site], *, diag: float | None = DIAG,
             ws: Workspace | None = None) -> tuple[list, DetGenerator]:
    state = SearchState(id="S0", ws=ws or _ws(), verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=sites, depth=0,
                        parent=None, via=None)
    gen = DetGenerator(diagonal=(lambda w: diag) if diag is not None else None)
    return gen.propose(state, 50), gen


def _of(cands: list, op: str) -> list[tuple[list[str], dict]]:
    return [(list(c.targets), dict(c.params)) for c in cands if c.op == op]


def node(fid: str, surface: str, span: float | None = None) -> dict:
    return {"id": fid, "hop": 0, "surface": surface, "area": 10.0, "min_width": 1.0, "angular_span_deg": span}


def edge(eid: str, faces: list[str], tol: float, gap: float | None = 0.0, kind: str | None = None) -> dict:
    e: dict = {"edge": eid, "faces": faces, "tolerance": tol}
    if kind:
        e["kind"] = kind
    else:
        e.update(gap=gap, continuity="C0", dihedral_deg=90.0)
    return e


# --- rebuild_face -----------------------------------------------------------------------------------------------------

def test_a_self_overlap_site_gets_rebuild_face_on_its_first_two_faces_at_g0() -> None:
    got, _ = _propose([_site("self_overlap", ["F1", "F2", "F3"])])
    assert _of(got, "rebuild_face") == [(["F1"], {"continuity": "G0"}), (["F2"], {"continuity": "G0"})]


def test_an_invalid_site_gets_rebuild_face_only_with_a_self_intersection_finding() -> None:
    plain, _ = _propose([_site("invalid", ["F1"])])
    assert _of(plain, "rebuild_face") == []
    bad, _ = _propose([_site("invalid", ["F1"], hard_codes=["self_intersection"])])
    assert _of(bad, "rebuild_face") == [(["F1"], {"continuity": "G0"})]


# --- rebuild_edge_as_intersection ---------------------------------------------------------------------------------

def test_an_edge_whose_tolerance_or_gap_exceeds_the_normalise_target_is_rebuilt_worst_first() -> None:
    edges = [edge("E5", ["F1", "F2"], tol=0.01), edge("E6", ["F2", "F3"], tol=1e-7, gap=1e-7),
             edge("E7", ["F1"], tol=0.5, kind="free"), edge("E8", ["F1", "F3"], tol=1e-7, gap=0.05),
             edge("E9", ["F3", "F4"], tol=0.003)]
    got, _ = _propose([_site("proximity", ["F1", "F2"], edges=edges)])
    # target = 1e-5 x 120 mm = 0.0012 mm; E6 is fine, E7 is a free edge (no two faces to intersect)
    assert _of(got, "rebuild_edge_as_intersection") == [(["E8"], {}), (["E5"], {})]


def test_no_edge_rebuild_without_a_diagonal_or_outside_the_edge_kinds() -> None:
    edges = [edge("E5", ["F1", "F2"], tol=0.01)]
    none, gen = _propose([_site("proximity", ["F1"], edges=edges)], diag=None)
    assert _of(none, "rebuild_edge_as_intersection") == []
    assert any("rebuild_edge_as_intersection not proposed" in n for n in gen.notes)
    sliver, _ = _propose([_site("sliver", ["F1"], edges=edges)])
    assert _of(sliver, "rebuild_edge_as_intersection") == []


# --- reapproximate ------------------------------------------------------------------------------------------------

def test_reapproximate_sweeps_only_free_form_faces_inside_the_bound() -> None:
    nodes = [node("F1", "bsplinesurface"), node("F2", "plane"), node("F3", "beziersurface")]
    got, _ = _propose([_site("periodic", ["F1", "F2", "F3"], nodes=nodes)])
    assert _of(got, "reapproximate") == [(["F1", "F3"], {"tol_mm": 0.25 * BOUND, "max_degree": 9}),
                                         (["F1", "F3"], {"tol_mm": 0.5 * BOUND, "max_degree": 6})]


def test_reapproximate_is_not_offered_for_analytic_faces_or_unmeasured_ones() -> None:
    planes, gen = _propose([_site("proximity", ["F1", "F2"], nodes=[node("F1", "plane"), node("F2", "cylinder")])])
    assert _of(planes, "reapproximate") == []
    assert any("reapproximate not proposed" in n for n in gen.notes)
    unmeasured, _ = _propose([_site("self_overlap", ["F1"])])  # no graph: the surface type is unknown
    assert _of(unmeasured, "reapproximate") == []


# --- refit_cluster ------------------------------------------------------------------------------------------------

def _cluster(*faces: str, seam: str | None = None) -> dict:
    nodes = [node(f, "cylinder", 360.0) if f == seam else node(f, "plane") for f in faces]
    edges = [edge(f"E{i + 1}", [a, b], tol=1e-7) for i, (a, b) in enumerate(zip(faces, faces[1:]))]
    return {"nodes": nodes, "edges": edges}


def test_a_connected_cluster_of_two_to_six_faces_is_refit_mildest_first() -> None:
    got, _ = _propose([_site("sliver", ["F1", "F2", "F3"], **_cluster("F1", "F2", "F3"))])
    assert _of(got, "refit_cluster") == [(["F1", "F2", "F3"], {"tol_mm": 0.5 * BOUND}),
                                         (["F1", "F2", "F3"], {"tol_mm": BOUND})]


@pytest.mark.parametrize("why, site", [
    ("one face", _site("sliver", ["F1"], **_cluster("F1"))),
    ("seven faces", _site("overlap", FACES[:7], **_cluster(*FACES[:7]))),
    ("not connected", _site("sliver", ["F1", "F2", "F3"], **_cluster("F1", "F2"))),
    ("a seam face", _site("self_overlap", ["F1", "F2"], **_cluster("F1", "F2", seam="F2"))),
    ("no graph", _site("sliver", ["F1", "F2"])),
])
def test_refit_cluster_is_not_offered_where_the_op_would_decline(why: str, site: Site) -> None:
    got, _ = _propose([site])
    assert _of(got, "refit_cluster") == [], why


# --- fix_poles ----------------------------------------------------------------------------------------------------

def test_a_pole_site_gets_fix_poles_one_face_at_a_time() -> None:
    got, _ = _propose([_site("pole", ["F1", "F2", "F3"])])
    assert _of(got, "fix_poles") == [(["F1"], {}), (["F2"], {})]


def test_fix_poles_needs_a_face_and_an_observed_pole() -> None:
    no_face, gen = _propose([_site("pole", ["E1"])])
    assert _of(no_face, "fix_poles") == []
    assert any("fix_poles not proposed" in n for n in gen.notes)
    # `ops.edges.FixPoles.applies_to` also allows periodic and invalid, but ShapeFix_Face RAISES when it changes
    # nothing, so the sweep stays on the kind a mesher actually reported a pole for.
    for kind in ("periodic", "invalid", "self_overlap"):
        other, _ = _propose([_site(kind, ["F1"])])
        assert _of(other, "fix_poles") == [], kind


# --- to_canonical -------------------------------------------------------------------------------------------------

def test_to_canonical_snaps_one_free_form_face_per_candidate_at_half_the_bound() -> None:
    nodes = [node("F1", "bsplinesurface"), node("F2", "plane"), node("F3", "othersurface"),
             node("F4", "surfaceofrevolution")]
    got, _ = _propose([_site("sliver", ["F1", "F2", "F3", "F4"], nodes=nodes)])
    assert _of(got, "to_canonical") == [(["F1"], {"tol_mm": 0.5 * BOUND}), (["F3"], {"tol_mm": 0.5 * BOUND})]


def test_to_canonical_is_not_offered_for_analytic_faces_or_without_a_diagonal() -> None:
    analytic, gen = _propose([_site("invalid", ["F1", "F2"],
                                    nodes=[node("F1", "plane"), node("F2", "cylinder")])])
    assert _of(analytic, "to_canonical") == []
    assert any("to_canonical not proposed" in n for n in gen.notes)
    nodiag, no_size = _propose([_site("sliver", ["F1"], nodes=[node("F1", "bsplinesurface")])], diag=None)
    assert _of(nodiag, "to_canonical") == []
    assert any("to_canonical not proposed" in n for n in no_size.notes)
    wrong_kind, _ = _propose([_site("periodic", ["F1"], nodes=[node("F1", "bsplinesurface")])])
    assert _of(wrong_kind, "to_canonical") == []   # not one of ToCanonical.site_kinds


# --- the whole registry: swept, or AI-only ON THE RECORD ----------------------------------------------------------

def _one_site_per_kind() -> list[Site]:
    """A site of every kind the sweep knows, each carrying the facts its rows are gated on."""
    free = [node(f, "bsplinesurface") for f in ("F1", "F2")]
    bad_edge = [edge("E1", ["F1", "F2"], tol=0.01)]
    return [
        _site("open", ["F1", "F2"], free_loops=[{"edges": ["E1", "E2"], "span_mm": 1.0}]),
        _site("sliver", ["F1", "F2"], **_cluster("F1", "F2")),
        _site("sliver", ["F1", "F2"], nodes=free),
        _site("invalid", ["F1", "F2", "B1"], hard_codes=["self_intersection", "no_solids"], nodes=free,
              edges=bad_edge),
        _site("proximity", ["F1", "F2"], nodes=free, edges=bad_edge),
        _site("periodic", ["F1", "F2"], nodes=free),
        _site("pole", ["F1", "F2"], nodes=free),
        _site("self_overlap", ["F1", "F2"], nodes=free, edges=bad_edge),
        _site("overlap", ["F1", "F2", "B1", "B2"], nodes=free, edges=bad_edge),
        _site("intersection", ["F1", "F2", "B1", "B2"], edges=bad_edge),
        _site("unbounded", []),
    ]


def test_the_det_sweep_reaches_every_registry_op_except_the_ones_it_explains() -> None:
    swept: set[str] = set()
    for site in _one_site_per_kind():          # one site at a time: a row dropped for another site's facts
        got, _ = _propose([site], ws=_ws(solids=()))   # no solid, so `sew` is not predicted to decline
        swept |= {c.op for c in got}
    assert "t18_set" not in REAL_OPS                # the snapshot is the real registry, not the fixture's fake
    assert REAL_OPS - swept == set(search.DET_UNREACHABLE_OPS) - {search.RUN_STAGE}
    for op, why in search.DET_UNREACHABLE_OPS.items():
        assert op not in swept and len(why) > 40, op   # listed means NOT swept, with a reason worth reading


# --- every proposal is a valid call, and each op is reached through search() ---------------------------------------

def _every_site() -> list[Site]:
    nodes = [node("F1", "bsplinesurface"), node("F2", "plane")]
    return [_site("self_overlap", ["F1", "F2"], nodes=nodes, edges=[edge("E1", ["F1", "F2"], tol=0.01)]),
            _site("sliver", ["F3", "F4"], **_cluster("F3", "F4")),
            _site("pole", ["F3"]),
            _site("invalid", ["F4"], nodes=[node("F4", "bsplinesurface")])]


def test_every_new_proposal_is_inside_its_ops_own_schema() -> None:
    got, _ = _propose(_every_site())
    new = [c for c in got if c.op in search.DET_COVERED_OPS]
    assert {c.op for c in new} == set(search.DET_COVERED_OPS)
    for c in new:
        assert validate_call(c.op, list(c.targets), dict(c.params), _ws()) == [], (c.op, c.params)


#: the smallest site that earns each covered op its row, so the op has to be reached at the PRODUCTION
#: `k_propose` (12 per expansion) rather than at a cap raised for the test
_REACH_CASES = {
    "rebuild_face": _site("self_overlap", ["F1"]),
    "rebuild_edge_as_intersection": _site("proximity", ["F1"], edges=[edge("E1", ["F1", "F2"], tol=0.01)]),
    "reapproximate": _site("periodic", ["F1"], nodes=[node("F1", "bsplinesurface")]),
    "refit_cluster": _site("sliver", ["F1", "F2"], **_cluster("F1", "F2")),
    "fix_poles": _site("pole", ["F1"]),
    "to_canonical": _site("invalid", ["F1"], nodes=[node("F1", "bsplinesurface")]),
}


@pytest.mark.parametrize("op", search.DET_COVERED_OPS)
def test_each_covered_op_is_reached_from_the_search_det_arm(op: str) -> None:
    gens, proposer = search.arm_generators("search_det")
    assert proposer is None
    for g in gens:
        if isinstance(g, DetGenerator):
            g.diagonal = lambda w: DIAG
    r = run(gens, World(target=3, sites_fn=lambda pool, ws, v: [_REACH_CASES[op]]),
            start=make_ws(0, ids=("F1", "F2", "E1")),
            budget=search.Budget(wall_s=60, ops=60, model_calls=0, model_usd=0.0))
    reached = {e["op"] for e in r.trace.of_kind("candidate") if e.get("source") == "det"}
    assert op in reached, reached


# --- lane C4: why search_det repaired 0 of 30 in the smoke --------------------------------------------------------


class _DeclinesInParams:
    """An op that declines the way the body ops do: an UNCHANGED workspace and `diff.params["declined"]`."""

    name = "c4_param_decline"
    params_schema = {"type": "object", "properties": {}, "additionalProperties": False,
                     "x-targets": {"kind": "face", "min": 0}}

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        from meshpipeline.cad.repair_v4.workspace import Diff

        return ws, Diff(op=self.name, params={"declined": "unresolved_overlap", "detail": {"volume": 2.5}},
                        modified={}, generated={}, removed=[], declared_added=[], declared_removed=[], seconds=0.0)


def test_a_decline_carried_in_the_diff_is_a_decline_with_its_reason_not_a_duplicate_state(monkeypatch: Any) -> None:
    """conformalise / remove_duplicate_faces answer a decline with the unchanged workspace and the reason in the
    Diff. The search took that as applied, spent a cheap verdict on the unchanged shape, and recorded a DUPLICATE: the
    smoke's overlap files showed `conformalise: duplicate` with no reason at all."""
    from tests.unit.cad.repair_v4.test_search_wiring import Scripted

    from meshpipeline.cad.repair_v4.ops import REGISTRY

    monkeypatch.setitem(REGISTRY, "c4_param_decline", _DeclinesInParams())
    gen = Scripted(lambda s: [search.Candidate("c4_param_decline", [], {}, "det")] if s.depth == 0 else [])
    w = World(target=3)
    r = run([gen], w, start=make_ws(0), budget=search.Budget(wall_s=60, ops=10, model_calls=0, model_usd=0.0))
    [e] = [e for e in r.trace.of_kind("candidate") if e["op"] == "c4_param_decline"]
    assert e["outcome"] == "rejected" and e["stage"] == "declined"
    assert e["errors"][0].startswith("unresolved_overlap")


def _overlap(*solids: str) -> Site:
    return _site("overlap", list(solids))


def test_an_overlap_site_sweeps_cut_by_both_ways_for_each_body_pair() -> None:
    """fragment mode declines EVERY real overlap (`unresolved_overlap`: no keeper); only `cut_by:<B>` can resolve one,
    and the kernel still refuses an overlap deeper than the bound or a volume change over its cap. 10 of the smoke's
    30 files carry body_overlap; det never proposed cut_by, so only the AI could."""
    got, _ = _propose([_overlap("B1", "B2")], ws=_ws(solids=("B1", "B2")))
    cuts = [(t, p) for t, p in _of(got, "conformalise") if p["mode"].startswith("cut_by:")]
    assert cuts == [(["B1", "B2"], {"mode": "cut_by:B1", "fuzzy_mm": 0.0}),
                    (["B1", "B2"], {"mode": "cut_by:B2", "fuzzy_mm": 0.0})]


def test_cut_by_is_swept_on_at_most_three_body_pairs() -> None:
    got, _ = _propose([_overlap("B1", "B2", "B3", "B4")], ws=_ws(solids=("B1", "B2", "B3", "B4")))
    cuts = [p["mode"] for _, p in _of(got, "conformalise") if p["mode"].startswith("cut_by:")]
    assert len(cuts) == 6


# --- lane C5: a coarse (whole-part) site gets part-wide conditioning, not nothing ---------------------------------


def _coarse(kind: str = "invalid", soft: list | None = None) -> Site:
    return _site(kind, ["B1"], coarse={"reason": "gmsh_prod failed without saying where", "members": ["gmsh_prod"]},
                 soft=soft or [])


def test_a_coarse_site_sweeps_part_wide_conditioning_stronger_than_the_staged_healer() -> None:
    got, _ = _propose([_coarse()])
    assert _of(got, "split_angle") == [(FACES, {"max_deg": 60}), (FACES, {"max_deg": 45})]
    assert _of(got, "split_continuity") == [(FACES, {"order": 2})]


def test_a_coarse_site_removes_exactly_the_small_entities_the_checker_found() -> None:
    soft = [{"code": "small_face", "entity": "F4", "detail": {}}, {"code": "small_edge", "entity": "E2", "detail": {}},
            {"code": "large_tolerance", "entity": "E3", "detail": {}}]
    got, _ = _propose([_coarse(soft=soft)])
    rows = _of(got, "remove_small")
    assert rows and all(t == ["F4", "E2"] for t, _ in rows)  # never a wide entity: the op refuses those


def test_a_located_site_gets_no_part_wide_rows() -> None:
    got, _ = _propose([_site("invalid", ["F1"])])
    assert _of(got, "split_angle") == [] and _of(got, "remove_small") == []


def test_every_coarse_proposal_is_inside_its_ops_schema() -> None:
    soft = [{"code": "small_face", "entity": "F4", "detail": {}}]
    got, _ = _propose([_coarse(soft=soft)])
    for c in got:
        assert validate_call(c.op, list(c.targets), dict(c.params), _ws()) == [], (c.op, c.params)


def test_part_wide_rows_wait_while_any_site_is_placed() -> None:
    """Aim where there is evidence; go part-wide only when there is none. The coarse rows beside a placed open site
    turned Task 47's one-hole repair from 5 ops into 11 (production allows 6)."""
    placed = _site("open", [], free_loops=[{"edges": ["E1", "E2", "E3"], "span_mm": 2.0}], diagonal_mm=DIAG)
    got, _ = _propose([placed, _coarse()])
    assert _of(got, "split_angle") == [] and _of(got, "split_continuity") == []
    alone, _ = _propose([_coarse()])
    assert _of(alone, "split_angle")


# --- lane C7: fuse, cut_by at any depth, trim_unbounded (the owner's 2026-11-24 goal) -----------------------------


def test_an_overlap_site_sweeps_both_cuts_then_a_fuse_for_each_pair() -> None:
    got, _ = _propose([_overlap("B1", "B2")], ws=_ws(solids=("B1", "B2")))
    assert _of(got, "fuse") == [(["B1", "B2"], {"fuzzy_mm": 0.0})]
    modes = [p["mode"] for _, p in _of(got, "conformalise")]
    assert modes.index("cut_by:B1") < len(modes) and "cut_by:B2" in modes  # the det arm tries which body yields


def test_an_unbounded_site_sweeps_trim_unbounded_at_raymonds_pad_then_a_wider_one() -> None:
    got, _ = _propose([_site("unbounded", [])])
    assert _of(got, "trim_unbounded") == [([], {"pad_fraction": 0.01}), ([], {"pad_fraction": 0.05})]


def test_the_c7_proposals_are_inside_their_ops_schemas() -> None:
    got, _ = _propose([_overlap("B1", "B2"), _site("unbounded", [])], ws=_ws(solids=("B1", "B2")))
    for c in got:
        if c.op in ("fuse", "trim_unbounded", "conformalise"):
            assert validate_call(c.op, list(c.targets), dict(c.params), _ws(solids=("B1", "B2"))) == [], c


def test_fuse_and_trim_unbounded_are_registered_with_bounded_schemas() -> None:
    from meshpipeline.cad.repair_v4.ops import REGISTRY

    assert REGISTRY["fuse"].params_schema["x-targets"] == {"kind": "solid", "min": 2, "max": None}
    assert REGISTRY["trim_unbounded"].params_schema["properties"]["pad_fraction"] == {
        "type": "number", "minimum": 0.001, "maximum": 0.25}
    assert validate_call("fuse", ["B1"], {}, _ws(solids=("B1", "B2")))  # one body is not a fuse
    assert validate_call("trim_unbounded", ["F1"], {"pad_fraction": 0.01}, _ws())  # it takes no targets
