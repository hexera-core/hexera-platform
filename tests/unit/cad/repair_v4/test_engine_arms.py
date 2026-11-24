# ruff: noqa: F811 - fixtures imported from a sibling module are redefined as test arguments, by design
# Responsibility: Pin what the four arms do at the engine seam (`repair_v4()` with `ARM` set): `input_only` does nothing,
#                 `staged` runs the healers and nothing else, the search arms reach `_find_best`; the judge is sized from
#                 the part; `verdict_summary["search"]` carries every input of the shared `file_unmeasured` rule.
# Boundaries: hermetic; the fakes of test_engine_entry_exit (pool, judge, middle). The real kernel is the native tier.
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import meshpipeline.cad.repair_v4 as v4
from meshpipeline.cad.repair_v4 import search as search_mod
from meshpipeline.cad.repair_v4.measurement import file_unmeasured

from .test_engine_entry_exit import FakePool, FakeVerdict, World, _run, files, world  # noqa: F401  - shared fixtures


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def run_staged(pool: Any, ws: Any, **_kw: Any) -> Any:
        calls.append(ws)
        return SimpleNamespace(ws=dataclasses.replace(ws, brep_path="/w/staged.brep"))

    monkeypatch.setattr("meshpipeline.cad.repair_v4.stages.run_staged", run_staged)
    return calls


def test_input_only_touches_nothing_and_passes_the_upload_through(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "ARM", "input_only")
    monkeypatch.setattr(v4, "_find_best", lambda *a, **k: pytest.fail("input_only must not search"))
    result = _run(files)
    assert not staged, "input_only ran the staged healers"
    assert result.delivered is False
    assert Path(files[1]).read_bytes() == Path(files[0]).read_bytes()
    assert not [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"]
    s = result.verdict_summary
    assert s["status"] == "not_clean" and s["input"]["clean"] is False
    assert s["search"] == {"reason": "input_only", "changed": False, "clean": False, "verdict_candidates": 0,
                           "inconclusive_candidates": 0, "infrastructure_failed": False}
    assert result.file_unmeasured is False


def test_staged_runs_the_healers_only_and_delivers_through_the_gate(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "ARM", "staged")
    monkeypatch.setattr(v4, "_find_best", lambda *a, **k: pytest.fail("staged must not search"))
    seen: list[str] = []

    def full(pool: Any, ws: Any, original: str) -> FakeVerdict:
        seen.append(ws.brep_path)  # the judge sees the input, then the staged workspace
        return FakeVerdict(q=3 if ws.brep_path == "/w/staged.brep" else 1)

    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(full=full))
    result = _run(files)
    assert len(staged) == 1 and seen == ["/w/in.brep", "/w/staged.brep"]
    assert result.delivered is True and result.verdict_summary["search"]["reason"] == "staged"
    assert result.verdict_summary["search"]["changed"] is True
    (_, payload) = [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"][0]
    assert payload["brep"] == "/w/staged.brep"


def test_staged_that_is_worse_than_the_input_is_not_delivered(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "ARM", "staged")
    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(
        full=lambda p, w, o: FakeVerdict(q=0 if w.brep_path == "/w/staged.brep" else 1)))
    assert _run(files).delivered is False


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_the_search_arms_reach_the_middle(world: World, files: tuple[str, str], arm: str,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "ARM", arm)
    _run(files)
    assert world.middle_args["source"] == files[0]


def test_an_unknown_arm_is_a_value_error_before_a_pool_exists(
        world: World, files: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "ARM", "bogus")
    with pytest.raises(ValueError, match="unknown search arm 'bogus'"):
        _run(files)
    assert not FakePool.instances


