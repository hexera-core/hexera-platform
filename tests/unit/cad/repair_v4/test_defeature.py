"""D14: the defeature op and Raymond Lee's recogniser as a candidate source - the parts that need no kernel.

The kernel half (recognition on real shapes, one defeaturing call, the one-at-a-time fallback, the faces-gone count,
the T0 report, the slot-end no-op) is in tests/native/test_native_repair_v4_defeature.py.
"""
from __future__ import annotations

from typing import Any

import pytest

from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined
from meshpipeline.cad.repair_v4.ops.defeature import Defeature, defeature_rows
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Workspace


def _feature(fid: str, kind: str, faces: list[str], cells: int = 10) -> dict:
    return {"id": fid, "kind": kind, "face_ids": faces, "removable": kind != "slot_end", "est_surface_cells": cells}


FEATURES = [
    _feature("hole0", "hole", ["F3", "F4"], cells=50),
    _feature("blend1", "blend", ["F7"], cells=80),
    _feature("slot_end2", "slot_end", ["F9", "F10"], cells=500),     # the costliest: still never proposed
]


def test_slot_ends_are_never_proposed_and_each_feature_is_one_group() -> None:
    [(op, targets, params, why)] = defeature_rows(FEATURES)
    assert op == "defeature" and params["source"] == "recognised"
    assert "F9" not in targets and "F10" not in targets
    assert targets == ["F7", "F3", "F4"]                    # costliest feature first (Raymond's order)
    assert params["groups"] == [[0], [1, 2]]                # blend1, then hole0's two faces
    assert "1 holes, 1 blends" in why


def test_at_a_site_only_the_features_touching_its_faces() -> None:
    [(_, targets, params, _)] = defeature_rows(FEATURES, site_faces=["F4", "F99"])
    assert targets == ["F3", "F4"] and params["groups"] == [[0, 1]]
    assert defeature_rows(FEATURES, site_faces=["F9"]) == []   # only a slot end there: nothing to propose
    assert defeature_rows([], site_faces=["F1"]) == []


def _site(sid: str, kind: str, entities: list[str], failing: list[str], coarse: bool = False) -> Site:
    return Site(id=sid, kind=kind, entities=entities, xyz=[], meshers_failing=failing, dist_over_h=None,  # type: ignore[arg-type]
                graph={"coarse": {"reason": "x", "members": failing}} if coarse else {}, view_png=None)


def _state(sites: list[Site]) -> SearchState:
    ws = Workspace(brep_path="/w/s.brep", ids={f"F{i}": f"fp{i}" for i in range(1, 12)}, labels={}, history=[],
                   original="/w/s.brep")
    st = SearchState.__new__(SearchState)
    st.ws, st.sites, st.lineage, st.depth = ws, sites, (), 0  # type: ignore[attr-defined]
    return st


def _gen(features: list[dict]) -> tuple[DetGenerator, list[int]]:
    asked: list[int] = []
    g = DetGenerator(diagonal=lambda ws: 100.0)

    def recognise(ws: Workspace) -> list[dict]:
        asked.append(1)
        return features
    g.features = recognise
    return g, asked


def test_the_det_sweep_proposes_the_recognised_set_once_and_site_rows_where_they_suit() -> None:
    assert "defeature" in REGISTRY
    g, asked = _gen(FEATURES)
    sites = [_site("S1", "sliver", ["F7", "F8"], ["gmsh_alt"]), _site("S2", "proximity", ["F3", "F5"], ["gmsh_prod"])]
    cands = [c for c in g.propose(_state(sites), 999) if c.op == "defeature"]
    assert len(asked) == 1, "the recogniser was asked more than once for one state"
    whole = [c for c in cands if c.params.get("source") == "recognised" and "on the part" in c.rationale]
    assert len(whole) == 1 and set(whole[0].targets) == {"F3", "F4", "F7"}
    own = [c for c in cands if c.params.get("source") == "site"]
    assert sorted(tuple(c.targets) for c in own) == [("F3", "F5"), ("F7", "F8")]
    assert all("F9" not in c.targets for c in cands)          # never a slot end, from any row


def test_no_defeature_at_open_or_coarse_or_passing_sites() -> None:
    g, asked = _gen(FEATURES)
    sites = [_site("S1", "open", ["F3"], ["gmsh_prod"]),           # faces already missing: removing more is no repair
             _site("S2", "proximity", ["F4"], ["gmsh_prod"], coarse=True),
             _site("S3", "proximity", ["F5"], [])]                 # nothing fails there
    cands = [c for c in g.propose(_state(sites), 999) if c.op == "defeature"]
    assert all(c.params.get("source") != "site" for c in cands)
    assert all("at the site" not in c.rationale for c in cands)


