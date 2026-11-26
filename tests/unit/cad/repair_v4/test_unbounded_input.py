"""D10: an UNBOUNDED input is classified on the ORIGINAL, cheaply, before the engine's form is taken.

Lane D measured abc/0060_00600434_7a2e012e32625e654d620ccc_step_001.step (sha bfc8d0b5, diagonal 3.5e100): on the
original STEP layer 1 reported `unbounded_geometry` in 4 s (out of scope, counted); on the engine's re-export the
detection no longer fired, check_valid ran to its 900 s timeout and the file hit the 1800 s file wall - an EXCLUSION.
The fix asks the kernel's `valid_extent` (a bounding box, the very rule layer 1 stops at) on the BREP the engine
reads; an unbounded input is judged on its original STEP, everything else exactly as before.
"""
from __future__ import annotations

from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import search
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

ORIGINAL = "/in/bfc8d0b5/0060_00600434_7a2e012e32625e654d620ccc_step_001.step"


class Pool:
    def __init__(self, unbounded: bool | None) -> None:
        self.unbounded, self.calls = unbounded, []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append(fn)
        if fn == "read_step_xcaf":
            return CallResult(ok=True, value={"brep": payload["brep_out"], "labels": {}, "units": "mm"}, error=None,
                              status="ok", seconds=0.1)
        if fn == "valid_extent":
            if self.unbounded is None:  # the kernel could not say: judge as before
                return CallResult(ok=False, value=None, error="boom", status="error", seconds=0.1)
            return CallResult(ok=True, value={"unbounded": self.unbounded}, error=None, status="ok", seconds=0.1)
        raise AssertionError(f"unexpected kernel call {fn}")


@pytest.fixture
def exports(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []

    def step_for(pool: Any, brep: str) -> str:
        made.append(brep)
        return "/tmp/engine_form/state_0.step"

    monkeypatch.setattr(search, "step_for", step_for)
    monkeypatch.setattr(search, "original_facts", lambda pool, brep: (3, 1))
    return made


def test_an_unbounded_input_is_judged_on_its_original_never_exported(exports: list[str]) -> None:
    pool = Pool(unbounded=True)
    assert search.engine_form(pool, ORIGINAL) == ORIGINAL
    assert exports == [] and pool.calls == ["read_step_xcaf", "valid_extent"]  # a bbox, never a full check


@pytest.mark.parametrize("unbounded", [False, None], ids=["bounded", "extent_call_failed"])
def test_a_bounded_input_takes_the_engine_form_exactly_as_before(exports: list[str], unbounded: bool | None) -> None:
    assert search.engine_form(Pool(unbounded=unbounded), ORIGINAL) == "/tmp/engine_form/state_0.step"
    assert len(exports) == 1


def _ws(history: list[Diff] | None = None) -> Workspace:
    return Workspace(brep_path="/w/input.brep", ids={}, labels={}, history=history or [], original="/w/input.brep")


def _judge(pool: Pool, ws: Workspace) -> str:
    seen: list[str] = []

    def fn(pool: Any, step: str, original: str) -> Any:
        seen.append(step)
        return type("V", (), {"valid": type("R", (), {"ok": False})()})()

    search._judged(fn, pool, ws, ORIGINAL)
    return seen[0]


def test_the_engine_and_arms_input_verdict_of_an_unbounded_file_is_the_originals(exports: list[str]) -> None:
    """repair_v4's entry and the eval arms judge the INPUT through `_judged` on the start workspace (no history)."""
    assert _judge(Pool(unbounded=True), _ws()) == ORIGINAL and exports == []
    assert _judge(Pool(unbounded=False), _ws()) == "/tmp/engine_form/state_0.step"


def test_a_repaired_state_is_never_swapped_for_the_input(exports: list[str]) -> None:
    """Only the input is classified on its original. A state an op produced is judged as the engine exports it when it
    is bounded; when it is still unbounded it is invalid by its extent alone and never checked (lane C9: on the
    re-export of an unbounded shape check_valid runs to its wall). Neither is ever the original."""
    edited = _ws(history=[Diff(op="sew", params={}, modified={}, generated={}, removed=[], declared_added=[],
                               declared_removed=[], seconds=0.0)])
    assert _judge(Pool(unbounded=False), edited) == "/tmp/engine_form/state_0.step"
    seen: list[str] = []
    v = search._judged(lambda pool, step, original: seen.append(step), Pool(unbounded=True), edited, ORIGINAL)
    assert seen == [] and [f.code for f in v.valid.hard] == ["unbounded_geometry"] and v.panel == []
