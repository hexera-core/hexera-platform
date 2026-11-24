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
    # pins WHICH tier the delta was taken on (World gives the same q on both, so the numbers cannot): the child
    # was confirmed with the full panel before the feedback was sent
    assert "full panel against the parent's full score" in prev["message"]


def test_an_unconfirmed_child_is_reported_on_the_cheap_tier() -> None:
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(2)]})), reply()])
    run([proposer(model)], World(), k_confirm=0)
    body = json.loads(user_turns(model.calls[1])[-1].split("\n", 1)[1])
    assert "cheap tier" in body["previous_candidates"][0]["message"]


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

    def _sweep(self, site: Site, diag: float | None) -> list:
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


def test_input_too_large_and_model_unavailable_are_recorded_and_are_not_ai_capped() -> None:
    ai = proposer(FakeModel([]), max_input_tokens=1)
    r = run([ai], World())
    assert r.ai_stop_reason == "input_too_large" and r.ai_capped is False
    assert r.trace.of_kind("stop")[0]["ai_stop_reason"] == "input_too_large"
    assert r.trace.of_kind("ai_stopped")[0]["reason"] == "input_too_large"


def test_a_file_ceiling_is_ai_capped_and_names_its_reason() -> None:
    ai = proposer(FakeModel([]), usd_ceiling=0.0001)
    r = run([ai], World())
    assert r.ai_stop_reason == "no_affordable_turn" and r.ai_capped is True


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
    assert 2 not in by_q                                  # still inconclusive: infrastructure, so NOT told as a failure
    assert [e["outcome"] for e in r.trace.of_kind("candidate") if e["params"]["q"] == 2] == ["inconclusive"]
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
            # the real export writes the file, and the verdict cache keys on its bytes: a fake that writes nothing
            # is a pool that lies about its own contract
            with open(payload["path"], "wb") as handle:
                handle.write(b"STEP of " + payload["brep"].encode())
            return CallResult(True, {}, None, "ok", 0.0)
        if fn == "mesher_versions":
            return CallResult(True, {"gmsh": "4.15.2", "netgen": "6.2", "ocp": "7.8"}, None, "ok", 0.0)
        if fn == "valid_extent":  # D10: the input's extent is asked first; this part is bounded
            return CallResult(True, {"unbounded": False}, None, "ok", 0.0)
        if fn == "faithful_check":
            v = {"ok": True, "max_dev_mm": 0.0, "bound_mm": 0.1, "area_ratio": 1.0, "volume_ratio": 1.0,
                 "bodies_before": 1, "bodies_after": 1, "reasons": []}
            return CallResult(True, v, None, "ok", 0.0)
        raise AssertionError(f"unexpected kernel call {fn}")


def fake_panel_module(run_panel: Any) -> SimpleNamespace:
    """A stand-in for `verdict.panel` built FROM the real module: every public non-callable name (the
    contract constants, e.g. `INFRASTRUCTURE_KIND`) is copied with its real value, so a constant added to
    `panel` is on the fake without anyone remembering to. Callables are never copied (a real `run_panel`
    would mesh); the one the test supplies is the only behaviour. Completeness against what `assemble`
    reads is pinned by `test_the_fake_panel_module_covers_everything_assemble_reads`."""
    from meshpipeline.cad.repair_v4.verdict import panel as real_panel

    constants = {n: v for n, v in vars(real_panel).items()
                 if not n.startswith("_") and not callable(v) and not inspect.ismodule(v)}
    return SimpleNamespace(**constants, run_panel=run_panel)


def _panel_attributes_read_by_assemble() -> set[str]:
    """Every `X` in `_panel_module().X` in assemble.py, also through a local alias (`p = _panel_module()`)."""
    import ast

    tree = ast.parse(inspect.getsource(assemble))

    def is_call(n: ast.AST) -> bool:
        return isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_panel_module"

    aliases = {t.id for a in ast.walk(tree) if isinstance(a, ast.Assign) and is_call(a.value)
               for t in a.targets if isinstance(t, ast.Name)}
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and (is_call(n.value) or (isinstance(n.value, ast.Name) and n.value.id in aliases))}


