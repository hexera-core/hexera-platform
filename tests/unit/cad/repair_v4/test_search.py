# Responsibility: Verify the v4 best-first search driver, DetGenerator, budgets, trace and the
#                 delivery rule against a FAKE world (no kernel, no pool, no real verdict).
# Boundaries: the fake world is one integer, the "quality" q stored on a Workspace; a fake op sets
#             it, a fake Judge scores it. Real pieces used: Workspace, REGISTRY, validate_call,
#             OpDeclined/OpFailed. Tasks 5 (verdict) and 17 (sites) are faked through `Judge`.
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any

import pytest

from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed
from meshpipeline.cad.repair_v4.search import (
    Budget,
    Candidate,
    DetGenerator,
    Judge,
    Rejection,
    SearchState,
    deliverable,
    search,
)
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

_counter = itertools.count(1)


@dataclass
class FakeVerdict:
    q: int
    clean: bool
    tier: str


def _ws(q: int, *, ids: tuple[str, ...] = ("F1", "F2"), history: list[Diff] | None = None) -> Workspace:
    n = next(_counter)
    return Workspace(
        brep_path=f"/fake/{n}.brep",
        ids={i: f"fp{n}{i}" for i in ids},
        labels={"_q": {"q": q}},
        history=history or [],
        original="/fake/orig.brep",
    )


def _q(ws: Workspace) -> int:
    return int(ws.labels["_q"]["q"])


class SetOp:
    """Fake op: a new workspace whose quality is params['q']; params['drop'] removes F1."""

    name = "t18_set"
    params_schema = {
        "type": "object",
        "properties": {"q": {"type": "integer", "minimum": 0, "maximum": 100}, "drop": {"type": "boolean"}},
        "required": ["q"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 0},
    }
    applied: list[dict] = []
    behaviour: Any = None  # a callable(params) that may raise OpDeclined / OpFailed

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(
        self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float
    ) -> tuple[Workspace, Diff]:
        SetOp.applied.append(dict(params))
        if SetOp.behaviour is not None:
            SetOp.behaviour(params)
        drop = bool(params.get("drop"))
        ids = tuple(i for i in ws.ids if not (drop and i == "F1"))
        diff = Diff(
            op=self.name,
            params=params,
            modified={},
            generated={},
            removed=["F1"] if drop else [],
            declared_added=[],
            declared_removed=[],
            seconds=0.0,
        )
        new = _ws(params["q"], ids=ids, history=[*ws.history, diff])
        return new, diff


@pytest.fixture(autouse=True)
def fake_op(monkeypatch: pytest.MonkeyPatch) -> None:
    SetOp.applied = []
    SetOp.behaviour = None
    monkeypatch.setitem(REGISTRY, "t18_set", SetOp())


class FakeJudge:
    """Clean at q >= target. Score = (1, q, 0, 0, 0): `q` is the panel passes."""

    def __init__(self, target: int = 99) -> None:
        self.target = target
        self.cheap_calls: list[int] = []
        self.full_calls: list[int] = []

    def judge(self) -> Judge:
        return Judge(cheap=self._cheap, full=self._full, score=self._score, sites=self._sites)

    def _cheap(self, pool: Any, ws: Workspace, original: str) -> FakeVerdict:
        self.cheap_calls.append(_q(ws))
        return FakeVerdict(_q(ws), _q(ws) >= self.target, "cheap")

    def _full(self, pool: Any, ws: Workspace, original: str) -> FakeVerdict:
        self.full_calls.append(_q(ws))
        return FakeVerdict(_q(ws), _q(ws) >= self.target, "full")

    @staticmethod
    def _score(v: FakeVerdict) -> tuple:
        return (1, v.q, 0.0, 0.0, 0)

    @staticmethod
    def _sites(pool: Any, ws: Workspace, verdict: FakeVerdict) -> list[Site]:
        return [
            Site(
                id="S1",
                kind="proximity",
                entities=["F1", "F2"],
                xyz=[],
                meshers_failing=[],
                dist_over_h=None,
                graph={},
                view_png=None,
            )
        ]