def test_groups_must_index_the_targets_each_once() -> None:
    ws = Workspace(brep_path="/w/s.brep", ids={"F1": "a", "F2": "b"}, labels={}, history=[], original="/w/s.brep")
    with pytest.raises(OpDeclined, match="invalid_params"):
        Defeature().apply(Any, ws, ["F1", "F2"], {"groups": [[0, 2]]}, timeout_s=1)  # type: ignore[arg-type]
    with pytest.raises(OpDeclined, match="invalid_params"):
        Defeature().apply(Any, ws, ["F1", "F2"], {"groups": [[0], [0]]}, timeout_s=1)  # type: ignore[arg-type]


def test_defeature_comes_after_every_sites_own_rows_so_k_cuts_it_first() -> None:
    """D16 (lane B13, snapshot v2): on the one body_overlap dev file seven defeature applications ran first and the
    body ops were `skipped_budget`. Every site's own rows now come before any defeature row, the site-level removals
    before the whole recognised set, so the cap and the op budget cut defeature first."""
    g, _ = _gen(FEATURES)
    sites = [_site("S1", "sliver", ["F7", "F8"], ["gmsh_alt"]), _site("S2", "proximity", ["F3", "F5"], ["gmsh_prod"])]
    cands = g.propose(_state(sites), 999)
    ops = [c.op for c in cands]
    assert "defeature" in ops and any(op != "defeature" for op in ops)
    first = ops.index("defeature")
    assert all(op == "defeature" for op in ops[first:]), ops       # nothing site-specific after a defeature row
    assert any("(site S2," in c.rationale for c in cands[:first])  # S2's own rows are not behind S1's defeature
    tail = [c.params.get("source") for c in cands[first:]]
    assert tail[-1] == "recognised" and "on the part" in cands[-1].rationale   # the whole set is last
    g2, _ = _gen(FEATURES)
    assert all(c.op != "defeature" for c in g2.propose(_state(sites), first))   # k at the own rows: no defeature


def _located(sid: str, kind: str, entities: list[str], failing: list[str], named: list[str]) -> Site:
    s = _site(sid, kind, entities, failing)
    s.graph = {"mesher_faces": named}
    return s


def test_a_mesher_located_failure_gets_a_removal_of_exactly_the_faces_it_named_at_any_kind() -> None:
    """D16: gmsh's "other" failure is an `invalid` site, a kind the D14 rule left out; the faces it named are now
    proposed for removal, alone, and nothing else from the D14 rule fires there."""
    g, _ = _gen(FEATURES)
    cands = [c for c in g.propose(_state([_located("S1", "invalid", ["F7", "F8"], ["gmsh_prod"], ["F7"])]), 999)
             if c.op == "defeature" and c.params.get("source") != "recognised"]
    assert [(c.targets, c.params["source"]) for c in cands] == [(["F7"], "located")]
    assert "gmsh_prod failed on" in cands[0].rationale


def test_the_located_removal_comes_before_the_recognised_and_site_rows_and_the_whole_part() -> None:
    g, _ = _gen(FEATURES)
    cands = [c for c in g.propose(_state([_located("S1", "proximity", ["F3", "F5"], ["gmsh_prod"], ["F3"])]), 999)
             if c.op == "defeature"]
    assert [(c.params["source"], c.targets) for c in cands] == [
        ("located", ["F3"]),                 # what the mesher named
        ("recognised", ["F3", "F4"]),        # the hole touching it, whole
        ("site", ["F3", "F5"]),              # the site's faces, with the proximity context
        ("recognised", ["F7", "F3", "F4"]),  # the whole recognised set, last
    ]


def test_no_located_removal_without_a_mesher_or_at_an_open_site() -> None:
    g, _ = _gen(FEATURES)
    valid_only = _located("S1", "self_overlap", ["F3"], ["valid"], ["F3"])   # layer 1 is not a mesher
    open_site = _located("S2", "open", ["F7"], ["gmsh_prod"], ["F7"])        # faces already missing
    cands = g.propose(_state([valid_only, open_site]), 999)
    assert not [c for c in cands if c.op == "defeature" and c.params.get("source") in ("located", "site")]
