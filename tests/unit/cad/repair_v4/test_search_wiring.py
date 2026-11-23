# Responsibility: Prove the search driver is WIRED to the proposer (Task 19), the sites module (Task 17), the
#                 stages (Task 16) and the checker (Task 5): the proposer hears what became of its candidates, one
#                 unfixable registry is shared, site ids survive a forking search, `run_stage` is routed, a raising
#                 `score` cannot end a branch, and `default_judge` calls the REAL verdict/sites modules.
# Boundaries: hermetic. The REAL AIProposer runs against a scripted model (no network); the REAL Verdict, score,
#             SiteRegistry, observation, stages and render modules run. Only the kernel (WorkerPool) and the ops that
#             would need one are fakes, registered in `ops.REGISTRY`.
from __future__ import annotations

import base64
import inspect
import itertools
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import render, search, stages
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.ops import REGISTRY
from meshpipeline.cad.repair_v4.proposer import AIProposer, Outcome, UnfixableRegistry
from meshpipeline.cad.repair_v4.search import (
    Budget,
    Candidate,
    DetGenerator,
    Judge,
    SearchResult,
    default_judge,
)
from meshpipeline.cad.repair_v4.sites import LineageMismatch, SiteRegistry
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict, Verdict, score
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace
from meshpipeline.contracts.model_inference import ModelRoundResult, ProposeReply, ToolCallRequest

ROUTE = "openai:gpt-6-astra"
ASTRA = (10.0, 50.0, 1.0)
_n = itertools.count(1)
BIG = Budget(wall_s=1000, ops=1000, model_calls=1000, model_usd=1000)


# --- a small world: workspaces that carry a quality, a fake op, a Judge made of REAL Verdicts ---------------


@dataclass(frozen=True)
class Member:
    """A panel member with every field the real `observation` and `score` read."""

    name: str
    status: str = "pass"
    min_sicn: float | None = 0.3
    error_kind: str | None = None
    message: str = ""
    elements: int = 10
    located: list = field(default_factory=list)


def make_verdict(q: int, tier: str, target: int, inconclusive: bool = False) -> Verdict:
    panel: list[Any] = [Member(f"m{i}") for i in range(q)]
    if inconclusive:
        panel.append(Member("pool", "timeout", None, "infrastructure"))
    return Verdict(
        ValidReport(True, [], [], "mm", 1, True),
        panel,
        FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1),
        clean=tier == "full" and q >= target and not inconclusive,
        tier=tier,  # type: ignore[arg-type]
        inconclusive=inconclusive,
    )


def make_ws(q: int, *, ids: tuple[str, ...] = ("F1", "F2"), history: list[Diff] | None = None,
            inc: int = 0, fp: str | None = None) -> Workspace:
    n = next(_n)
    return Workspace(
        brep_path=f"/fake/w{n}.brep",
        ids={i: (fp or f"fp{n}") + i for i in ids},
        labels={"_q": {"q": q, "inc": inc}},
        history=history or [],
        original="/fake/orig.brep",
        indices={i: k + 1 for k, i in enumerate(ids)},
    )


def _q(ws: Workspace) -> int:
    return int(ws.labels["_q"]["q"])


class T18Op:
    """Moves the workspace to quality params['q']. `to` replaces F1 by that id (a Modified diff); `dup` keeps the
    geometry fingerprint of the parent (the same state reached again); `inc` makes the first `inc` cheap verdicts of
    the new state inconclusive."""

    name = "t18_set"
    params_schema = {
        "type": "object",
        "properties": {"q": {"type": "integer", "minimum": 0, "maximum": 100}, "to": {"type": "string"},
                       "inc": {"type": "integer", "minimum": 0, "maximum": 100}, "dup": {"type": "boolean"}},
        "required": ["q"], "additionalProperties": False, "x-targets": {"kind": "face", "min": 0},
    }

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        to = params.get("to")
        ids = tuple(to if (i == "F1" and to) else i for i in ws.ids)
        diff = Diff(op=self.name, params=params, modified={"F1": [to]} if to and "F1" in ws.ids else {},
                    generated={}, removed=[], declared_added=[], declared_removed=[], seconds=0.0)
        fp = None
        if params.get("dup"):
            fp = "same"
        new = make_ws(params["q"], ids=ids, history=[*ws.history, diff], inc=int(params.get("inc", 0)), fp=fp)
        return new, diff


