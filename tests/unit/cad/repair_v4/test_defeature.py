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