def test_the_fake_panel_module_covers_everything_assemble_reads() -> None:
    read = _panel_attributes_read_by_assemble()
    assert {"INFRASTRUCTURE_KIND", "run_panel"} <= read, f"the scan found too little: {sorted(read)}"
    fake = fake_panel_module(lambda *a, **k: [])
    missing = sorted(a for a in read if not hasattr(fake, a))
    assert not missing, (f"fake_panel_module lacks {missing}, which assemble.py reads off the panel module; "
                         "add them to the helper (constants are copied from the real module automatically)")


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
    monkeypatch.setattr(assemble, "default_members", lambda tier: [SimpleNamespace(name="m0", config={})])
    monkeypatch.setattr(assemble, "_panel_module", lambda: fake_panel_module(run_panel))
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
    # layer 3 on the exact indices is what `clean` reads (that rule is not B12's); the SCORE's first key is validity
    # alone since B12, so the unfaithful state is still valid and ranks on its meshing
    assert v.clean is False and j.score(v)[0] == 1


def test_the_clean_rule_default_judge_recomputes_agrees_with_the_real_assembly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    knobs = {"valid": True, "faithful": True, "panel": "pass", "inconclusive": False}
    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: ValidReport(
        bool(knobs["valid"]), [], [], "mm", 1, True))
    monkeypatch.setattr(assemble, "check_faithful_steps", lambda pool, o, s, *, declared_added, declared_removed:
                        FaithfulReport(bool(knobs["faithful"]), 0.0, 0.1, 1.0, 1.0, 1, 1))
    monkeypatch.setattr(assemble, "default_members", lambda tier: [SimpleNamespace(name="m0")])
    monkeypatch.setattr(assemble, "_panel_module", lambda: fake_panel_module(
        lambda pool, step, members, *, timeout_s: [Member("m0", str(knobs["panel"]))]
        + ([Member("pool", "timeout", None, "infrastructure")] if knobs["inconclusive"] else [])))
    for panel, valid_ok, faithful_ok, inc in itertools.product(("pass", "fail"), (True, False), (True, False),
                                                                (False, True)):
        knobs.update(panel=panel, valid=valid_ok, faithful=faithful_ok, inconclusive=inc)
        real = assemble.verdict(None, "s.step", "o.step")  # type: ignore[arg-type]
        assert real.inconclusive is inc
        cheap = assemble.cheap_verdict(None, "s.step", "o.step")  # type: ignore[arg-type]
        assert search._clean_with(cheap, cheap.faithful) is cheap.clean is False       # the tier term
        # the re-run layer 3 replaces the STEP one: flip it and the recomputed `clean` must follow the real rule
        for report_ok in (True, False):
            report = FaithfulReport(report_ok, 0.0, 0.1, 1.0, 1.0, 1, 1)
            knobs["faithful"] = report_ok
            assert search._clean_with(real, report) is assemble.verdict(None, "s.step", "o.step").clean  # type: ignore[arg-type]


def test_clean_of_pins_every_term_of_the_rule_including_inconclusive() -> None:
    ok = {"tier": "full", "inconclusive": False, "valid": ValidReport(True, [], [], "mm", 1, True),
          "panel": [Member("m0")], "faithful": FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1)}
    assert assemble.clean_of(**ok) is True                                              # type: ignore[arg-type]
    for term, bad in (("tier", "cheap"), ("inconclusive", True), ("panel", []), ("faithful", None),
                      ("panel", [Member("m0", "fail")]), ("valid", ValidReport(False, [], [], "mm", 1, True))):
        assert assemble.clean_of(**{**ok, term: bad}) is False, term                    # type: ignore[arg-type]


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


# === fix round: per-lineage declarations, infrastructure is not a verdict, the dollar cap, dispatch ==============


class RecordingDet(DetGenerator):
    """Records, per expanded state, which site ids survive the shared registry for THAT state's lineage."""

    def __init__(self, unfixable: UnfixableRegistry) -> None:
        super().__init__(unfixable)
        self.visible: dict[str, list[str]] = {}

    def propose(self, state: search.SearchState, k: int) -> list[Candidate]:
        assert self.unfixable is not None
        self.visible[state.id] = [s.id for s in self.unfixable.filter(state.sites, state.lineage)]
        return []