class FinaliseHelper:
    """The op a `finalise` stage applies; one step better, new geometry."""

    name = "orient_and_close"
    params_schema = {"type": "object", "x-targets": {"kind": "solid", "min": 0}}

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        diff = Diff(op=self.name, params=params, modified={}, generated={}, removed=[], declared_added=[],
                    declared_removed=[], seconds=0.0)
        new = make_ws(_q(ws) + 1, ids=tuple(ws.ids), history=[*ws.history, diff])
        return new, diff


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "t18_set", T18Op())  # type: ignore[arg-type]
    monkeypatch.setitem(REGISTRY, "orient_and_close", FinaliseHelper())  # type: ignore[arg-type]


class World:
    """A Judge: REAL `Verdict`s and the REAL `score`; sites are whatever `sites_fn` says."""

    def __init__(self, target: int = 99, sites_fn: Any = None) -> None:
        self.target = target
        self.sites_fn = sites_fn or self.one_site
        self.cheap_calls = 0
        self._inc_left: dict[str, int] = {}

    def judge(self) -> Judge:
        return Judge(cheap=self.cheap, full=self.full, score=score, sites=self.sites_fn)

    def cheap(self, pool: Any, ws: Workspace, original: str) -> Verdict:
        self.cheap_calls += 1
        left = self._inc_left.setdefault(ws.brep_path, int(ws.labels["_q"]["inc"]))
        if left > 0:
            self._inc_left[ws.brep_path] = left - 1
            return make_verdict(_q(ws), "cheap", self.target, inconclusive=True)
        return make_verdict(_q(ws), "cheap", self.target)

    def full(self, pool: Any, ws: Workspace, original: str) -> Verdict:
        return make_verdict(_q(ws), "full", self.target)

    @staticmethod
    def one_site(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
        return [site("S1", "proximity", ["F1", "F2"])]


def site(sid: str, kind: str, entities: list[str]) -> Site:
    return Site(id=sid, kind=kind, entities=entities, xyz=[(0.0, 0.0, 0.0)], meshers_failing=["gmsh"],
                dist_over_h=0.5, graph={}, view_png=None)  # type: ignore[arg-type]


def run(gens: list[Any], w: World, start: Workspace | None = None, budget: Budget = BIG, pool: Any = None,
        **kw: Any) -> SearchResult:
    start = start or make_ws(0)
    return search.search(pool, start, "/fake/orig.step", gens, budget, workers=2, judge=w.judge(), **kw)


class Scripted:
    def __init__(self, script: Any) -> None:
        self.script = script
        self.states: list[search.SearchState] = []

    def propose(self, state: search.SearchState, k: int) -> list[Candidate]:
        self.states.append(state)
        return list(self.script(state))[:k]


def cand(q: int, **extra: Any) -> Candidate:
    return Candidate("t18_set", [], {"q": q, **extra}, "det", rationale=f"set q={q}")


# --- a scripted model, as Task 19's own tests use ------------------------------------------------------------------


def call_of(name: str, args: Any, call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=json.dumps(args))


def reply(*calls: ToolCallRequest) -> ModelRoundResult:
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text="", finish_reason="tool_calls")


@dataclass
class FakeModel:
    script: list
    cost: float = 0.01
    calls: list = field(default_factory=list)

    async def __call__(self, messages, tools, *, route, job_id="", user_id="", tool_choice="auto", cache_key=""):
        self.calls.append(json.loads(json.dumps(messages, default=str)))
        item = self.script.pop(0) if self.script else reply()
        return ProposeReply(reply=item, cost_usd=self.cost, provider="openai", model="gpt-6-astra")


def proposer(model: FakeModel, *, unfixable: UnfixableRegistry | None = None, **kw: Any) -> AIProposer:
    kw.setdefault("max_turns", 8)
    kw.setdefault("usd_ceiling", 1.0)
    return AIProposer(ROUTE, ledger=WaveLedger.for_test(cap_usd=100.0), call=model, file_id="f1", job_id="j",
                      prices=ASTRA, output_tokens=1000, unfixable=unfixable,
                      stage_names=[s.name for s in stages.STAGES], **kw)


def ai_cand(q: int, **extra: Any) -> dict:
    return {"op": "t18_set", "targets": [], "params": {"q": q, **extra}, "rationale": f"try q={q}"}


