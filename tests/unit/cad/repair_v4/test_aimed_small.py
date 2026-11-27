"""D18: unify_same_domain and remove_small AIMED at the sliver/narrow faces of a mesher's located failure.

Lane A11's evidence: 21 of its 35 located failures are slivers (>= 20:1) or narrow faces (8-20:1). A low_quality
location names the worst ELEMENT, not necessarily the bad geometry (263d5e2d5bab: both members name face 5 while the
2995:1 sliver is face 9). The kernel half is tests/native/test_native_repair_v4_aimed_small.py.
"""
from __future__ import annotations

from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Workspace


def _node(fid: str, hop: int, area: float, width: float) -> dict:
    return {"id": fid, "hop": hop, "surface": "plane", "area": area, "min_width": width}


def _site(kind: str, named: list[str], nodes: list[dict], edges: list[tuple[str, str]], error_kind: str,
          failing: tuple[str, ...] = ("gmsh_alt",)) -> Site:
    graph = {"mesher_faces": named, "nodes": nodes, "edges": [{"edge": f"E{i + 1}", "kind": "smooth", "faces": list(e)} for i, e in enumerate(edges)],
             "failures": [{"mesher": m, "error_kind": error_kind} for m in failing]}
    return Site(id="S1", kind=kind, entities=sorted({n["id"] for n in nodes if n["hop"] == 0}), xyz=[],  # type: ignore[arg-type]
                meshers_failing=list(failing), dist_over_h=None, graph=graph, view_png=None)


def _state(sites: list[Site], solid: bool = True) -> SearchState:
    ids = {f"F{i}": f"fp{i}" for i in range(1, 20)}
    if solid:
        ids["B1"] = "fpB"
    st = SearchState.__new__(SearchState)
    st.ws = Workspace(brep_path="/w/s.brep", ids=ids, labels={}, history=[], original="/w/s.brep")  # type: ignore[attr-defined]
    st.sites, st.lineage, st.depth = sites, (), 0  # type: ignore[attr-defined]
    st.id, st.score, st.parent, st.via = "S0", None, None, None  # type: ignore[attr-defined]  (B21 reads them)
    return st


def _aimed(cands: list) -> list:
    return [c for c in cands if c.op in ("unify_same_domain", "remove_small") and "(site S1" in c.rationale
            and ("named by the mesher" in c.rationale or "beside the face" in c.rationale)]


# face 5: named, 46:1; face 9: a neighbour, 2995:1; face 7: neighbour, square; face 11: hop 2, a sliver too far away
A11 = [_node("F5", 0, 46.0, 1.0), _node("F9", 1, 2995.0 * 0.01, 0.1), _node("F7", 1, 100.0, 10.0),
       _node("F11", 2, 5000.0 * 0.01, 0.1)]
EDGES = [("F5", "F9"), ("F5", "F7"), ("F9", "F7"), ("F9", "F11")]


def test_at_a_low_quality_site_the_worst_sliver_comes_first_even_if_the_mesher_did_not_name_it() -> None:
    cands = _aimed(DetGenerator(diagonal=lambda ws: 100.0).propose(
        _state([_site("proximity", ["F5"], A11, EDGES, "low_quality")]), 999))
    # B20 (owner priority 2026-11-24) re-sorts the rows within the family by AIMEDNESS: a row whose targets are the
    # site's own faces (the face the mesher named) goes ahead of one that reaches into its neighbours. The worst
    # sliver (F9, beside the named face) is still aimed at, both ways; it is no longer first.
    assert [(c.op, c.targets[0]) for c in cands] == [
        ("remove_small", "F5"), ("unify_same_domain", "F9"), ("remove_small", "F9"), ("unify_same_domain", "F5")]
    beside = next(c for c in cands if c.op == "unify_same_domain" and c.targets[0] == "F9")
    assert beside.targets == ["F9", "F11", "F5", "F7"] and beside.params == {"scope": "targets", "keep": []}
    assert "beside the face the mesher named" in beside.rationale and "2995:1 sliver" in beside.rationale
    assert all("F11" != c.targets[0] for c in cands)         # hop 2 is not the site


def test_where_the_mesher_named_the_defect_its_face_comes_first() -> None:
    """overlapping_facets names a surface tag, which IS the face (lane A11, 74/74 ... 843/843)."""
    cands = _aimed(DetGenerator(diagonal=lambda ws: 100.0).propose(
        _state([_site("self_overlap", ["F5"], A11, EDGES, "overlapping_facets", ("gmsh_default",))]), 999))
    assert [c.targets[0] for c in cands][:2] == ["F5", "F5"]


def test_remove_small_is_aimed_with_a_limit_its_guard_accepts_and_only_on_a_solid() -> None:
    site = _site("proximity", ["F5"], A11, EDGES, "low_quality")
    [rs] = [c for c in _aimed(DetGenerator(diagonal=lambda ws: 100.0).propose(_state([site]), 999))
            if c.op == "remove_small" and c.targets == ["F9"]]      # the worst sliver's row (B20 orders F5's first)
    length, width = 2995.0 * 0.01 / 0.1, 0.1
    assert abs(rs.params["max_width_mm"] - 1.05 * (length + width)) < 1e-9
    no_solid = _aimed(DetGenerator(diagonal=lambda ws: 100.0).propose(_state([site], solid=False), 999))
    assert no_solid and all(c.op == "unify_same_domain" for c in no_solid)


def test_nothing_is_aimed_without_a_mesher_location_or_a_narrow_face() -> None:
    g = DetGenerator(diagonal=lambda ws: 100.0)
    assert not _aimed(g.propose(_state([_site("proximity", [], A11, EDGES, "low_quality")]), 999))
    assert not _aimed(g.propose(_state([_site("self_overlap", ["F5"], A11, EDGES, "x", ("valid",))]), 999))
    square = [_node("F5", 0, 100.0, 10.0), _node("F7", 1, 100.0, 10.0)]
    assert not _aimed(g.propose(_state([_site("proximity", ["F5"], square, [("F5", "F7")], "low_quality")]), 999))
    assert not _aimed(g.propose(_state([_site("open", ["F5"], A11, EDGES, "no_volume")]), 999))


def test_the_aimed_rows_come_before_the_sites_own_sweep() -> None:
    cands = DetGenerator(diagonal=lambda ws: 100.0).propose(
        _state([_site("proximity", ["F5"], A11, EDGES, "low_quality")]), 999)
    # B19: a proximity site's PREFERRED families (near_faces, face_split, ...) come first, so "before the sweep" now
    # holds within the small-face family: its four aimed rows lead every other small-face row the site gets
    from meshpipeline.cad.repair_v4.search import _family_of

    small = [c for c in cands if _family_of(c.op) == "small"]
    assert len(_aimed(small[:4])) == 4, [(c.op, c.rationale) for c in small[:6]]