def _same_id_site(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
    return [site("S@F1", "proximity", ["F1", "F2"])]


def test_an_unfixable_declaration_on_one_sibling_does_not_suppress_the_other_siblings_same_id_site() -> None:
    """Site ids are minted per lineage, so two siblings reuse one id for different geometry (S@F1 here, on every
    state). The model declares S@F1 unfixable on the branch it expands first; the sibling's S@F1 must still be
    swept and shown."""
    shared = UnfixableRegistry()
    det = RecordingDet(shared)
    model = FakeModel([
        reply(call_of("propose", {"candidates": [ai_cand(1), ai_cand(2)]})), reply(),                # at the start
        reply(call_of("declare_unfixable", {"site_id": "S@F1", "reason": "breaches the fidelity bound"})),
        reply(),                                                                                     # first sibling
    ])
    run([det, proposer(model, unfixable=shared)], World(sites_fn=_same_id_site), patience=50)
    [(declaring, site_id)] = list(shared.reasons)
    assert site_id == "S@F1" and declaring != ""
    siblings = [sid for sid, ids in det.visible.items() if sid != "S0" and sid != declaring]
    assert siblings, "the sibling was never expanded: the test proves nothing"
    assert all(det.visible[sid] == ["S@F1"] for sid in siblings)         # its own same-id site was swept
    # ...and shown to the model: some call after the declaring one was made on the sibling, which names S@F1
    assert any("S@F1" in user_turns(c)[-1] for c in model.calls[3:])
    assert det.visible[declaring] == ["S@F1"]                             # swept before the model declared, this round


def test_an_unfixable_declaration_is_seen_by_descendants_only() -> None:
    reg = UnfixableRegistry()
    reg.declare("S1", "too big", ("S0", "S2"))
    s1 = site("S1", "open", ["F1"])
    assert reg.filter([s1], ("S0", "S2")) == []                    # the declaring state
    assert reg.filter([s1], ("S0", "S2", "S5")) == []              # its descendant
    assert reg.filter([s1], ("S0", "S3")) == [s1]                  # a sibling
    assert reg.filter([s1], ("S0",)) == [s1]                       # the parent
    assert reg.filter([s1]) == [s1]                                # no lineage


def test_the_search_gives_every_state_its_lineage() -> None:
    g = Scripted(lambda s: [cand(1)] if s.depth == 0 else [cand(2)] if s.depth == 1 else [])
    run([g], World(), patience=50)
    by_depth = {s.depth: s.lineage for s in g.states}
    assert by_depth[0] == (g.states[0].id,)
    assert by_depth[1] == (g.states[0].id, g.states[1].id) and by_depth[2][:2] == by_depth[1]


def _open_by_depth(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
    return [site("S@F1", "open", ["F1"])] if ws.history else []


def test_the_picture_of_a_same_id_site_is_drawn_and_shown_on_each_sibling_branch() -> None:
    pool = PngPool()
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1), ai_cand(2)]})), reply()])
    run([proposer(model)], World(sites_fn=_open_by_depth), pool=pool, patience=50)
    renders = [p["brep_path"] for fn, p in pool.calls if fn == "render_view"]
    assert len(renders) == 2 and len(set(renders)) == 2          # two siblings, one id, two different workspaces
    assert json.dumps(model.calls[-1], default=str).count('"type": "image_url"') == 2   # the proposer showed both


def test_a_descendant_inherits_what_its_ancestor_already_drew() -> None:
    pool = PngPool()
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply(),
                       reply(call_of("propose", {"candidates": [ai_cand(2)]})), reply()])
    run([proposer(model)], World(sites_fn=_open_by_depth), pool=pool, patience=50)
    assert len([1 for fn, _ in pool.calls if fn == "render_view"]) == 1


# --- CheckerUnavailable is infrastructure -------------------------------------------------------------------------


class FailingOp:
    name = "t18_fail"
    params_schema = {"type": "object", "x-targets": {"kind": "face", "min": 0}}

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float) -> Any:
        from meshpipeline.cad.repair_v4.ops import OpFailed

        raise OpFailed("error", "the kernel said no")


class DeadChecker(World):
    """The checker (not the part) is dead for workspaces with quality `dead_q`, for `times` attempts (None = always)."""

    def __init__(self, dead_q: int, times: int | None = None) -> None:
        super().__init__()
        self.dead_q, self.left = dead_q, times

    def cheap(self, pool: Any, ws: Workspace, original: str) -> Verdict:
        from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

        if _q(ws) == self.dead_q and (self.left is None or self.left > 0):
            if self.left is not None:
                self.left -= 1
            raise CheckerUnavailable("write_step_xcaf: crashed: worker killed")
        return super().cheap(pool, ws, original)