def user_turns(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        if m["role"] == "user":
            c = m["content"]
            out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c))
    return out


# --- item 1: feedback --------------------------------------------------------------------------------------------


def test_the_proposer_is_sent_the_diff_and_verdict_delta_of_its_own_candidates() -> None:
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(2)]})), reply()])
    ai = proposer(model)
    result = run([ai], World())
    assert result.changed
    # the second model round was opened with what the FIRST round's candidate did
    second = user_turns(model.calls[1])[-1]
    assert "previous_candidates" in second
    body = json.loads(second.split("\n", 1)[1])
    [prev] = body["previous_candidates"]
    assert prev["status"] == "applied" and prev["op"] == "t18_set"
    assert prev["diff"]["op"] == "t18_set"
    assert prev["delta"]["panel_passes"] == [0, 2]


def test_every_outcome_status_reaches_only_the_generator_that_proposed_it() -> None:
    got: list[Outcome] = []
    other: list[Outcome] = []

    class Fb(Scripted):
        def __init__(self, script: Any, sink: list[Outcome]) -> None:
            super().__init__(script)
            self.sink = sink

        def feedback(self, outcomes: Any) -> None:
            self.sink.extend(outcomes)

    good = Fb(lambda s: [cand(1)] if s.depth == 0 else [], got)
    bad = Fb(lambda s: [Candidate("nope", [], {}, "ai")] if s.depth == 0 else [], other)
    run([good, bad], World())
    assert [o.status for o in got] == ["applied"]
    assert [o.status for o in other] == ["invalid"] and "unknown op" in other[0].message


# --- items 2, 3: one unfixable registry; stop_reason --------------------------------------------------------------


class SpyDet(DetGenerator):
    seen: list[list[str]] = []

    @staticmethod
    def _sweep(site: Site) -> list:
        SpyDet.seen[-1].append(site.id)
        return []

    def propose(self, state: search.SearchState, k: int) -> list[Candidate]:
        SpyDet.seen.append([])
        return super().propose(state, k)


def test_a_site_the_model_declares_unfixable_is_no_longer_swept_and_no_longer_shown() -> None:
    SpyDet.seen = []
    shared = UnfixableRegistry()
    model = FakeModel([
        reply(call_of("declare_unfixable", {"site_id": "S1", "reason": "fix would breach the fidelity bound"}, "a"),
              call_of("propose", {"candidates": [ai_cand(1)]}, "b")),
        reply(),
    ])
    ai = proposer(model, unfixable=shared)
    run([SpyDet(unfixable=shared), ai], World())
    assert SpyDet.seen[0] == ["S1"]            # same round: the deterministic sweep ran before the model spoke
    assert SpyDet.seen[1] == []                # next round: the registry the model wrote to filtered it
    assert '"sites": []' in user_turns(model.calls[1])[-1]


def test_a_deterministic_generator_that_does_not_share_the_registry_is_refused() -> None:
    ai = proposer(FakeModel([]), unfixable=UnfixableRegistry())
    with pytest.raises(ValueError, match="UnfixableRegistry"):
        run([DetGenerator(), ai], World())
    with pytest.raises(ValueError, match="UnfixableRegistry"):
        run([DetGenerator(unfixable=UnfixableRegistry()), ai], World())


def test_ai_generators_builds_one_shared_registry_and_names_the_stages() -> None:
    gens, ai = search.ai_generators(ROUTE, ledger=WaveLedger.for_test(cap_usd=10.0), max_turns=3, usd_ceiling=1.0,
                             file_id="f", job_id="j", call=FakeModel([]), prices=ASTRA)
    assert isinstance(gens[0], DetGenerator) and gens[1] is ai
    assert gens[0].unfixable is ai.unfixable and gens[0].unfixable is not None
    assert ai.stage_names == tuple(sorted(s.name for s in stages.STAGES))


def test_ai_generators_refuses_a_ledger_that_is_not_a_wave_ledger() -> None:
    with pytest.raises(TypeError):
        search.ai_generators(ROUTE, ledger=object(), max_turns=3, usd_ceiling=1.0, file_id="f",  # type: ignore[arg-type]
                      job_id="j", call=FakeModel([]), prices=ASTRA)