class Scripted:
    """A generator that returns `script(state)`, remembering what it was told was rejected."""

    def __init__(self, script: Any, source: str = "det") -> None:
        self.script = script
        self.source = source
        self.states: list[SearchState] = []
        self.rejections: list[Rejection] = []
        self.calls = 0
        self.model_calls = 0
        self.model_usd = 0.0

    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        self.calls += 1
        self.states.append(state)
        return list(self.script(state))[:k]

    def on_rejected(self, rejections: list[Rejection]) -> None:
        self.rejections.extend(rejections)

    def model_usage(self) -> tuple[int, float]:
        return self.model_calls, self.model_usd


def cand(q: int, **extra: Any) -> Candidate:
    return Candidate("t18_set", [], {"q": q, **extra}, "det", rationale=f"set q={q}")


BIG = Budget(wall_s=1000, ops=1000, model_calls=1000, model_usd=1000)


def run(gens: list[Any], fj: FakeJudge, budget: Budget = BIG, start_q: int = 0, **kw: Any):
    start = _ws(start_q)
    return search(None, start, start.original, gens, budget, workers=2, judge=fj.judge(), **kw), start


def stop_reason(result) -> str:
    return result.trace.of_kind("stop")[0]["reason"]


# --- tests --------------------------------------------------------------------------------------


def test_stops_when_clean() -> None:
    fj = FakeJudge(target=3)
    gen = Scripted(lambda s: [cand(1), cand(3), cand(2)])
    result, start = run([gen], fj)
    assert result.clean and _q(result.best) == 3
    assert result.best_verdict.tier == "full"
    assert stop_reason(result) == "clean"
    assert gen.calls == 1  # no second expansion once clean


def test_start_already_clean_is_returned_without_expanding() -> None:
    fj = FakeJudge(target=0)
    gen = Scripted(lambda s: [cand(5)])
    result, start = run([gen], fj)
    assert result.clean and result.best is start and gen.calls == 0
    assert SetOp.applied == []


def test_respects_wall_and_op_budget() -> None:
    # ops: three op applications, never more, even though 10 are proposed
    fj = FakeJudge()
    gen = Scripted(lambda s: [cand(1 + i) for i in range(10)])
    result, _ = run([gen], fj, Budget(wall_s=1000, ops=3, model_calls=99, model_usd=99))
    assert len(SetOp.applied) == 3 and stop_reason(result) == "ops"
    assert result.trace.of_kind("stop")[0]["spent"]["ops"] == 3
    assert any(e["outcome"] == "skipped_budget" for e in result.trace.of_kind("candidate"))

    # wall: a fake clock that advances 1 s per reading stops the search on time
    SetOp.applied = []
    ticks = itertools.count()
    fj2 = FakeJudge()
    gen2 = Scripted(lambda s: [cand(1)])
    start = _ws(0)
    result2 = search(
        None,
        start,
        start.original,
        [gen2],
        Budget(wall_s=5, ops=1000, model_calls=99, model_usd=99),
        workers=1,
        judge=fj2.judge(),
        clock=lambda: float(next(ticks)),
    )
    assert stop_reason(result2) == "wall"
    assert gen2.calls < 10


def test_model_call_and_usd_budgets_each_stop_the_search() -> None:
    def ai(s: SearchState) -> list[Candidate]:
        gen.model_calls += 1
        gen.model_usd += 0.5
        return [cand(s.depth + 1)]

    for budget, reason, calls in (
        (Budget(wall_s=1000, ops=1000, model_calls=2, model_usd=100), "model_calls", 2),
        (Budget(wall_s=1000, ops=1000, model_calls=100, model_usd=1.0), "model_usd", 2),
    ):
        gen = Scripted(ai, source="ai")
        result, _ = run([gen], FakeJudge(), budget)
        assert stop_reason(result) == reason
        assert gen.calls == calls


def test_confirms_top_k_with_full_panel() -> None:
    fj = FakeJudge()
    gen = Scripted(lambda s: [cand(q) for q in (4, 9, 2, 7, 5)] if s.depth == 0 else [])
    result, start = run([gen], fj, k_confirm=2)
    # the full panel saw the start and exactly the two best cheap scorers, nothing else
    assert sorted(fj.full_calls) == [0, 7, 9]
    assert sorted(fj.cheap_calls) == [2, 4, 5, 7, 9]
    assert [e["tier"] for e in result.trace.of_kind("confirm")] == ["full", "full"]
    assert _q(result.best) == 9 and result.best_verdict.tier == "full"