class Collect(Scripted):
    def __init__(self, script: Any) -> None:
        super().__init__(script)
        self.got: list[Outcome] = []

    def feedback(self, outcomes: Any) -> None:
        self.got.extend(outcomes)


def test_a_killed_checker_is_inconclusive_and_is_not_told_to_the_proposer_as_a_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "t18_fail", FailingOp())  # type: ignore[arg-type]
    g = Collect(lambda s: [cand(5), Candidate("t18_fail", [], {}, "det"), cand(1)] if s.depth == 0 else [])
    r = run([g], DeadChecker(dead_q=5), patience=50)
    by_op_q = {(o.candidate.op, o.candidate.params.get("q")): o for o in g.got}
    assert (("t18_set", 5)) not in by_op_q                          # the dead checker produced NO feedback at all
    assert by_op_q[("t18_set", 1)].status == "applied"
    # a GENUINE candidate failure still reads as one, and still reaches the proposer
    assert by_op_q[("t18_fail", None)].status == "failed" and "the kernel said no" in by_op_q[("t18_fail", None)].message
    outcomes = {e["params"].get("q"): e["outcome"] for e in r.trace.of_kind("candidate") if e["op"] == "t18_set"}
    assert outcomes[5] == "inconclusive" and outcomes[1] == "evaluated"
    assert not [e for e in r.trace.of_kind("candidate") if e["outcome"] == "rejected" and e.get("params", {}).get("q") == 5]
    assert _q(r.best) == 1                                          # and the search went on


def test_a_checker_that_comes_back_on_the_retry_judges_the_candidate() -> None:
    g = Collect(lambda s: [cand(5)] if s.depth == 0 else [])
    run([g], DeadChecker(dead_q=5, times=1), patience=50)
    assert [o.status for o in g.got] == ["applied"]


def test_an_inconclusive_verdict_does_not_count_on_the_scoreboard() -> None:
    assert make_verdict(2, "full", 99, inconclusive=True).counts_for_scoreboard is False
    assert make_verdict(2, "full", 99).counts_for_scoreboard is True


@pytest.mark.parametrize("dies", ["step_for", "original_facts", "check_faithful"])
def test_every_checker_call_on_the_driver_path_is_infrastructure_not_a_candidate_failure(
        dies: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    start = make_ws(0)
    armed = {"on": False}

    def maybe(name: str, ok: Any) -> Any:
        def fn(*a: Any, **k: Any) -> Any:
            if armed["on"] and dies == name:
                raise CheckerUnavailable(f"{name}: crashed")
            return ok(*a, **k) if callable(ok) else ok
        return fn

    monkeypatch.setattr(search, "step_for", maybe("step_for", lambda pool, brep: brep))
    monkeypatch.setattr(search, "original_facts", maybe("original_facts", (3, 1)))
    monkeypatch.setattr(search, "declared_edits", lambda ws, faces: SimpleNamespace(added=["F1"], removed=[]))
    monkeypatch.setattr(search, "check_faithful", maybe("check_faithful", lambda *a, **k: FaithfulReport(
        True, 0.0, 0.1, 1.0, 1.0, 1, 1)))

    def fn(pool: Any, step: str, original: str) -> Verdict:
        return make_verdict(1, "cheap", 99)

    w = World()
    judge = Judge(cheap=lambda pool, ws, o: search._judged(fn, pool, ws, o),
                  full=lambda pool, ws, o: make_verdict(_q(ws), "full", 99), score=score, sites=w.one_site)

    class Arm(Collect):
        def propose(self, state: search.SearchState, k: int) -> list[Candidate]:
            armed["on"] = state.depth == 0
            return super().propose(state, k)

    g = Arm(lambda s: [cand(1)] if s.depth == 0 else [])
    r = search.search(None, start, "/fake/orig.step", [g], BIG, workers=1, judge=judge, patience=50)
    assert g.got == []                                                            # not told it failed
    assert [e["outcome"] for e in r.trace.of_kind("candidate")] == ["inconclusive"]
    assert not r.trace.of_kind("generator_error")


# --- the dollar cap and its ceiling -------------------------------------------------------------------------------


def test_the_usd_cap_binds_on_its_own() -> None:
    # model_calls is huge, so ONLY the dollar term can stop the AI; one FakeModel call costs 0.01
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})) for _ in range(40)])
    ai = proposer(model)
    r = run([ai], World(), budget=Budget(wall_s=1000, ops=1000, model_calls=10_000, model_usd=0.011), patience=50)
    assert r.ai_capped is True and r.ai_stop_reason == "model_budget"
    assert r.trace.of_kind("ai_capped")
    spent = ai.result()["spent_usd"]
    # model_usd is an advisory trigger checked once per expansion: the first expansion's propose may make up to
    # max_turns calls before it is looked at. Pin the exact count, so eight calls (the unbounded case) cannot pass.
    assert len(model.calls) == ai.result()["turns"] == 2
    assert spent == pytest.approx(0.01 * len(model.calls), rel=0.5) and spent < 0.05