def test_input_too_large_and_model_failure_are_recorded_and_are_not_ai_capped() -> None:
    ai = proposer(FakeModel([]), max_input_tokens=1)
    r = run([ai], World())
    assert r.ai_stop_reason == "input_too_large" and r.ai_capped is False
    assert r.trace.of_kind("stop")[0]["ai_stop_reason"] == "input_too_large"
    assert r.trace.of_kind("ai_stopped")[0]["reason"] == "input_too_large"


def test_a_file_ceiling_is_ai_capped_and_names_its_reason() -> None:
    ai = proposer(FakeModel([]), usd_ceiling=0.0001)
    r = run([ai], World())
    assert r.ai_stop_reason == "file_ceiling" and r.ai_capped is True


def test_a_proposer_is_a_model_generator_to_the_budget() -> None:
    # AIProposer has no model_usage(); the driver must still count its turns and dollars and cap it
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})) for _ in range(20)])
    ai = proposer(model)
    r = run([ai], World(), budget=Budget(wall_s=1000, ops=1000, model_calls=1, model_usd=1000), patience=50)
    assert r.ai_capped is True and r.ai_stop_reason == "model_budget"
    assert len(model.calls) == 1


# --- item 4: run_stage ------------------------------------------------------------------------------------------


@pytest.fixture
def stage_world(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.setattr(stages.ops, "validate_call", lambda *a, **k: [])
    calls: list[Any] = []

    def judge(pool: Any, ws: Workspace) -> tuple:
        calls.append(ws.brep_path)
        return (1, _q(ws), 0.3, 0.0, 0)

    monkeypatch.setattr(stages, "_judge", judge)
    return calls


def test_run_stage_signature_is_the_one_the_driver_calls() -> None:
    assert list(inspect.signature(stages.run_stage).parameters) == ["pool", "ws", "name", "params"]


def test_a_run_stage_candidate_is_routed_to_stages_run_stage(stage_world: list[Any]) -> None:
    start = make_ws(0, ids=("F1", "F2", "B1"))
    model = FakeModel([reply(call_of("propose", {"candidates": [{"run_stage": "finalise", "params": {}}]})), reply()])
    r = run([proposer(model)], World(), start=start)
    assert r.changed and _q(r.best) == 1
    [ev] = [e for e in r.trace.of_kind("candidate") if e["outcome"] == "evaluated"]
    assert ev["op"] == "run_stage" and ev["params"] == {"stage": "finalise", "params": {}}
    prev = json.loads(user_turns(model.calls[1])[-1].split("\n", 1)[1])["previous_candidates"][0]
    assert prev["status"] == "applied" and prev["diff"]["op"] == "run_stage"


def test_a_run_stage_that_changes_nothing_or_names_no_stage_is_a_rejection_not_a_crash(
        stage_world: list[Any]) -> None:
    start = make_ws(0, ids=("F1", "F2"))          # no solid: finalise has nothing to do
    got: list[Any] = []

    class Fb(Scripted):
        def feedback(self, outcomes: Any) -> None:
            got.extend(outcomes)

    g = Fb(lambda s: [Candidate("run_stage", [], {"stage": "finalise", "params": {}}, "ai"),
                      Candidate("run_stage", [], {"stage": "nonsense", "params": {}}, "ai"),
                      Candidate("run_stage", ["F1"], {"stage": "finalise", "params": {}}, "ai")]
           if s.depth == 0 else [])
    r = run([g], World(), start=start)
    assert not r.changed
    by_status = {o.status: [x.message for x in got if x.status == o.status] for o in got}
    assert sorted(o.status for o in got) == ["declined", "invalid", "invalid"]
    assert any("unknown stage" in m for m in by_status["invalid"]) and any("targets" in m for m in by_status["invalid"])
    assert "changed nothing" in by_status["declined"][0] or "no_change" in by_status["declined"][0]


# --- item 5: one SiteRegistry per linear lineage ----------------------------------------------------------------


def _live_site(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
    """Anchored on the lowest live face, as find_sites does without a registry."""
    faces = sorted((i for i in ws.ids if i[0] == "F"), key=lambda s: int(s[1:]))
    return [site(f"S@{faces[0]}", "proximity", faces)]


def test_a_real_registry_refuses_a_fork_which_is_why_the_driver_keeps_one_per_state() -> None:
    root = make_ws(0)
    a, b = T18Op().apply(None, root, [], {"q": 1, "to": "F5"}, timeout_s=1)[0], \
        T18Op().apply(None, root, [], {"q": 2, "to": "F6"}, timeout_s=1)[0]
    reg = SiteRegistry()
    reg.apply(root, _live_site(None, root, None))
    reg.apply(a, _live_site(None, a, None))
    with pytest.raises(LineageMismatch):
        reg.apply(b, _live_site(None, b, None))


def test_site_ids_survive_the_op_that_rebuilds_their_anchor_on_every_branch_of_a_forking_search() -> None:
    w = World(sites_fn=_live_site)
    g = Scripted(lambda s: {0: [cand(1, to="F5"), cand(2, to="F6")], 1: [cand(3, to="F7")]}.get(s.depth, [])
                 if s.depth < 2 else [])
    r = run([g], w, patience=50)
    assert not r.trace.of_kind("judge_error")        # no LineageMismatch, however the search forked
    by_depth: dict[int, list[list[str]]] = {}
    for s in g.states:
        by_depth.setdefault(s.depth, []).append([x.id for x in s.sites])
    assert by_depth[0] == [["S@F1"]]
    # two sibling branches and a grandchild: each carries the ORIGINAL name although F1 no longer lives
    assert len(by_depth[1]) == 2 and all(ids == ["S@F1"] for ids in by_depth[1])
    assert by_depth[2] and all(ids == ["S@F1"] for ids in by_depth[2])
    assert all("F1" not in s.ws.ids for s in g.states if s.depth > 0)


def test_a_lineage_error_is_recorded_and_the_state_is_still_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    real = SiteRegistry.apply
    boom = {"n": 0}

    def apply(self: SiteRegistry, ws: Workspace, sites: list[Site]) -> None:
        boom["n"] += 1
        if boom["n"] == 2:
            raise LineageMismatch("simulated")
        real(self, ws, sites)

    monkeypatch.setattr(SiteRegistry, "apply", apply)
    g = Scripted(lambda s: [cand(1, to="F5")] if s.depth == 0 else [])
    r = run([g], World(sites_fn=_live_site))
    errs = r.trace.of_kind("judge_error")
    assert [e["where"] for e in errs] == ["site_registry"]
    assert [len(s.sites) for s in g.states] == [1, 1]


# --- item 6: render.view(pool, ws, site) --------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 8


class PngPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload))
        return CallResult(True, {"png_b64": base64.b64encode(PNG).decode()}, None, "ok", 0.0)