def test_the_judge_is_sized_from_the_part_not_a_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_make_judge` calls `default_judge_for(pool, ws)`, whose `h_mm` is `1000 * base_size(diag_mm / 1000)`."""
    seen: list[float] = []
    monkeypatch.setattr(search_mod, "default_judge", lambda *, h_mm, cache=None: seen.append(h_mm) or "judge")
    for diag_mm in (17.32, 1.732e4):  # decades apart: a 1000x slip in either direction is visible
        pool = SimpleNamespace(call=lambda fn, payload, *, timeout_s, d=diag_mm: SimpleNamespace(
            ok=True, value={"diag_mm": d}, error=None, status="ok"))
        assert v4._make_judge(pool, SimpleNamespace(brep_path="/w/x.brep")) == "judge"
    from meshpipeline.engines.fea_plan import base_size

    assert seen == pytest.approx([1000 * base_size(0.01732), 1000 * base_size(17.32)])
    assert seen[1] / seen[0] == pytest.approx(1000.0, rel=0.05)
    assert not hasattr(v4, "H_MM")


def test_the_search_summary_carries_every_input_of_the_shared_rule(world: World, files: tuple[str, str]) -> None:
    result = _run(files)
    s = result.verdict_summary["search"]
    assert set(s) >= {"reason", "changed", "clean", "verdict_candidates", "inconclusive_candidates",
                      "infrastructure_failed"}
    assert result.file_unmeasured == file_unmeasured(
        reason=s["reason"], infrastructure_failed=s["infrastructure_failed"],
        inconclusive_candidates=s["inconclusive_candidates"], verdict_candidates=s["verdict_candidates"],
        out_clean=s["clean"])


def test_a_summary_with_no_search_block_was_not_measured() -> None:
    assert v4.V4Result(trace_path=None, verdict_summary={"status": "not_measured"}).file_unmeasured is True


def test_the_rule_is_applied_not_restated() -> None:
    """A search that judged nothing and met an inconclusive candidate is unmeasured, by `measurement`'s rule."""
    bad = {"reason": "exhausted", "changed": False, "clean": False, "verdict_candidates": 0,
           "inconclusive_candidates": 2, "infrastructure_failed": False}
    assert v4.V4Result(trace_path=None, verdict_summary={"search": bad}).file_unmeasured is True
    assert v4.V4Result(trace_path=None, verdict_summary={"search": {**bad, "verdict_candidates": 1}}
                       ).file_unmeasured is False
    assert v4.V4Result(trace_path=None, verdict_summary={"search": {**bad, "reason": "no_verdict"}}
                       ).file_unmeasured is True


#: captured at import, before the `world` fixture replaces it with a fake: the test below needs the REAL middle
_REAL_FIND_BEST = v4._find_best


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_a_search_arm_delivers_the_staged_healers_gain_when_the_search_adds_nothing(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    monkeypatch.setattr(v4, "ARM", arm)
    monkeypatch.setattr(v4, "_find_best", _REAL_FIND_BEST)
    monkeypatch.setattr(v4, "_ai_kwargs", lambda source, budget: {})
    monkeypatch.setattr(search_mod, "arm_generators", lambda name, **kw: ([], None))
    healed = FakeVerdict(q=3, clean=True)
    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(
        full=lambda p, w, o: healed if w.brep_path == "/w/staged.brep" else FakeVerdict(q=1)))
    monkeypatch.setattr(search_mod, "score", lambda v: (1, v.q, 0.0, 0.0, 0))

    def search(pool: Any, start: Any, original: str, generators: Any, budget: Any, **kw: Any) -> Any:
        assert start.brep_path == "/w/staged.brep"  # the search starts from the healer's output (contract 04)
        return search_mod.SearchResult(best=start, best_verdict=healed, clean=True, trace=None, changed=False,
                                       reason="clean")  # already clean: nothing beat the start

    monkeypatch.setattr(search_mod, "search", search)
    result = _run(files)
    assert result.delivered is True, "the staged healer made the file clean and the search arm shipped the input"
    (_, payload) = [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"][0]
    assert payload["brep"] == "/w/staged.brep"


def _arm_world(monkeypatch: pytest.MonkeyPatch, arm: str, *, input_v: Any, staged_v: Any, search_to: str | None,
               search_v: Any = None) -> None:
    """The real `_find_best`. `search_to=None`: the search adds nothing (best is its start, changed=False);
    otherwise it returns a new state `search_to` with verdict `search_v`, changed=True."""
    monkeypatch.setattr(v4, "ARM", arm)
    monkeypatch.setattr(v4, "_find_best", _REAL_FIND_BEST)
    monkeypatch.setattr(v4, "_ai_kwargs", lambda source, budget: {})
    monkeypatch.setattr(search_mod, "arm_generators", lambda name, **kw: ([], None))
    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(
        full=lambda p, w, o: staged_v if w.brep_path == "/w/staged.brep" else input_v))
    monkeypatch.setattr(search_mod, "score", lambda v: (1, v.q, 0.0, 0.0, 0))

    def search(pool: Any, start: Any, original: str, generators: Any, budget: Any, **kw: Any) -> Any:
        if search_to is None:
            return search_mod.SearchResult(best=start, best_verdict=staged_v, clean=bool(staged_v.clean),
                                           trace=None, changed=False, reason="patience")
        return search_mod.SearchResult(best=dataclasses.replace(start, brep_path=search_to), best_verdict=search_v,
                                       clean=bool(search_v.clean), trace=None, changed=True, reason="clean")

    monkeypatch.setattr(search_mod, "search", search)


def _written(path: str) -> bool:
    return [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"][0][1]["brep"] == path


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_a_state_worse_than_the_input_is_still_refused_after_a_healer_only_change(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    _arm_world(monkeypatch, arm, input_v=FakeVerdict(q=2), staged_v=FakeVerdict(q=0), search_to=None)
    result = _run(files)
    assert result.delivered is False
    assert not [c for c in FakePool.instances[0].calls if c[0] == "write_step_xcaf"]


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_a_clean_input_is_never_replaced_by_a_search_arm(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    _arm_world(monkeypatch, arm, input_v=FakeVerdict(q=3, clean=True), staged_v=FakeVerdict(q=3, clean=True),
               search_to=None)
    assert _run(files).delivered is False


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_a_search_that_improves_on_the_healer_is_delivered(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    _arm_world(monkeypatch, arm, input_v=FakeVerdict(q=1), staged_v=FakeVerdict(q=2), search_to="/w/found.brep",
               search_v=FakeVerdict(q=3, clean=True))
    assert _run(files).delivered is True and _written("/w/found.brep")


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_nothing_changed_at_all_delivers_nothing(
        world: World, files: tuple[str, str], monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    """The healer changed nothing (best is the input workspace) and the search added nothing: passthrough."""
    monkeypatch.setattr(v4, "ARM", arm)
    monkeypatch.setattr(v4, "_find_best", _REAL_FIND_BEST)
    monkeypatch.setattr(v4, "_ai_kwargs", lambda source, budget: {})
    monkeypatch.setattr(search_mod, "arm_generators", lambda name, **kw: ([], None))
    monkeypatch.setattr("meshpipeline.cad.repair_v4.stages.run_staged", lambda pool, ws, **_kw: SimpleNamespace(ws=ws))
    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(full=lambda p, w, o: FakeVerdict(q=1)))
    monkeypatch.setattr(search_mod, "score", lambda v: (1, v.q, 0.0, 0.0, 0))
    monkeypatch.setattr(search_mod, "search", lambda pool, start, *a, **k: search_mod.SearchResult(
        best=start, best_verdict=FakeVerdict(q=1), clean=False, trace=None, changed=False, reason="patience"))
    assert _run(files).delivered is False


def _panel(passes: int, of: int = 4) -> tuple:
    return tuple(SimpleNamespace(name=f"m{i}", status="pass" if i < passes else "fail") for i in range(of))


@pytest.mark.parametrize("arm", ["search_det", "search_ai"])
def test_a_search_arm_never_starts_from_a_staged_state_the_full_panel_says_is_worse(
        world: World, files: tuple[str, str], staged: list[Any], monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    """Task 49: the healer's `condition` stage took a CLEAN file from 4 panel passes to 3 and the cheap tier, which
    meshes only gmsh_default, kept it. The engine's search now starts from the state the FULL panel confirmed - here
    the input, because the staged state lost a member."""
    clean_in = FakeVerdict(q=4, clean=True, panel=_panel(4))
    damaged = FakeVerdict(q=3, clean=False, panel=_panel(3))
    monkeypatch.setattr(v4, "ARM", arm)
    monkeypatch.setattr(v4, "_find_best", _REAL_FIND_BEST)
    monkeypatch.setattr(v4, "_ai_kwargs", lambda source, budget: {})
    monkeypatch.setattr(search_mod, "arm_generators", lambda name, **kw: ([], None))
    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(
        full=lambda p, w, o: damaged if w.brep_path == "/w/staged.brep" else clean_in))
    starts: list[str] = []

    def search(pool: Any, start: Any, original: str, generators: Any, budget: Any, **kw: Any) -> Any:
        starts.append(start.brep_path)
        return search_mod.SearchResult(best=start, best_verdict=clean_in, clean=True, trace=None, changed=False,
                                       reason="clean")

    monkeypatch.setattr(search_mod, "search", search)
    result = _run(files)
    assert starts == ["/w/in.brep"], "the search started from the state the healer damaged"
    assert result.delivered is False                       # a clean input is passed through, never replaced