def test_without_the_usd_cap_the_same_run_is_not_capped() -> None:
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})) for _ in range(6)])
    r = run([proposer(model)], World(), budget=Budget(wall_s=1000, ops=1000, model_calls=10_000, model_usd=1000),
            patience=2)
    assert r.ai_stop_reason != "model_budget"


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1.0])
def test_a_non_finite_or_negative_model_usd_is_refused_by_the_driver(bad: float) -> None:
    with pytest.raises(ValueError, match="model_usd"):
        run([Scripted(lambda s: [])], World(), budget=Budget(wall_s=1000, ops=1000, model_calls=1, model_usd=bad))


@pytest.mark.parametrize("bad", [0.0, -0.5, float("inf"), float("nan"), 1.01, 50.0])
def test_a_usd_ceiling_over_the_per_file_dollar_or_not_finite_is_refused_at_construction(bad: float) -> None:
    with pytest.raises(ValueError, match="usd_ceiling"):
        search.ai_generators(ROUTE, ledger=WaveLedger.for_test(cap_usd=10.0), max_turns=3, usd_ceiling=bad,
                             file_id="f", job_id="j", call=FakeModel([]), prices=ASTRA)


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 0.0, -1.0, 1.01, 50.0])
def test_the_proposer_itself_refuses_a_non_finite_or_over_one_dollar_ceiling(bad: float) -> None:
    with pytest.raises(ValueError, match="usd_ceiling"):
        proposer(FakeModel([]), usd_ceiling=bad)


@pytest.mark.parametrize("ok", [1.0, 0.5, 0.0001])
def test_a_ceiling_up_to_one_dollar_is_accepted(ok: float) -> None:
    gens, _ = search.ai_generators(ROUTE, ledger=WaveLedger.for_test(cap_usd=10.0), max_turns=3, usd_ceiling=ok,
                                   file_id="f", job_id="j", call=FakeModel([]), prices=ASTRA)
    assert len(gens) == 2


# --- engine dispatch: the arm selects the generators --------------------------------------------------------------


def test_the_search_ai_arm_reaches_the_ai_proposer_and_the_search_det_arm_cannot() -> None:
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(3)]})), reply()])
    gens, ai = search.arm_generators(
        "search_ai", route=ROUTE, ledger=WaveLedger.for_test(cap_usd=10.0), max_turns=3, usd_ceiling=1.0,
        file_id="f", job_id="j", call=model, prices=ASTRA)
    assert isinstance(ai, AIProposer) and ai in gens
    r = run(gens, World(), patience=50)
    assert len(model.calls) >= 1 and _q(r.best) == 3 and r.ai[0]["spent_usd"] > 0

    det_gens, none = search.arm_generators("search_det")
    assert none is None and not any(isinstance(g, AIProposer) for g in det_gens)
    assert all(isinstance(g, DetGenerator) for g in det_gens)


def test_an_unknown_arm_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown search arm"):
        search.arm_generators("search_x")


# --- a search that measured nothing is not a negative result -------------------------------------------------------


def test_an_all_inconclusive_search_is_not_a_patience_exit() -> None:
    g = Scripted(lambda s: [cand(1, inc=99), cand(2, inc=99)] if s.depth == 0 else [])
    r = run([g], World(), patience=2)
    assert r.reason == "no_verdict" and r.infrastructure_failed is True
    assert (r.inconclusive_candidates, r.verdict_candidates) == (2, 0)
    assert r.inconclusive_rate == 1.0 and not r.changed and not r.clean
    stop = r.trace.of_kind("stop")[-1]
    assert stop["reason"] == "no_verdict" and stop["ended_by"] in ("patience", "exhausted")