def _open_site(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
    return [site("S@F1", "open", ["F1"]), site("S@F2", "proximity", ["F2"])]


def test_the_driver_renders_image_kind_sites_for_a_model_generator_once_through_the_real_render_view() -> None:
    pool = PngPool()
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply()])
    ai = proposer(model)
    seen: list[Site] = []
    run([ai, Scripted(lambda s: seen.extend(s.sites) or [])], World(sites_fn=_open_site), pool=pool, patience=50)
    renders = [p for fn, p in pool.calls if fn == "render_view"]
    assert len(renders) == 1 and renders[0]["brep_path"].endswith(".brep")   # open only, and only once
    assert renders[0]["entities"] == [{"id": "F1", "type": "face", "index": 1}]
    assert any(s.view_png == PNG for s in seen)
    assert "image_url" in json.dumps(model.calls[0], default=str)


def test_no_model_generator_means_no_render() -> None:
    pool = PngPool()
    run([Scripted(lambda s: [])], World(sites_fn=_open_site), pool=pool)
    assert pool.calls == []


# --- the unguarded score calls ------------------------------------------------------------------------------------


def test_an_inconclusive_child_verdict_does_not_abort_the_branch_and_is_retried_once() -> None:
    got: list[Outcome] = []

    class Fb(Scripted):
        def feedback(self, outcomes: Any) -> None:
            got.extend(outcomes)

    g = Fb(lambda s: [cand(1, inc=1), cand(2, inc=99), cand(3)] if s.depth == 0 else [])
    r = run([g], World(), patience=50)
    by_q = {o.candidate.params["q"]: o for o in got}
    assert by_q[1].status == "applied"                    # inconclusive once, then judged
    assert by_q[2].status == "failed" and "inconclusive" in by_q[2].message
    assert by_q[3].status == "applied"
    assert _q(r.best) == 3


