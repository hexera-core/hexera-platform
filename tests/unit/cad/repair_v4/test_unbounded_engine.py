# Responsibility: Lane C9 - an unbounded input reaches the search and its trim. Measured on sha bfc8d0b5 (C8): the engine
#                 spent 1808 s in the staged healer's baseline (its `_judge` judges a re-export, whose check_valid runs
#                 to its wall on an unbounded shape) and raised SearchUnavailable before anything was searched. Pins:
#                 `_find_best` skips the staged healer for an unbounded input; `_judged` reports a still-unbounded STATE
#                 invalid by its extent alone, never by running the checker; the det sweep puts trim_unbounded first.
# Boundaries: hermetic; fakes for the pool and the judge. The real kernel is tests/native/test_native_repair_v4_c9_unbounded.py.
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import meshpipeline.cad.repair_v4 as v4
from meshpipeline.cad.repair_v4 import search as S
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.verdict.assemble import cheap_verdict, score, verdict
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

from .test_engine_entry_exit import FakePool, FakeVerdict, World, _run, files, world  # noqa: F401  - shared fixtures

FINDING = {"code": "unbounded_geometry", "entities": [], "detail": {"diagonal_mm": 2e100, "open_sides": ["xmax"]}}


class _ExtentPool:
    """Answers only `valid_extent`, and counts the calls."""

    def __init__(self, unbounded: bool) -> None:
        self.unbounded, self.calls = unbounded, []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append(fn)
        assert fn == "valid_extent", f"unexpected kernel call {fn}"
        return CallResult(True, {"unbounded": self.unbounded, "finding": FINDING if self.unbounded else None}, None,
                          "ok", 0.0)


def _ws(history: bool) -> Workspace:
    diff = Diff(op="split_angle", params={}, modified={}, generated={}, removed=[], declared_added=[],
                declared_removed=[], seconds=0.0)
    return Workspace(brep_path="/fake/s.brep", ids={"F1": "f", "B1": "b", "B2": "c"}, labels={},
                     history=[diff] if history else [], original="/fake/o.brep", indices={"F1": 1, "B1": 1, "B2": 2})


@pytest.mark.parametrize("fn, tier", [(cheap_verdict, "cheap"), (verdict, "full")])
def test_a_state_still_unbounded_is_invalid_by_its_extent_alone(monkeypatch: pytest.MonkeyPatch, fn: Any,
                                                                  tier: str) -> None:
    monkeypatch.setattr(S, "original_facts", lambda pool, original: ([], 2))
    monkeypatch.setattr(S, "step_for", lambda *a, **k: pytest.fail(
        "an unbounded state must not be exported or checked: that is the 1808 s wall"))
    pool = _ExtentPool(True)  # any kernel call but valid_extent fails the test, so the checker cannot have run
    v = S._judged(fn, pool, _ws(history=True), "/fake/in.step")
    assert pool.calls == ["valid_extent"]  # one bounding-box call, nothing else
    assert v.tier == tier and not v.clean and v.panel == [] and v.out_of_scope
    assert [f.code for f in v.valid.hard] == ["unbounded_geometry"] and v.valid.hard[0].detail["open_sides"] == ["xmax"]
    assert v.valid.solids == 2 and score(v)[0] == 0  # ranks like the unbounded input, never above a bounded state


def test_the_unbounded_input_with_no_history_keeps_d10s_original_step_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "original_facts", lambda pool, original: ([], 1))
    seen: list[str] = []

    def fn(pool: Any, step: str, original: str, **kw: Any) -> Any:
        seen.append(step)
        return SimpleNamespace(valid=SimpleNamespace(ok=False))

    S._judged(fn, _ExtentPool(True), _ws(history=False), "/fake/in.step")
    assert seen == ["/fake/in.step"]


# --- the engine skips the staged healer for an unbounded input ------------------------------------------------------


def test_an_unbounded_input_starts_the_search_from_the_input_not_the_staged_healer(
        world: World, files: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(v4, "ARM", "search_det")
    monkeypatch.setattr(v4, "_find_best", _REAL_FIND_BEST)
    world.input_verdict = _OutOfScope(q=0)  # the input judged on its original: layer 1 says unbounded_geometry
    monkeypatch.setattr("meshpipeline.cad.repair_v4.stages.run_staged",
                        lambda *a, **k: pytest.fail("the staged healer re-exports: on an unbounded input it hangs"))
    started: list[str] = []

    def search(pool: Any, start: Any, original: str, generators: Any, budget: Any, **kw: Any) -> Any:
        started.append(start.brep_path)
        return S.SearchResult(best=start, best_verdict=FakeVerdict(q=1), clean=False, trace=None, changed=False,
                              reason="exhausted")

    monkeypatch.setattr(S, "search", search)
    _run(files)
    assert started == ["/w/in.brep"]


_REAL_FIND_BEST = v4._find_best


class _OutOfScope(FakeVerdict):
    out_of_scope = True


# --- trim_unbounded first ---------------------------------------------------------------------------------------


def _site(kind: str, entities: list[str]) -> Site:
    return Site(id=f"S@{kind}", kind=kind, entities=entities, xyz=[], meshers_failing=[], dist_over_h=None,
                graph={}, view_png=None)  # type: ignore[arg-type]


def test_trim_unbounded_is_proposed_first_and_survives_the_k_cut() -> None:
    ws = dataclasses.replace(_ws(history=False), ids={"F1": "f", "F2": "g", "B1": "b", "B2": "c"},
                             indices={"F1": 1, "F2": 2, "B1": 1, "B2": 2})
    state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), parent=None, depth=0, via=None,
                        sites=[_site("overlap", ["B1", "B2", "F1", "F2"]), _site("unbounded", [])])
    got = DetGenerator(diagonal=lambda w: 100.0).propose(state, 2)
    assert [c.op for c in got] == ["trim_unbounded", "trim_unbounded"]