def test_a_genuine_negative_search_still_reads_as_patience() -> None:
    """The same shape with real verdicts (judged, none better than the start): an ordinary failure, not infrastructure."""
    g = Scripted(lambda s: [cand(0, to="Z1"), cand(0, to="Z2")] if s.depth == 0 else [])
    r = run([g], World(), start=make_ws(1), patience=2)
    assert r.reason in ("patience", "exhausted") and r.infrastructure_failed is False
    assert r.verdict_candidates >= 1 and r.inconclusive_candidates == 0 and r.inconclusive_rate == 0.0


def test_a_mixed_search_reports_both_counts_and_is_not_infrastructure_failed() -> None:
    g = Scripted(lambda s: [cand(1), cand(2, inc=99), cand(3, inc=99)] if s.depth == 0 else [])
    r = run([g], World(), patience=50)
    assert r.inconclusive_candidates == 2 and r.verdict_candidates == 1
    assert r.infrastructure_failed is False and r.reason != "no_verdict"
    assert r.inconclusive_rate == pytest.approx(2 / 3) and r.changed


def test_a_stage_the_checker_could_not_judge_is_inconclusive_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "run_stage", lambda pool, ws, name, params: SimpleNamespace(
        inconclusive=[name], rolled_back={}, skipped={}, ws=ws, applied_ops={name: 2}))
    c = Candidate(op=search.RUN_STAGE, targets=[], params={"stage": "finalise"}, source="det")
    out = search._stage_outcome(None, make_ws(0), c, 0, 1.0)
    assert out.kind == "inconclusive"
    assert out.applied == 2  # an unjudged stage still ran its ops; they are still charged


# --- Task 22d: pictures reach the proposer, cost nothing when nobody looks, and are never a verdict ----------------


class NoPngPool:
    """A kernel that cannot draw: every render comes back not-ok (the shape of a timeout)."""

    def __init__(self, status: str = "timeout") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.status = status

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload))
        return CallResult(False, None, "render failed", self.status, 0.0)  # type: ignore[arg-type]


class RaisingPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload))
        raise RuntimeError("pool is closed")


def _open_by_depth_id(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
    e = f"F{len(ws.history) % 2 + 1}"           # alternate entities: the registry must not carry one id across
    return [site(f"S@{e}", "open", [e])]


def _two_round_model() -> FakeModel:
    return FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply(),
                      reply(call_of("propose", {"candidates": [ai_cand(2)]})), reply()])


def test_the_picture_reaches_the_proposers_message_payload_as_the_rendered_bytes() -> None:
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply()])
    run([proposer(model)], World(sites_fn=_open_site), pool=PngPool(), patience=50)
    first_user = model.calls[0][1]["content"]
    images = [p for p in first_user if p["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()


def _expanded_ids(r: SearchResult) -> list[list[str]]:
    return [[s["id"] for s in e["sites"]] for e in r.trace.of_kind("expand")]


def test_attaching_an_image_changes_no_site_id_in_any_round() -> None:
    with_png, without = PngPool(), NoPngPool()
    seen_a: list[Site] = []
    seen_b: list[Site] = []
    a = run([proposer(_two_round_model()), Scripted(lambda s: seen_a.extend(s.sites) or [])],
            World(sites_fn=_open_by_depth), pool=with_png, patience=50)
    b = run([proposer(_two_round_model()), Scripted(lambda s: seen_b.extend(s.sites) or [])],
            World(sites_fn=_open_by_depth), pool=without, patience=50)
    assert any(s.view_png == PNG for s in seen_a) and all(s.view_png is None for s in seen_b)
    assert _expanded_ids(a) == _expanded_ids(b) and len(_expanded_ids(a)) >= 2
    assert [s.id for s in seen_a] == [s.id for s in seen_b]


def test_view_png_is_not_part_of_the_registrys_identity() -> None:
    ws = make_ws(0)
    bare, imaged = site("S@F1", "open", ["F1"]), site("S@F1", "open", ["F1"])
    imaged.view_png = PNG
    ra, rb = SiteRegistry(), SiteRegistry()
    ra.apply(ws, [bare])
    rb.apply(ws, [imaged])
    child = Workspace(brep_path="/fake/c.brep", ids=ws.ids, labels=ws.labels, indices=ws.indices,
                      original=ws.original, history=[Diff(op="t18_set", params={}, modified={}, generated={}, removed=[], declared_added=[],
                                    declared_removed=[], seconds=0.0)])
    nxt_a, nxt_b = site("S@F1", "open", ["F1"]), site("S@F1", "open", ["F1"])
    nxt_b.view_png = PNG
    ra.apply(child, [nxt_a])
    rb.apply(child, [nxt_b])
    assert (bare.id, nxt_a.id) == (imaged.id, nxt_b.id) == ("S@F1", "S@F1")
    assert ra.retired == rb.retired and nxt_a.graph == nxt_b.graph


@pytest.mark.parametrize("pool_factory", [NoPngPool, lambda: NoPngPool("crashed"), RaisingPool])
def test_a_site_with_no_picture_is_still_proposed_and_nothing_reads_as_a_failure(pool_factory: Any) -> None:
    pool = pool_factory()
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply()])
    r = run([proposer(model)], World(sites_fn=_open_site), pool=pool, patience=50)
    assert [fn for fn, _ in pool.calls] == ["render_view"]
    assert "image_url" not in json.dumps(model.calls[0])           # no picture, and the proposer was still asked
    assert [e["outcome"] for e in r.trace.of_kind("candidate")] != [] and _q(r.best) == 1
    assert not r.trace.of_kind("judge_error")
    assert r.inconclusive_candidates == 0 and not r.infrastructure_failed