def test_an_inconclusive_duplicate_does_not_abort_the_branch() -> None:
    g = Scripted(lambda s: [cand(1, dup=True, inc=99), cand(2)] if s.depth == 0 else [])
    r = run([g], World(), start=make_ws(0, fp="same"), patience=50)
    assert _q(r.best) == 2
    assert not r.trace.of_kind("judge_error")


def test_only_confirmed_states_can_be_best_or_delivered() -> None:
    class W(World):
        def full(self, pool: Any, ws: Workspace, original: str) -> Verdict:
            if _q(ws) == 0:
                return super().full(pool, ws, original)
            raise InconclusiveVerdict("pool died")        # confirmation of any child never succeeds

    g = Scripted(lambda s: [cand(5)] if s.depth == 0 else [])
    r = run([g], W(), patience=50)
    assert not r.changed and _q(r.best) == 0
    assert r.trace.of_kind("confirm_error")


# --- item 7: default_judge against the REAL modules ----------------------------------------------------------------


class RealishPool:
    """The kernel boundary only: indexing, STEP export and layer 3. Everything above it is the real code."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload))
        if fn == "ws_index":
            ents = [{"type": "face", "index": k, "fp": f"f{k}"} for k in range(1, 4)]
            return CallResult(True, {"entities": ents, "quantum": 1e-6}, None, "ok", 0.0)
        if fn == "write_step_xcaf":
            return CallResult(True, {}, None, "ok", 0.0)
        if fn == "faithful_check":
            v = {"ok": True, "max_dev_mm": 0.0, "bound_mm": 0.1, "area_ratio": 1.0, "volume_ratio": 1.0,
                 "bodies_before": 1, "bodies_after": 1, "reasons": []}
            return CallResult(True, v, None, "ok", 0.0)
        raise AssertionError(f"unexpected kernel call {fn}")


@pytest.fixture
def real_checker(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    """The real `cheap_verdict`/`verdict`/`score`; only layer 1 and the panel (which need a kernel and a mesher)
    are replaced, exactly as Task 5's own tests do."""
    seen = SimpleNamespace(valid_steps=[], panel_steps=[], faithful_steps=[])

    def check_valid(pool: Any, step: str, *, original_units: Any) -> ValidReport:
        seen.valid_steps.append(step)
        return ValidReport(True, [], [], "mm", 1, True)

    def check_faithful_steps(pool: Any, original: str, step: str, *, declared_added: list, declared_removed: list):
        seen.faithful_steps.append((original, step, declared_added, declared_removed))
        return FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1, declared_added, declared_removed)

    def run_panel(pool: Any, step: str, members: Any, *, timeout_s: float) -> list:
        seen.panel_steps.append(step)
        return [Member("m0")]

    monkeypatch.setattr(assemble, "check_valid", check_valid)
    monkeypatch.setattr(assemble, "check_faithful_steps", check_faithful_steps)
    monkeypatch.setattr(assemble, "default_members", lambda tier: [SimpleNamespace(name="m0")])
    monkeypatch.setattr(assemble, "_panel_module", lambda: SimpleNamespace(run_panel=run_panel))
    for name in ("orig.brep", "w.brep"):
        (tmp_path / name).write_bytes(name.encode())
    seen.orig = str(tmp_path / "orig.brep")
    seen.brep = str(tmp_path / "w.brep")
    return seen


def test_default_judge_calls_the_real_checker_with_steps_and_translated_face_indices(
        real_checker: SimpleNamespace) -> None:
    diff = Diff(op="fill_loop", params={}, modified={}, generated={"E1": ["F9"]}, removed=[],
                declared_added=["F9"], declared_removed=["F2"], seconds=0.0)
    ws = Workspace(brep_path=real_checker.brep, ids={"F1": "a", "F9": "b"}, labels={}, history=[diff],
                   original=real_checker.orig, indices={"F1": 1, "F9": 2})
    pool = RealishPool()
    j = default_judge(h_mm=2.5)
    v = j.cheap(pool, ws, "orig.step")
    assert isinstance(v, Verdict) and v.tier == "cheap" and j.score(v)[0] == 1
    # layer 1 and the panel read a STEP file the driver exported, never the BREP path
    assert real_checker.valid_steps and not real_checker.valid_steps[0].endswith(".brep")
    assert [c for c in pool.calls if c[0] == "write_step_xcaf"][0][1]["brep"] == real_checker.brep
    # layer 3 was re-run on the BREPs with FACE INDICES (F9 is the second live face -> F2), not stable ids;
    # F2 (removed) exists in the original and keeps its index
    [(fn, payload)] = [c for c in pool.calls if c[0] == "faithful_check"]
    assert payload["original_brep"] == real_checker.orig and payload["repaired_brep"] == real_checker.brep
    assert payload["declared_added"] == ["F2"] and payload["declared_removed"] == ["F2"]
    assert v.faithful is not None and v.faithful.declared_added == ["F2"]