def test_patience_stops_flat_search() -> None:
    fj = FakeJudge()
    # every child is worse than the start; the search keeps expanding but must stop after 3
    gen = Scripted(
        lambda s: [
            cand(
                0,
            )
            for _ in range(1)
        ],
        source="det",
    )
    result, start = run([gen], fj, start_q=5, patience=3)
    assert stop_reason(result) == "patience"
    assert gen.calls == 3
    assert result.best is start and not result.clean


def test_invalid_candidate_returns_error_to_source_not_crash() -> None:
    """Review Focus 5: a target a previous op removed comes back as a structured error."""
    fj = FakeJudge(target=2)

    def script(s: SearchState) -> list[Candidate]:
        if s.depth == 0:
            return [cand(1, drop=True)]  # removes F1
        return [
            Candidate("t18_set", ["F1"], {"q": 5}, "ai", "touch the removed face"),
            Candidate("t18_set", [], {"q": 999}, "ai", "out of bounds"),
            Candidate("nope", [], {}, "ai", "unknown op"),
            cand(2),
        ]

    gen = Scripted(script, source="ai")
    result, _ = run([gen], fj)
    assert result.clean and _q(result.best) == 2  # the search kept going after the errors
    assert len(gen.rejections) == 3
    removed = gen.rejections[0]
    assert removed.candidate.targets == ["F1"] and removed.stage == "validate"
    assert any("removed by op 't18_set'" in e for e in removed.errors)
    assert any("above the maximum" in e for e in gen.rejections[1].errors)
    assert any("unknown op" in e for e in gen.rejections[2].errors)
    rejected = [e for e in result.trace.of_kind("candidate") if e["outcome"] == "rejected"]
    assert len(rejected) == 3 and rejected[0]["source"] == "ai"
    assert rejected[0]["stage"] == "validate" and "removed by op" in rejected[0]["errors"][0]
    assert 5 not in [a["q"] for a in SetOp.applied]  # never applied


def test_declined_and_failed_ops_are_reported_and_search_continues() -> None:
    def behave(params: dict) -> None:
        if params["q"] == 1:
            raise OpDeclined("nothing to split")
        if params["q"] == 2:
            raise OpFailed("timeout", "worker did not answer")
        if params["q"] == 3:
            raise RuntimeError("boom")

    SetOp.behaviour = behave
    gen = Scripted(lambda s: [cand(1), cand(2), cand(3), cand(4)] if s.depth == 0 else [])
    result, _ = run([gen], FakeJudge())
    assert _q(result.best) == 4
    stages = {r.candidate.params["q"]: r.stage for r in gen.rejections}
    assert stages == {1: "declined", 2: "failed", 3: "error"}
    assert gen.rejections[0].errors == ["nothing to split"]
    assert "timeout" in gen.rejections[1].errors[0]


def test_duplicate_state_is_not_expanded_twice() -> None:
    # fingerprints equal => same state; fake the collision by an op that returns the same ids
    fj = FakeJudge()
    gen = Scripted(lambda s: [cand(1)])
    start = _ws(0)

    class SameOp(SetOp):
        name = "t18_same"

        def apply(self, pool, ws, targets, params, *, timeout_s):
            new = ws.fork()
            new.brep_path = f"/fake/same{next(_counter)}.brep"
            new.labels = {"_q": {"q": 1}}
            return new, Diff(
                op=self.name,
                params=params,
                modified={},
                generated={},
                removed=[],
                declared_added=[],
                declared_removed=[],
                seconds=0.0,
            )

    REGISTRY["t18_same"] = SameOp()
    try:
        gen = Scripted(lambda s: [Candidate("t18_same", [], {"q": 1}, "det")])
        result = search(None, start, start.original, [gen], BIG, workers=1, judge=fj.judge(), patience=50)
    finally:
        del REGISTRY["t18_same"]
    assert [e["outcome"] for e in result.trace.of_kind("candidate")].count("duplicate") >= 1
    assert gen.calls <= 3


