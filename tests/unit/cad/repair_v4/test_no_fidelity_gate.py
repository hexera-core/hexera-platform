# Responsibility: Pin the owner decision of 2026-11-24 (lane B12): CLEAN is VALID + MESHABLE. Faithfulness is measured
#                 and reported, never a gate: a large-deviation edit that meshes in more members WINS the search and is
#                 delivered; no op schema caps a parameter by the fidelity bound; fuse is allowed; no sweep refuses on
#                 fidelity.
# Boundaries: hermetic (fake world, real search, real score, real op schemas). The kernels are the native tier.
from __future__ import annotations

from typing import Any

import pytest
from tests.unit.cad.repair_v4 import test_search_wiring as W

from meshpipeline.cad.repair_v4 import search
from meshpipeline.cad.repair_v4.ops import REGISTRY, validate_call
from meshpipeline.cad.repair_v4.verdict.assemble import Verdict, score
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport
from meshpipeline.cad.repair_v4.workspace import Workspace


class MovedWorld(W.World):
    """A state's quality is its `q`; every state with q >= 3 moved the surface by 25 mm (layer 3: unfaithful)."""

    def _v(self, ws: Workspace, tier: str) -> Verdict:
        q = W._q(ws)
        moved = q >= 3
        faithful = FaithfulReport(not moved, 25.0 if moved else 0.0, 0.1, 1.0, 1.0, 1, 1,
                                  reasons=["deviation_exceeds_bound", "area_changed"] if moved else [])
        return Verdict(ValidReport(True, [], [], "mm", 1, True), [W.Member(f"m{i}") for i in range(q)], faithful,
                       clean=False, tier=tier)  # type: ignore[arg-type]

    def cheap(self, pool: Any, ws: Workspace, original: str) -> Verdict:
        return self._v(ws, "cheap")

    def full(self, pool: Any, ws: Workspace, original: str) -> Verdict:
        return self._v(ws, "full")


def test_a_large_deviation_edit_that_meshes_more_wins_the_search_and_is_delivered(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "t18_set", W.T18Op())  # type: ignore[arg-type]
    monkeypatch.setitem(REGISTRY, "orient_and_close", W.FinaliseHelper())  # type: ignore[arg-type]
    gen = W.Scripted(lambda st: [W.cand(1), W.cand(3)] if W._q(st.ws) == 0 else [])
    world = MovedWorld()
    start = W.make_ws(0)
    res = W.run([gen], world, start=start, patience=2)
    assert W._q(res.best) == 3, "the 25 mm edit that meshes in 3 members must out-rank the faithful one in 1"
    assert res.best_verdict.faithful.max_dev_mm == 25.0 and not res.best_verdict.faithful.ok
    input_v = world.full(None, start, "o")
    assert search.deliverable(res, input_v) is res.best              # and the delivery gate ships it


def test_score_key_zero_is_validity_alone() -> None:
    moved = Verdict(ValidReport(True, [], [], "mm", 1, True), [W.Member("m0")],
                    FaithfulReport(False, 999.0, 0.1, 0.5, 0.5, 1, 3, reasons=["body_count_changed"]),
                    clean=False, tier="full")  # type: ignore[arg-type]
    assert score(moved)[0] == 1


@pytest.mark.parametrize("op,targets,params", [
    ("reapproximate", ["F1"], {"tol_mm": 5.0, "max_degree": 6}),
    ("to_canonical", ["F1"], {"tol_mm": 2.5}),
    ("refit_cluster", ["F1", "F2"], {"tol_mm": 10.0}),
    ("remove_small", ["F1"], {"max_width_mm": 25.0}),
    ("sew", [], {"tol_ladder": [0.5, 2.0, 10.0]}),
    ("conformalise", ["B1", "B2"], {"mode": "fragment", "fuzzy_mm": 3.0}),
    ("fuse", ["B1", "B2"], {"fuzzy_mm": 3.0}),
    ("conformalise", ["B1", "B2"], {"mode": "cut_by:B1"}),
])
def test_no_op_schema_caps_a_parameter_by_the_fidelity_bound(op: str, targets: list, params: dict) -> None:
    """Every fidelity-tied maximum (0.1 mm, the bound) is gone from these schemas (B12)."""
    ws = Workspace(brep_path="/x.brep", ids={"F1": "a", "F2": "b", "B1": "c", "B2": "d", "E1": "e"}, labels={},
                   history=[], original="/x.brep", indices={"F1": 1, "F2": 2, "B1": 1, "B2": 2, "E1": 1})
    assert op in REGISTRY
    assert validate_call(op, targets, params, ws) == []


def test_the_det_sweep_proposes_fuse_as_well_as_cut_by_on_an_overlap() -> None:
    overlap = W.site("S1", "overlap", ["B1", "B2"])
    st = W.make_ws(0, ids=("B1", "B2"))
    got = search.DetGenerator(diagonal=lambda ws: 100.0).propose(
        search.SearchState("S0", st, None, (0, 0, 0.0, 0.0, 0), [overlap], 0, None, None), 50)
    modes = [c.params.get("mode") for c in got if c.op == "conformalise"]
    assert any(c.op == "fuse" for c in got) and any(str(m).startswith("cut_by:") for m in modes)