def test_default_judge_full_verdict_is_clean_only_on_the_re_run_layer_3(real_checker: SimpleNamespace,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    diff = Diff(op="fill_loop", params={}, modified={}, generated={}, removed=[], declared_added=["F1"],
                declared_removed=[], seconds=0.0)
    ws = Workspace(brep_path=real_checker.brep, ids={"F1": "a"}, labels={}, history=[diff],
                   original=real_checker.orig, indices={"F1": 1})
    j = default_judge(h_mm=2.5)
    assert j.full(RealishPool(), ws, "orig.step").clean is True

    class Unfaithful(RealishPool):
        def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
            if fn == "faithful_check":
                return CallResult(True, {"ok": False, "max_dev_mm": 9.0, "bound_mm": 0.1, "area_ratio": 1.0,
                                         "volume_ratio": 1.0, "bodies_before": 1, "bodies_after": 1,
                                         "reasons": ["deviation_exceeds_bound"]}, None, "ok", 0.0)
            return super().call(fn, payload, timeout_s=timeout_s)

    v = j.full(Unfaithful(), ws, "orig.step")
    assert v.clean is False and j.score(v)[0] == 0   # layer 3 on the exact indices decides `clean`, not the STEP pass


def test_the_clean_rule_default_judge_recomputes_agrees_with_the_real_assembly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    knobs = {"valid": True, "faithful": True, "panel": "pass"}
    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: ValidReport(
        bool(knobs["valid"]), [], [], "mm", 1, True))
    monkeypatch.setattr(assemble, "check_faithful_steps", lambda pool, o, s, *, declared_added, declared_removed:
                        FaithfulReport(bool(knobs["faithful"]), 0.0, 0.1, 1.0, 1.0, 1, 1))
    monkeypatch.setattr(assemble, "default_members", lambda tier: [SimpleNamespace(name="m0")])
    monkeypatch.setattr(assemble, "_panel_module", lambda: SimpleNamespace(
        run_panel=lambda pool, step, members, *, timeout_s: [Member("m0", str(knobs["panel"]))]))
    for panel, valid_ok, faithful_ok in itertools.product(("pass", "fail"), (True, False), (True, False)):
        knobs.update(panel=panel, valid=valid_ok, faithful=faithful_ok)
        real = assemble.verdict(None, "s.step", "o.step")  # type: ignore[arg-type]
        # the re-run layer 3 replaces the STEP one: flip it and the recomputed `clean` must follow the real rule
        for report_ok in (True, False):
            report = FaithfulReport(report_ok, 0.0, 0.1, 1.0, 1.0, 1, 1)
            knobs["faithful"] = report_ok
            assert search._clean_with(real, report) is assemble.verdict(None, "s.step", "o.step").clean  # type: ignore[arg-type]


def test_default_judge_sites_and_checker_signatures_are_the_real_ones() -> None:
    from meshpipeline.cad.repair_v4.sites import find_sites

    params = inspect.signature(find_sites).parameters
    assert list(params)[:3] == ["pool", "ws", "verdict"] and params["h_mm"].kind is inspect.Parameter.KEYWORD_ONLY
    for fn in (assemble.cheap_verdict, assemble.verdict):
        assert list(inspect.signature(fn).parameters)[:3] == ["pool", "step", "original"]
    assert list(inspect.signature(render.view).parameters)[:3] == ["pool", "ws", "site"]


def test_search_with_the_real_default_judge_runs_through_the_real_modules(real_checker: SimpleNamespace) -> None:
    ws = Workspace(brep_path=real_checker.brep, ids={"F1": "a"}, labels={}, history=[],
                   original=real_checker.orig, indices={"F1": 1})
    # a one-member full panel passes: the start is clean, so no expansion is needed
    r = search.search(RealishPool(), ws, "orig.step", [], BIG, workers=1, h_mm=2.5)
    assert r.clean is True and r.trace.of_kind("stop")[0]["reason"] == "clean"