def test_a_render_that_raises_is_traced_as_view_unavailable_not_as_a_judge_error() -> None:
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply()])
    r = run([proposer(model)], World(sites_fn=_open_site), pool=RaisingPool(), patience=50)
    assert [e["site"] for e in r.trace.of_kind("view_unavailable")] == ["S@F1"]
    assert not r.trace.of_kind("judge_error")


def test_the_search_det_arm_renders_nothing_whatever_the_sites() -> None:
    gens, ai = search.arm_generators("search_det")
    assert ai is None
    pool = PngPool()
    run(gens, World(sites_fn=_open_site), pool=pool, patience=50)
    assert [fn for fn, _ in pool.calls if fn == "render_view"] == []


def test_a_proposer_that_has_stopped_is_not_shown_pictures_nobody_will_look_at() -> None:
    pool = PngPool()
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(1)]})), reply(), reply(), reply()])
    ai = proposer(model, max_turns=1)          # spends its only turn on the first state
    r = run([ai], World(sites_fn=_open_by_depth_id), pool=pool, patience=50)
    assert ai.turns_used == 1
    assert len(r.trace.of_kind("expand")) >= 2          # the search went on, deterministic-only
    assert len([1 for fn, _ in pool.calls if fn == "render_view"]) == 1


def _many_open(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
    return [site(f"S@F{n}", "open", [f"F{n}"]) for n in range(1, 8)]


def test_one_expansion_draws_at_most_the_cap_and_the_rest_wait_for_a_descendant() -> None:
    pool = PngPool()
    ids = tuple(f"F{n}" for n in range(1, 8))
    ai = proposer(FakeModel([reply()]))                      # proposes nothing: exactly one expansion
    run([ai], World(sites_fn=_many_open), start=make_ws(0, ids=ids), pool=pool, patience=50)
    drawn = [p["entities"][0]["id"] for fn, p in pool.calls if fn == "render_view"]
    assert search.MAX_VIEWS_PER_EXPANSION == 4
    assert drawn == ["F1", "F2", "F3", "F4"]                 # best-first, and no more


def test_search_gives_the_deterministic_sweep_the_parts_diagonal() -> None:
    """`search()` binds its pool's `ws_diagonal` to every DetGenerator, so the sweep sizes sew against the part."""
    calls: list[str] = []

    class Pool:
        def call(self, fn: str, payload: dict, timeout_s: float = 0.0) -> Any:
            calls.append(fn)

            class R:
                ok, status, error = True, "ok", None
                value = {"diag_mm": 173.0}

            return R()

    gen = DetGenerator()
    # the open site carries its free loop (4 mm of a 173 mm part, inside the cap), so nothing is withheld
    held = site("S1", "open", [])
    held.graph = {"free_loops": [{"edges": ["E1", "E2", "E3"], "span_mm": 4.0}], "diagonal_mm": 173.0}
    run([gen], World(sites_fn=lambda pool, ws, verdict: [held]), pool=Pool(), budget=Budget(
        wall_s=30.0, ops=2, model_calls=0, model_usd=0.0))
    assert "ws_diagonal" in calls
    assert gen.notes == [], gen.notes  # a diagonal was found: nothing was withheld


# --- Task 53: the op budget is charged per APPLIED op, for every arm --------------------------------------------


class ChainStep:
    """One op of a stage body: appends its own diff and makes the state one step better."""

    params_schema: dict = {"type": "object", "x-targets": {"kind": "face", "min": 0}}

    def __init__(self, name: str, applied: list[str]) -> None:
        self.name, self.applied = name, applied

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        self.applied.append(self.name)
        diff = Diff(op=self.name, params=params, modified={}, generated={}, removed=[], declared_added=[],
                    declared_removed=[], seconds=0.0)
        return make_ws(_q(ws) + 1, ids=tuple(ws.ids), history=[*ws.history, diff]), diff


@pytest.fixture
def sewing_ops(monkeypatch: pytest.MonkeyPatch, stage_world: list[Any]) -> list[str]:
    """`shells` on a part with no solid: sew THEN make_volume, two ops for one candidate."""
    applied: list[str] = []
    for name in ("sew", "make_volume"):
        monkeypatch.setitem(REGISTRY, name, ChainStep(name, applied))  # type: ignore[arg-type]
    return applied


def _shells_candidate() -> Candidate:
    # an explicit ladder so the stage never needs the kernel's diagonal
    return Candidate("run_stage", [], {"stage": "shells", "params": {"tol_ladder": [0.01]}}, "det")


def test_a_multi_op_stage_costs_its_real_applied_op_count(sewing_ops: list[str]) -> None:
    g = Scripted(lambda s: [_shells_candidate()] if s.depth == 0 else [])
    r = run([g], World(), start=make_ws(0, ids=("F1", "F2")))
    assert sewing_ops == ["sew", "make_volume"]
    assert r.trace.of_kind("stop")[0]["spent"]["ops"] == 2
    [ev] = [e for e in r.trace.of_kind("candidate") if e["outcome"] == "evaluated"]
    assert ev["ops_applied"] == 2
    assert r.path_steps[-1]["ops"] == 2


def test_a_single_op_candidate_still_costs_one(stage_world: list[Any]) -> None:
    g = Scripted(lambda s: [cand(1)] if s.depth == 0 else [])
    r = run([g], World(), start=make_ws(0))
    assert r.trace.of_kind("stop")[0]["spent"]["ops"] == 1
    [ev] = [e for e in r.trace.of_kind("candidate") if e["outcome"] == "evaluated"]
    assert ev["ops_applied"] == 1


def test_the_op_budget_counts_the_chain_and_stops_the_search(sewing_ops: list[str]) -> None:
    """Two chain candidates are four ops, not two: a budget of three ends the search, and what it
    reports as spent is the op count, in the same units the budget is written in."""
    g = Scripted(lambda s: [_shells_candidate(), _shells_candidate()] if s.depth == 0 else [])
    r = run([g], World(), start=make_ws(0, ids=("F1", "F2")),
            budget=Budget(wall_s=1000, ops=3, model_calls=0, model_usd=0))
    stop = r.trace.of_kind("stop")[0]
    assert stop["reason"] == "ops" and stop["spent"]["ops"] == 4


# --- D9: a declined round is answered with a re-prompt that carries the coded reason and the open sites ----------


def test_after_a_declined_round_the_model_is_asked_again_with_the_coded_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real AIProposer, a scripted model: its only candidate is declined by the op, so the frontier empties.
    Before D9 the search reported `exhausted` after that one turn; now the model is re-prompted on the best state,
    and the turn carries A6's structured `reason` (status + code, not message text to parse) and the observation's
    failing members and sites."""
    from meshpipeline.cad.repair_v4.ops import OpDeclined

    def declines(self: Any, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        raise OpDeclined("no_change: nothing to change on this part")

    monkeypatch.setattr(T18Op, "apply", declines)
    model = FakeModel([reply(call_of("propose", {"candidates": [ai_cand(2)]})),
                       reply(call_of("propose", {"candidates": [ai_cand(3)]}, "c2")), reply()])
    result = run([proposer(model)], World())
    assert len(model.calls) >= 2, "one model turn, then exhausted: the AI was not asked again"
    assert result.trace.of_kind("reprompt")
    body = json.loads(user_turns(model.calls[1])[-1].split("\n", 1)[1])
    [prev] = body["previous_candidates"]
    assert prev["status"] == "declined" and prev["reason"]["code"] == "no_change"
    assert body["observation"]["sites"] and body["observation"]["sites"][0]["meshers_failing"] == ["gmsh"]
