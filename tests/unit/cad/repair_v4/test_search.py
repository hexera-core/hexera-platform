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
    SearchResult,
    SearchState,
    deliverable,
    search,
)
from meshpipeline.cad.repair_v4.trace import Trace
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

_counter = itertools.count(1)


@dataclass
class FakeVerdict:
    q: int
    clean: bool
    tier: str


def _ws(
    q: int, c: int | None = None, *, ids: tuple[str, ...] = ("F1", "F2"), history: list[Diff] | None = None
) -> Workspace:
    n = next(_counter)
    return Workspace(
        brep_path=f"/fake/{n}.brep",
        ids={i: f"fp{n}{i}" for i in ids},
        labels={"_q": {"q": q, "c": q if c is None else c}},
        history=history or [],
        original="/fake/orig.brep",
    )


def _q(ws: Workspace) -> int:
    return int(ws.labels["_q"]["q"])


def _c(ws: Workspace) -> int:
    """What the CHEAP tier reports (a subset panel); equals q unless a test makes them differ."""
    return int(ws.labels["_q"]["c"])


class SetOp:
    """Fake op: a new workspace whose quality is params['q']; params['drop'] removes F1."""

    name = "t18_set"
    params_schema = {
        "type": "object",
        "properties": {
            "q": {"type": "integer", "minimum": 0, "maximum": 100},
            "c": {"type": "integer"},
            "drop": {"type": "boolean"},
        },
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
        new = _ws(params["q"], params.get("c"), ids=ids, history=[*ws.history, diff])
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
        self.cheap_calls.append(_c(ws))
        return FakeVerdict(_c(ws), _q(ws) >= self.target, "cheap")

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

    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        self.calls += 1
        self.states.append(state)
        return list(self.script(state))[:k]

    def on_rejected(self, rejections: list[Rejection]) -> None:
        self.rejections.extend(rejections)


class ScriptedAI(Scripted):
    """A model-backed generator: it reports cumulative usage, so the driver can cap it."""

    def __init__(self, script: Any) -> None:
        super().__init__(script, source="ai")
        self.model_calls = 0
        self.model_usd = 0.0

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


def test_model_budget_drops_the_ai_but_keeps_searching_and_marks_ai_capped() -> None:
    def ai(s: SearchState) -> list[Candidate]:
        gen.model_calls += 1
        gen.model_usd += 0.5
        return [cand(s.depth + 1)]

    for budget in (
        Budget(wall_s=1000, ops=1000, model_calls=2, model_usd=100),
        Budget(wall_s=1000, ops=1000, model_calls=100, model_usd=1.0),
    ):
        gen = ScriptedAI(ai)
        det = Scripted(lambda s: [cand(50 + s.depth)])
        result, _ = run([gen, det], FakeJudge(), budget, patience=6)
        assert result.ai_capped
        assert gen.calls == 2  # never asked again once the cap was reached
        assert det.calls > 2  # the deterministic generator carried on
        capped = result.trace.of_kind("ai_capped")
        assert len(capped) == 1 and result.trace.of_kind("stop")[0]["ai_capped"] is True


def test_zero_model_budget_means_no_ai_not_stop() -> None:
    zero = Budget(wall_s=1000, ops=1000, model_calls=0, model_usd=0.0)
    det = Scripted(lambda s: [cand(1)] if s.depth == 0 else [])
    result, _ = run([det], FakeJudge(), zero)  # deterministic-only: the search_det arm
    assert det.calls >= 1 and _q(result.best) == 1 and not result.ai_capped
    ai = ScriptedAI(lambda s: [cand(9)])
    result2, _ = run([ai, Scripted(lambda s: [cand(2)] if s.depth == 0 else [])], FakeJudge(), zero)
    assert ai.calls == 0 and result2.ai_capped and _q(result2.best) == 2


def test_confirms_top_k_with_full_panel() -> None:
    fj = FakeJudge()
    gen = Scripted(lambda s: [cand(q) for q in (4, 9, 2, 7, 5)] if s.depth == 0 else [])
    result, start = run([gen], fj, k_confirm=2)
    # the full panel saw the start and exactly the two best cheap scorers, nothing else
    assert sorted(fj.full_calls) == [0, 7, 9]
    assert sorted(fj.cheap_calls) == [0, 2, 4, 5, 7, 9]  # 0 is the start, for calibration
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
            new.labels = {"_q": {"q": 1, "c": 1}}
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
    gen = DetGenerator(diagonal=lambda ws: 100.0)  # a 100 mm part: bound 0.1 mm
    prox = gen.propose(_state([_site("proximity", ["F1", "F2"])]), 50)
    ratios = sorted(c.params["target_ratio"] for c in prox if c.op == "split_by_proximity")
    assert ratios == [0.06, 0.125, 0.25]
    assert all(c.source == "det" and c.rationale for c in prox)
    assert {c.op for c in prox} >= {"split_by_proximity", "split_angle"}

    sliver = gen.propose(_state([_site("sliver", ["F1"])]), 50)
    assert sorted(c.params["max_width_mm"] for c in sliver if c.op == "remove_small") == pytest.approx([0.001, 0.01, 0.05])

    assert gen.propose(_state([_site("unlocated", ["F1"])]), 50) == []
    assert len(gen.propose(_state([_site("proximity", ["F1", "F2"])]), 2)) == 2
    # every proposed call is well formed against the real registry
    from meshpipeline.cad.repair_v4.ops import validate_call

    st = _state([_site("proximity", ["F1", "F2"])])
    for c in gen.propose(st, 50):
        assert validate_call(c.op, c.targets, c.params, st.ws) == [], c


# --- robustness: one failing call never loses the best state ------------------------------------


class Boom(Exception):
    pass


def test_proposer_exception_is_recorded_and_search_continues() -> None:
    def flaky(s: SearchState) -> list[Candidate]:
        if flaky.calls == 0:  # type: ignore[attr-defined]
            flaky.calls += 1  # type: ignore[attr-defined]
            raise Boom("model hiccup")
        return [cand(4)]

    flaky.calls = 0  # type: ignore[attr-defined]
    good = Scripted(lambda s: [cand(1)] if s.depth == 0 else [])
    result, _ = run([Scripted(flaky), good], FakeJudge())
    errors = result.trace.of_kind("generator_error")
    assert errors and errors[0]["where"] == "propose" and "Boom: model hiccup" in errors[0]["error"]
    assert _q(result.best) >= 1


def test_on_rejected_exception_is_recorded() -> None:
    class Bad(Scripted):
        def on_rejected(self, rejections: list[Rejection]) -> None:
            raise Boom("cannot take feedback")

    gen = Bad(lambda s: [Candidate("nope", [], {}, "det"), cand(2)] if s.depth == 0 else [])
    result, _ = run([gen], FakeJudge())
    assert _q(result.best) == 2
    assert result.trace.of_kind("generator_error")[0]["where"] == "on_rejected"


def test_sites_and_confirm_failures_keep_the_best_so_far() -> None:
    fj = FakeJudge()
    judge = fj.judge()
    full_calls = {"n": 0}

    def sites(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
        if verdict.q == 3:
            raise Boom("site finder died")
        return fj._sites(pool, ws, verdict)

    def full(pool: Any, ws: Workspace, original: str) -> FakeVerdict:
        full_calls["n"] += 1
        if _q(ws) == 7:
            raise Boom("kernel crashed in confirm")
        return fj._full(pool, ws, original)

    judge = Judge(cheap=judge.cheap, full=full, score=judge.score, sites=sites)
    gen = Scripted(lambda s: [cand(3), cand(7)] if s.depth == 0 else [cand(1)], source="det")
    start = _ws(0)
    result = search(None, start, start.original, [gen], BIG, workers=2, judge=judge, patience=5)
    assert _q(result.best) == 3  # q=7 could not be confirmed so it is never delivered; 3 was kept
    assert result.trace.of_kind("confirm_error")[0]["error"].startswith("Boom")
    assert any(e["where"] == "sites" for e in result.trace.of_kind("judge_error"))
    assert result.trace.of_kind("stop")


# --- frontier order when cheap and full disagree -------------------------------------------------


def test_frontier_ranks_unconfirmed_children_against_confirmed_states_by_calibration() -> None:
    """Cheap and full DISAGREE in both directions. P is better than its cheap score says (full 4,
    cheap 1); its children C1/C2 look equal to P under cheap (1) but are really worse (full 1) once
    confirmed - the full tier demotes them. C3 is not confirmed (k_confirm=2): its cheap score alone
    (1) would rank it below sibling S (full 2) and starve it; calibrated against P (4 + (1 - 1)) it
    is expanded before S."""

    def script(s: SearchState) -> list[Candidate]:
        if s.depth == 0:
            return [cand(4, c=1), cand(2, c=1)]  # P, S
        if s.id == "S1":  # expanding P
            return [cand(1, c=1), cand(1, c=1), cand(1, c=1)]  # C1, C2, C3
        return []

    fj = FakeJudge()
    gen = Scripted(script)
    start = _ws(0, 0)
    result = search(None, start, start.original, [gen], BIG, workers=1, judge=fj.judge(), k_confirm=2, patience=10)
    order = [e["state"] for e in result.trace.of_kind("expand")]
    assert order[:4] == ["S0", "S1", "S5", "S2"], order
    confirms = {e["state"]: e for e in result.trace.of_kind("confirm")}
    assert confirms["S1"]["score"][1] > confirms["S1"]["cheap_score"][1]  # full above cheap
    assert confirms["S3"]["score"][1] == confirms["S3"]["cheap_score"][1]  # tie under cheap...
    assert confirms["S3"]["score"][1] < confirms["S1"]["score"][1]  # ...but below its parent in full
    assert _q(result.best) == 4


# --- default_judge: its calls into the REAL Task 5 / 17 / 16 modules are pinned in test_search_wiring.py ------


def test_search_requires_h_mm_when_no_judge_is_given() -> None:
    start = _ws(0)
    with pytest.raises(ValueError, match="h_mm"):
        search(None, start, start.original, [], BIG, workers=1)
    with pytest.raises(TypeError):
        from meshpipeline.cad.repair_v4.search import default_judge

        default_judge()


def test_deliverable_fallback_uses_the_real_score_and_names_the_panel_field() -> None:
    from meshpipeline.cad.repair_v4.search import SCORE_PANEL_PASSES, panel_passes
    from meshpipeline.cad.repair_v4.verdict.assemble import Verdict
    from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
    from meshpipeline.cad.repair_v4.verdict.valid import ValidReport

    @dataclass(frozen=True)
    class Member:
        name: str
        status: str = "pass"
        min_sicn: float | None = 0.3

    def real(passes: int) -> Verdict:
        return Verdict(
            ValidReport(True, [], [], "mm", 1, True),
            [Member(f"m{i}") for i in range(passes)],
            FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1),
            False,
            "full",
        )

    assert SCORE_PANEL_PASSES == 1
    result = SearchResult(best=_ws(3), best_verdict=real(3), clean=False, trace=Trace(), changed=True)
    assert deliverable(result, real(5)) is None  # the real score, no score_fn
    assert deliverable(result, real(2)) is result.best
    with pytest.raises(AssertionError, match="changed shape"):
        panel_passes((1, 2, 3.0))


def test_det_generator_proposes_for_a_whole_part_open_site() -> None:
    """The site an open shell yields has no entities; the sweep must still offer sew / make_volume /
    orient_and_close, and every call must be well formed against the registry."""
    from meshpipeline.cad.repair_v4.ops import validate_call

    state = _state([_site("open", [])])
    got = DetGenerator(diagonal=lambda ws: 100.0).propose(state, 50)
    assert {c.op for c in got} == {"sew", "make_volume", "orient_and_close"}
    for c in got:
        assert validate_call(c.op, c.targets, c.params, state.ws) == []


# --- D9: the AI is re-prompted while it has turns and dollars, not dropped when the frontier empties -------------


class ListeningAI(ScriptedAI):
    """A model generator that also hears what became of its candidates (as AIProposer does via `feedback`)."""

    def __init__(self, script: Any) -> None:
        super().__init__(script)
        self.heard: list[list[Any]] = []
        self.stop_reason: str | None = None

    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        self.model_calls += 1
        self.model_usd += 0.01
        return super().propose(state, k)

    def feedback(self, outcomes: list[Any]) -> None:
        self.heard.append(list(outcomes))


def _declining(params: dict) -> None:
    raise OpDeclined("no_change: nothing to do here")


def test_an_ai_whose_proposals_decline_is_reprompted_until_patience() -> None:
    """Snapshot v1: on 19 of 27 files search_ai stopped `exhausted` after ONE model turn - its proposals declined,
    the frontier emptied, and the search ended with turns and dollars left. Now the model is asked again on the best
    state, told why its candidates failed, until patience (or the $ ceiling, or the wall) ends the search."""
    SetOp.behaviour = _declining
    ai = ListeningAI(lambda s: [Candidate("t18_set", [], {"q": 5}, "ai", rationale="try")])
    result, start = run([ai], FakeJudge())
    assert ai.calls > 1, "the model was asked once and the search reported exhausted"
    assert stop_reason(result) == "patience"
    assert len(result.trace.of_kind("reprompt")) == ai.calls - 1
    assert all(s.ws is start for s in ai.states)  # re-asked on the best state, which is still the start
    # each re-prompt carries the outcome of the previous round: the decline and its reason
    assert ai.heard and all(o.status == "declined" and "no_change" in o.message for batch in ai.heard for o in batch)


def test_no_reprompt_once_the_ai_has_stopped_itself_or_its_budget_is_spent() -> None:
    SetOp.behaviour = _declining
    stopped = ListeningAI(lambda s: [Candidate("t18_set", [], {"q": 5}, "ai")])
    stopped.stop_reason = "file_ceiling"  # the proposer's own stop: the per-file $ ceiling
    result, _ = run([stopped], FakeJudge())
    assert stop_reason(result) == "exhausted" and not result.trace.of_kind("reprompt")
    capped = ListeningAI(lambda s: [Candidate("t18_set", [], {"q": 5}, "ai")])
    result, _ = run([capped], FakeJudge(), Budget(wall_s=1000, ops=1000, model_calls=2, model_usd=100))
    assert capped.calls == 2 and stop_reason(result) == "exhausted"


def test_a_reprompt_asks_only_the_model_not_the_deterministic_sweep() -> None:
    SetOp.behaviour = _declining
    ai = ListeningAI(lambda s: [Candidate("t18_set", [], {"q": 5}, "ai")])
    det = Scripted(lambda s: [cand(7)])
    run([ai, det], FakeJudge())
    assert det.calls == 1 and ai.calls > 1


def test_search_det_is_unchanged_one_expansion_then_exhausted() -> None:
    """The deterministic arm has no model generator: when its frontier empties it is exhausted, exactly as before."""
    SetOp.behaviour = _declining
    det = Scripted(lambda s: [cand(7), cand(8)])
    result, _ = run([det], FakeJudge())
    assert det.calls == 1 and stop_reason(result) == "exhausted" and not result.trace.of_kind("reprompt")
    SetOp.behaviour = None
    det_ok = Scripted(lambda s: [cand(s.depth + 1)] if s.depth < 2 else [])
    result, _ = run([det_ok], FakeJudge(), patience=9)
    assert stop_reason(result) == "exhausted" and not result.trace.of_kind("reprompt") and det_ok.calls == 3


# --- B18: the search is ANYTIME - a wall must not erase its result ------------------------------


def test_the_wall_stops_proposing_at_the_reserve_so_the_verdict_budget_is_real(monkeypatch) -> None:
    """The VERDICT BUDGET comes OUT of the wall: proposing stops at 1 - VERDICT_RESERVE of it. Pinned at 8.5 s of a
    10 s wall, a reserve of 0.2 stops the search and a reserve of 0 does not - which is the reserve, measured."""
    from meshpipeline.cad.repair_v4 import search as search_mod

    def pinned(at: float):
        calls = {"n": 0}

        def clock() -> float:
            calls["n"] += 1
            return 0.0 if calls["n"] <= 6 else at      # the start's own judging happens at t=0
        return clock

    def run_at(reserve: float, at: float) -> str:
        monkeypatch.setattr(search_mod, "VERDICT_RESERVE", reserve)
        fj = FakeJudge()
        start = _ws(0)
        got = search_mod.search(None, start, start.original, [Scripted(lambda s: [cand(1)] if s.depth < 1 else [])],
                                Budget(wall_s=10, ops=1000, model_calls=99, model_usd=99), workers=1,
                                judge=fj.judge(), patience=99, clock=pinned(at))
        return str(got.reason)

    assert run_at(0.2, 8.5) == "wall", "8.5 s is past the 8 s proposing deadline of a 10 s wall"
    assert run_at(0.0, 8.5) != "wall", "with no reserve the same 8.5 s is still inside the wall"


def test_a_wall_stopped_search_is_CONCLUSIVE_and_carries_its_best_state() -> None:
    """B16's finding, fixed: on bcf201b55323 the AI's row passed 3 panel members and was discarded for stopping on
    the wall. A wall stop now delivers the best state's FULL verdict, so the row counts."""
    ticks = itertools.count(0, 1)
    fj = FakeJudge()
    gen = Scripted(lambda s: [cand(3)] if s.depth == 0 else [cand(_q(s.ws))])
    start = _ws(0)
    result = search(None, start, start.original, [gen], Budget(wall_s=12, ops=1000, model_calls=99, model_usd=99),
                    workers=1, judge=fj.judge(), clock=lambda: float(next(ticks)))
    assert result.reason == "wall" and result.stopped_on_wall is True
    assert result.best_tier == "full", "the delivered state must be judged by the whole panel"
    assert result.unmeasured is False, "a wall stop with a full verdict is a MEASUREMENT, not machine time"
    assert _q(result.best) == 3 and result.best_verdict.q == 3


def test_a_cheap_only_best_state_is_given_the_full_panel_in_the_reserve() -> None:
    """When the wall lands before the confirm step has judged the best state, the reserve pays for it: the row is
    still full-tier. The trace records that final verdict."""
    fj = FakeJudge()
    gen = Scripted(lambda s: [cand(2)] if s.depth == 0 else [])
    result, _start = run([gen], fj, Budget(wall_s=1000, ops=1000, model_calls=9, model_usd=9), patience=1)
    assert result.best_tier == "full"
    # the search's own confirm already made it full, so no reserve verdict was needed
    assert result.final_verdict is None
    assert result.trace.of_kind("final_verdict") == []


def test_the_delivered_state_is_always_full_tier_whatever_stopped_the_search() -> None:
    """Why the reserve is a GUARANTEE and not a hope: `search` only ever promotes a child to best AFTER the confirm
    step has judged it with the full panel, so the delivered state carries a full verdict however the search ended.
    The reserve keeps time for the one case that is not covered - a best state that never got that far - and
    `final_verdict` records what happened when it is used."""
    fj = FakeJudge()
    for reason, kw in (("ops", {"budget": Budget(wall_s=1000, ops=2, model_calls=9, model_usd=9)}),
                       ("patience", {"patience": 1}),
                       ("exhausted", {})):
        gen = Scripted(lambda s: [cand(_q(s.ws) + 1)] if _q(s.ws) < 2 and reason != "exhausted" else [])
        result, _start = run([gen], fj, **kw)
        assert result.best_tier == "full", f"{reason}: delivered tier {result.best_tier}"
        assert result.unmeasured is False, reason


def test_a_time_stop_whose_final_verdict_did_not_complete_is_the_ONLY_unmeasured_case() -> None:
    """The rule, stated as the ruling states it: a file is unmeasured only if even the final verdict cannot
    complete. `out_tier` carries that - the full panel is the only tier that can say what a state is."""
    from meshpipeline.cad.repair_v4.measurement import file_unmeasured

    common = {"infrastructure_failed": False, "inconclusive_candidates": 0, "verdict_candidates": 4,
              "out_clean": False}
    assert file_unmeasured(reason="wall", out_tier="cheap", **common) is True
    assert file_unmeasured(reason="wall", out_tier=None, **common) is True
    assert file_unmeasured(reason="wall", out_tier="full", **common) is False


def test_every_arm_gets_the_same_rule_whatever_stopped_it() -> None:
    """det exhausting its candidates in 50 s and the AI still working at 650 s are judged by ONE rule: the tier of
    the verdict being delivered, not how the search ended."""
    from meshpipeline.cad.repair_v4.measurement import file_unmeasured

    common = {"infrastructure_failed": False, "inconclusive_candidates": 0, "verdict_candidates": 4,
              "out_clean": False}
    assert file_unmeasured(reason="exhausted", out_tier="full", **common) is False
    assert file_unmeasured(reason="patience", out_tier="full", **common) is False
    assert file_unmeasured(reason="wall", out_tier="full", **common) is False      # B18: was True
    assert file_unmeasured(reason="wall", out_tier="cheap", **common) is True
    # a row written BEFORE the ruling carries no tier on a time stop and keeps its old classification
    assert file_unmeasured(reason="wall", **common) is True