def test_trace_is_json_and_records_lineage(tmp_path) -> None:
    fj = FakeJudge(target=2)
    gen = Scripted(lambda s: [cand(1)] if s.depth == 0 else [cand(2)])
    result, _ = run([gen], fj)
    path = tmp_path / "trace.json"
    result.trace.write(path)
    data = json.loads(path.read_text())
    kinds = [e["kind"] for e in data["events"]]
    assert kinds[0] == "start" and kinds[-1] == "stop"
    assert {"expand", "candidate", "confirm", "best"} <= set(kinds)
    expand = [e for e in data["events"] if e["kind"] == "expand"][1]
    assert expand["parent"] == "S0" and expand["depth"] == 1 and expand["via"]["op"] == "t18_set"
    stop = data["events"][-1]
    assert stop["spent"]["ops"] == 2 and stop["budget"]["ops"] == 1000
    assert [s["id"] for s in expand["sites"]] == ["S1"]


def test_deliverable_passthrough_when_fewer_panel_passes() -> None:
    fj = FakeJudge()
    # the search improves cheap-first but the input passed 5 members; best only passes 3
    gen = Scripted(lambda s: [cand(3)] if s.depth == 0 else [])
    result, start = run([gen], fj, start_q=1)
    assert _q(result.best) == 3
    assert deliverable(result, FakeVerdict(5, False, "full"), score_fn=fj._score) is None
    assert deliverable(result, FakeVerdict(3, False, "full"), score_fn=fj._score) is result.best
    assert deliverable(result, FakeVerdict(2, False, "full"), score_fn=fj._score) is result.best


def test_deliverable_never_touches_a_clean_input_or_an_unchanged_result() -> None:
    fj = FakeJudge(target=3)
    gen = Scripted(lambda s: [cand(9)])
    result, _ = run([gen], fj, start_q=1)
    assert result.clean
    assert deliverable(result, FakeVerdict(3, True, "full"), score_fn=fj._score) is None
    flat = Scripted(lambda s: [])
    result2, start = run([flat], FakeJudge(), start_q=1)
    assert result2.best is start
    assert deliverable(result2, FakeVerdict(0, False, "full"), score_fn=fj._score) is None


# --- DetGenerator -------------------------------------------------------------------------------


def _site(kind: str, entities: list[str]) -> Site:
    return Site(
        id="S1",
        kind=kind,
        entities=entities,
        xyz=[],
        meshers_failing=[],
        dist_over_h=None,
        graph={},
        view_png=None,
    )


def _state(sites: list[Site]) -> SearchState:
    return SearchState(
        id="S0",
        ws=_ws(0),
        verdict=FakeVerdict(0, False, "full"),
        score=(1, 0, 0.0, 0.0, 0),
        sites=sites,
        depth=0,
        parent=None,
        via=None,
    )


def test_det_generator_sweeps_parameters_per_site_kind() -> None:
    gen = DetGenerator()
    prox = gen.propose(_state([_site("proximity", ["F1", "F2"])]), 50)
    ratios = sorted(c.params["target_ratio"] for c in prox if c.op == "split_by_proximity")
    assert ratios == [0.06, 0.125, 0.25]
    assert all(c.source == "det" and c.rationale for c in prox)
    assert {c.op for c in prox} >= {"split_by_proximity", "split_angle"}

    sliver = gen.propose(_state([_site("sliver", ["F1"])]), 50)
    assert {c.params.get("max_width_mm") for c in sliver if c.op == "remove_small"} == {0.001, 0.01, 0.05}

    assert gen.propose(_state([_site("unlocated", ["F1"])]), 50) == []
    assert len(gen.propose(_state([_site("proximity", ["F1", "F2"])]), 2)) == 2
    # every proposed call is well formed against the real registry
    from meshpipeline.cad.repair_v4.ops import validate_call

    st = _state([_site("proximity", ["F1", "F2"])])
    for c in gen.propose(st, 50):
        assert validate_call(c.op, c.targets, c.params, st.ws) == [], c
