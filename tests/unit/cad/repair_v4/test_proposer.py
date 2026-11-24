# Responsibility: Verify the v4 AI proposer against a scripted fake model - tool transport, the cost caps, the
# conversation it keeps, and what it shows the model.
# Boundaries: no network and no kernel. The model is a script, the Workspace is plain data, the observation is a
# stand-in for Task 17's, and the search driver is just these tests calling propose()/feedback().
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4 import proposer as P
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProposeReply,
    ToolCallRequest,
    UnpricedModel,
)

ROUTE = "openai:gpt-6-astra"
PNG = b"\x89PNG\r\n\x1a\nfake"
ASTRA = (10.0, 50.0, 1.0)
TERRA = (2.0, 12.0, 0.2)


def make_ws() -> Workspace:
    """F1..F4 live; F7 was removed by split_angle, F2 replaced by F5/F6."""
    removed = Diff(op="split_angle", params={}, modified={"F2": ["F5", "F6"]}, generated={},
                   removed=["F7"], declared_added=[], declared_removed=[], seconds=0.1)
    ws = Workspace(brep_path="/tmp/x.brep", ids={f"F{i}": f"fp{i}" for i in (1, 3, 4, 5, 6)},
                   labels={}, history=[removed], original="/tmp/x.brep")
    return ws


def site(sid: str, kind: str, *, png: bytes | None = PNG, entities=("F1",)) -> Site:
    return Site(id=sid, kind=kind, entities=list(entities), xyz=[(0, 0, 0)], meshers_failing=["gmsh"],
                dist_over_h=0.1, graph={}, view_png=png)  # type: ignore[arg-type]


def state(sites: list[Site], *, clean: bool = False, ws: Workspace | None = None):
    return SimpleNamespace(ws=ws or make_ws(), verdict=SimpleNamespace(clean=clean), sites=sites,
                           budget=SimpleNamespace(), round=0)


def fake_observe(st, sites: list[Site]) -> dict:
    """Stands in for Task 17's observation(); the proposer hands it the sites it is still allowed to show."""
    return {"sites": [{"id": s.id, "kind": s.kind, "entities": s.entities} for s in sites],
            "verdict": "not clean"}


def call_of(name: str, args, call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=args if isinstance(args, str) else json.dumps(args))


def reply(*calls: ToolCallRequest, text: str = "") -> ModelRoundResult:
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text, finish_reason="tool_calls")


def cand(op="split_angle", targets=("F1",), params=None, rationale="because"):
    return {"op": op, "targets": list(targets), "params": params if params is not None else {"max_deg": 90},
            "rationale": rationale}


@dataclass
class Fake:
    """A scripted model. Each scripted item is a ModelRoundResult, an Exception to raise, or a callable."""

    script: list
    cost: float = 0.05
    calls: list = field(default_factory=list)

    async def __call__(self, messages, tools, *, route, job_id="", user_id="", tool_choice="auto",
                       cache_key=""):
        self.calls.append(SimpleNamespace(messages=copy.deepcopy(messages), tools=copy.deepcopy(tools),
                                          route=route, job_id=job_id, tool_choice=tool_choice,
                                          cache_key=cache_key))
        if not self.script:
            raise AssertionError("the proposer called the model more often than the script allows")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ProposeReply(reply=item, cost_usd=self.cost, provider="openai", model="gpt-6-astra")


def make(fake: Fake, *, ceiling=1.0, max_turns=6, ledger=None, **kw) -> P.AIProposer:
    kw.setdefault("prices", ASTRA)
    kw.setdefault("output_tokens", 1000)       # reserve for a short answer so small test ceilings stay usable
    return P.AIProposer(ROUTE, max_turns=max_turns, usd_ceiling=ceiling, call=fake, observe=fake_observe,
                        ledger=ledger or WaveLedger.for_test(cap_usd=100.0), file_id=kw.pop("file_id", "f1"),
                        job_id=kw.pop("job_id", "job-9"), **kw)


def tool_results(messages) -> list[dict]:
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


# --- Review Focus 5: the schema error is a message the model acts on ---------------------------------------


def test_schema_error_returned_as_tool_result():
    fake = Fake([reply(call_of("propose", {"candidates": [cand(targets=("F7",))]})),
                 reply(call_of("propose", {"candidates": [cand(targets=("F1",))]}, "c2"))])
    p = make(fake)
    out = p.propose(state([site("S1", "proximity")]), k=3)

    assert len(fake.calls) == 2                      # the bad call cost a turn, and the next turn was allowed
    [result] = tool_results(fake.calls[1].messages)
    assert result["ok"] is False and result["accepted"] == 0
    [err] = result["errors"]
    assert err["index"] == 0 and err["op"] == "split_angle"
    assert any("F7" in e and "removed by op 'split_angle'" in e for e in err["errors"])
    assert [(c.op, c.targets, c.source) for c in out] == [("split_angle", ["F1"], "ai")]
    assert p.stop_reason is None and not p.ai_capped


def test_tool_errors_and_rejected_candidates_are_counted_for_the_triage_report():
    """A tool call answered `ok: false` is a tool error; each candidate it refused is a candidate error. Both reach
    `result()`, which the eval row's triage block reads (lane C2)."""
    bad = {"candidates": [cand(targets=("F7",)), cand(op="melt_everything")]}
    fake = Fake([reply(call_of("propose", bad)), reply(call_of("propose", "{not json", "c2")),
                 reply(call_of("propose", {"candidates": [cand(targets=("F1",))]}, "c3"))])
    p = make(fake)
    p.propose(state([site("S1", "proximity")]), k=3)
    r = p.result()
    assert (r["tool_errors"], r["candidate_errors"], r["declared_unfixable"]) == (2, 2, 0)


def test_a_declaration_is_counted_only_when_accepted():
    fake = Fake([reply(call_of("declare_unfixable", {"site_id": "S1", "reason": "needs 0.4 mm > bound"}),
                       call_of("declare_unfixable", {"site_id": "S404", "reason": "x"}, "c2")), reply()])
    p = make(fake)
    p.propose(state([site("S1", "proximity")]), k=3)
    assert p.result()["declared_unfixable"] == 1 and p.result()["tool_errors"] == 1


def test_a_replaced_target_names_its_successors():
    fake = Fake([reply(call_of("propose", {"candidates": [cand(targets=("F2",))]})), reply()])
    make(fake).propose(state([site("S1", "proximity")]), k=3)
    [result] = tool_results(fake.calls[1].messages)
    assert "replaced by op 'split_angle'" in json.dumps(result) and "F5, F6" in json.dumps(result)


@pytest.mark.parametrize("args, needle", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "must be a JSON object"),
    ({"candidates": "nope"}, "candidates"),
    ({"candidates": []}, "at least one"),
    ({"candidates": [cand(op="melt_everything")]}, "unknown op"),
    ({"candidates": [{"targets": ["F1"], "params": {}}]}, "op"),
    ({"candidates": [{**cand(), "surprise": 1}]}, "surprise"),            # filler keys, v3's other failure
    ({"candidates": [cand(params={"max_deg": 90, "filler": 1})]}, "filler"),
    ({"candidates": [cand(params={"max_deg": 9999})]}, "maximum"),
    ({"candidates": [cand(targets=("F99",))]}, "F99"),
])
def test_malformed_calls_are_structured_errors_that_cost_a_turn_and_nothing_else(args, needle):
    fake = Fake([reply(call_of("propose", args)), reply(call_of("propose", {"candidates": [cand()]}, "c2"))])
    ledger = WaveLedger.for_test(cap_usd=100.0)
    p = make(fake, ledger=ledger)
    out = p.propose(state([site("S1", "proximity")]), k=3)
    [result] = tool_results(fake.calls[1].messages)
    assert result["ok"] is False and needle in json.dumps(result)
    assert len(out) == 1                              # recovered on the next turn
    assert p.turns_used == 2 and p.stop_reason is None
    assert ledger.spent_usd() == pytest.approx(0.10)  # only the two model calls themselves


def test_an_unknown_tool_is_answered_not_raised():
    fake = Fake([reply(call_of("rm_rf", {})), reply(call_of("propose", {"candidates": [cand()]}, "c2"))])
    out = make(fake).propose(state([site("S1", "proximity")]), k=3)
    [result] = tool_results(fake.calls[1].messages)
    assert result["ok"] is False and "unknown tool" in json.dumps(result) and out


def test_valid_candidates_in_a_mixed_call_are_kept_and_the_bad_one_is_reported():
    fake = Fake([reply(call_of("propose", {"candidates": [cand(), cand(targets=("F7",))]}))])
    p = make(fake)
    out = p.propose(state([site("S1", "proximity")]), k=3)
    assert len(out) == 1 and len(fake.calls) == 1
    # the verdict on the mixed call is still delivered, on the next turn
    fake.script.append(reply())
    p.propose(state([site("S1", "proximity")]), k=3)
    [result] = tool_results(fake.calls[1].messages)
    assert result["accepted"] == 1 and len(result["errors"]) == 1


def test_candidates_beyond_k_are_not_returned_and_the_model_is_told():
    fake = Fake([reply(call_of("propose", {"candidates": [cand(), cand(params={"max_deg": 60}),
                                                          cand(params={"max_deg": 45})]}))])
    p = make(fake)
    assert len(p.propose(state([site("S1", "proximity")]), k=2)) == 2
    fake.script.append(reply())
    p.propose(state([site("S1", "proximity")]), k=2)
    [result] = tool_results(fake.calls[1].messages)
    assert result["accepted"] == 2 and result["deferred"] == 1


def test_run_stage_is_accepted_only_for_a_declared_stage():
    fake = Fake([reply(call_of("propose", {"candidates": [{"run_stage": "nope", "rationale": "r"}]})),
                 reply(call_of("propose", {"candidates": [{"run_stage": "faces", "params": {"tol_mm": 0.02},
                                                           "rationale": "r"}]}, "c2"))])
    out = make(fake, stage_names=("faces", "shells")).propose(state([site("S1", "proximity")]), k=3)
    [result] = tool_results(fake.calls[1].messages)
    assert "unknown stage" in json.dumps(result) and "faces" in json.dumps(result)
    assert [(c.op, c.targets, c.params) for c in out] == [("run_stage", [], {"stage": "faces", "params": {"tol_mm": 0.02}})]


def test_run_stage_refuses_a_parameter_the_stage_does_not_read_and_targets_it_ignores():
    """A stage merges params over its defaults, so a misspelt key used to be ignored in silence and the model was
    never told. It is now a structured error naming the stage's real parameters; so are targets, which a stage
    does not take."""
    bad_key = {"run_stage": "faces", "params": {"tol": 1}, "rationale": "r"}
    with_targets = {"run_stage": "shells", "targets": ["E1"], "params": {}, "rationale": "r"}
    good = {"run_stage": "shells", "params": {"fill_edges": ["E1"]}, "rationale": "r"}
    fake = Fake([reply(call_of("propose", {"candidates": [bad_key, with_targets, good]}))])
    p = make(fake, stage_names=("faces", "shells"))
    out = p.propose(state([site("S1", "open")]), k=3)
    assert [(c.op, c.params) for c in out] == [("run_stage", {"stage": "shells", "params": {"fill_edges": ["E1"]}})]
    [result] = tool_results(p.messages)
    errors = {e["index"]: " ".join(e["errors"]) for e in result["errors"]}
    assert set(errors) == {0, 1} and result["accepted"] == 1
    assert "'tol'" in errors[0] and "tol_mm" in errors[0]          # names the real parameter
    assert "no targets" in errors[1]


def test_the_stage_catalogue_is_generated_from_the_stages_own_defaults():
    from meshpipeline.cad.repair_v4.stages import DEFAULTS
    text = P.system_prompt(stage_names=("faces", "shells"))
    for name in ("faces", "shells"):
        assert f"  {name}: " + json.dumps(DEFAULTS[name], sort_keys=True, separators=(",", ":")) in text


def test_what_clean_means_is_written_from_the_checkers_constants(monkeypatch):
    """The prompt's definition of clean is generated from the checker's own constants, so it cannot drift: every
    hard code is listed with a description, every soft code is said NOT to fail, and the numeric limits are the
    checker's. Changing a constant changes the prompt."""
    from meshpipeline.cad.repair_v4.verdict import mesh_child, valid

    assert set(P.HARD_CODE_TEXT) == set(valid.HARD_CODES)
    text = P.system_prompt(stage_names=())
    valid_para = text.split("1. Valid:")[1].split("2. Meshable:")[0]
    for code in valid.HARD_CODES:
        assert f"   - {code}: {P.HARD_CODE_TEXT[code]}" in valid_para
    soft_line = next(line for line in valid_para.splitlines() if "SOFT" in line)
    assert all(code in soft_line for code in valid.SOFT_CODES) and "do NOT fail" in valid_para
    assert not any(f"- {code}:" in valid_para for code in valid.SOFT_CODES)
    assert "(undescribed)" not in text
    assert "at least 0.01" in text
    # B12 (owner decision 2026-11-24): clean is VALID + MESHABLE. No fidelity limit is stated as a requirement any more
    flat = " ".join(text.split())
    assert "VALID and MESHABLE" in flat and "FAITHFULNESS IS REPORTED, NOT REQUIRED" in flat
    for gone in ("min(0.1 mm", "0.1% of the part diagonal", "at most 2%", "are declined", "never fuses",
                 "within the fidelity bound", "3. Faithful"):
        assert gone not in flat, gone
    monkeypatch.setattr(mesh_child, "MIN_SICN", 0.05)
    monkeypatch.setattr(valid, "SOFT_CODES", ("small_edge", "tiny_thing"))
    changed = P.system_prompt(stage_names=())
    assert "at least 0.05" in changed and "tiny_thing" in changed


def test_without_declared_stages_run_stage_is_not_offered():
    fake = Fake([reply()])
    make(fake).propose(state([site("S1", "proximity")]), k=3)
    schema = fake.calls[0].tools[0]["function"]["parameters"]["properties"]["candidates"]["items"]
    assert "run_stage" not in schema["properties"]
    assert set(schema["properties"]["op"]["enum"]) >= {"split_angle"}


# --- the cost controls are implemented ----------------------------------------------------------------------


def test_usd_ceiling_stops_calls():
    bad = reply(call_of("propose", {"candidates": [cand(targets=("F7",))]}))
    fake = Fake([bad, bad, bad, bad], cost=0.20)
    p = make(fake, ceiling=0.5, max_turns=10)
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert len(fake.calls) == 2                      # $0.20 + $0.20; a third at ~$0.25 would breach $0.50
    assert p.stop_reason == "file_ceiling" and p.ai_capped
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert len(fake.calls) == 2                      # and it stays stopped


def test_wave_cap_is_shared_across_shards_and_marks_the_rest_ai_capped(tmp_path):
    path = tmp_path / "wave.jsonl"
    first = Fake([reply(call_of("propose", {"candidates": [cand()]}))], cost=0.45)
    a = make(first, ledger=WaveLedger.for_test(cap_usd=0.5, path=path), file_id="fa")
    assert len(a.propose(state([site("S1", "proximity")]), k=3)) == 1

    second = Fake([reply(call_of("propose", {"candidates": [cand()]}))])
    b = make(second, ledger=WaveLedger.for_test(cap_usd=0.5, path=path), file_id="fb")      # another shard, same wave
    assert b.propose(state([site("S1", "proximity")]), k=3) == []
    assert second.calls == []                          # the cap stopped the call, not the model
    assert b.ai_capped and b.stop_reason == "no_affordable_turn"  # not one turn fit what the wave had left


def test_every_call_is_written_to_the_ledger_with_its_job_and_model():
    ledger = WaveLedger.for_test(cap_usd=100.0)
    fake = Fake([reply(call_of("propose", {"candidates": [cand()]}))])
    make(fake, ledger=ledger, file_id="fileA", job_id="job-77").propose(state([site("S1", "proximity")]), k=3)
    [row] = ledger.entries()
    assert (row["file_id"], row["job_id"], row["provider"], row["model"]) == (
        "fileA", "job-77", "openai", "gpt-6-astra")
    assert row["usd"] == pytest.approx(0.05)


def test_a_clean_state_makes_no_call():
    fake = Fake([])
    assert make(fake).propose(state([site("S1", "proximity")], clean=True), k=3) == []
    assert fake.calls == [] and make(fake).stop_reason is None


def test_max_turns_bounds_the_conversation_across_rounds():
    fake = Fake([reply(call_of("propose", {"candidates": [cand()]})) for _ in range(5)])
    p = make(fake, max_turns=2)
    p.propose(state([site("S1", "proximity")]), k=3)
    p.propose(state([site("S1", "proximity")]), k=3)
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert len(fake.calls) == 2 and p.stop_reason == "max_turns"


def test_one_provider_failure_does_not_disable_the_ai_for_the_file():
    ledger = WaveLedger.for_test(cap_usd=100.0)
    fake = Fake([ModelRoundResult(failure_marker="x:timeout"),
                 reply(call_of("propose", {"candidates": [cand()]}))], cost=0.05)
    p = make(fake, ledger=ledger)
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert p.stop_reason is None and ledger.reserved_usd() == 0.0          # released, nothing billed
    assert len(p.propose(state([site("S1", "proximity")]), k=3)) == 1      # and the next round works


def test_consecutive_provider_failures_give_the_ai_up_for_the_file():
    fail = ModelRoundResult(failure_marker="x:down")
    fake = Fake([fail, fail, fail, reply()])
    p = make(fake)
    for _ in range(3):
        p.propose(state([site("S1", "proximity")]), k=3)
    assert p.stop_reason == "model_unavailable" and not p.ai_capped
    assert p.propose(state([site("S1", "proximity")]), k=3) == [] and len(fake.calls) == 3


def test_an_unpriced_model_propagates_and_is_never_metered_as_free():
    ledger = WaveLedger.for_test(cap_usd=100.0)
    fake = Fake([UnpricedModel("openai:mystery")])
    p = make(fake, ledger=ledger)
    with pytest.raises(UnpricedModel):
        p.propose(state([site("S1", "proximity")]), k=3)
    assert ledger.spent_usd() == 0.0 and ledger.reserved_usd() == 0.0 and ledger.entries() == []


# --- the conversation ---------------------------------------------------------------------------------------


def test_conversation_persists_across_rounds():
    fake = Fake([reply(call_of("propose", {"candidates": [cand()]})), reply()])
    p = make(fake)
    [c] = p.propose(state([site("S1", "proximity")]), k=3)
    diff = Diff(op="split_angle", params={"max_deg": 90}, modified={"F1": ["F8", "F9"]}, generated={},
                removed=[], declared_added=[], declared_removed=[], seconds=0.2)
    p.feedback([P.Outcome(candidate=c, status="applied", diff=diff,
                          delta={"valid": [0, 1], "panel_passes": [1, 2], "min_sicn": [0.001, 0.2]})])
    p.propose(state([site("S1", "proximity")]), k=3)

    first, second = fake.calls
    assert second.messages[:len(first.messages)] == first.messages      # append-only: the same conversation
    n = len(first.messages)
    assert [m["role"] for m in second.messages[n:n + 2]] == ["assistant", "tool"]   # its call, then the answer
    last = second.messages[-1]
    assert last["role"] == "user"
    text = last["content"] if isinstance(last["content"], str) else last["content"][0]["text"]
    fb = json.loads(text[text.index("{"):])["previous_candidates"]
    assert fb[0]["status"] == "applied" and fb[0]["op"] == "split_angle"
    assert fb[0]["diff"]["modified"] == {"F1": ["F8", "F9"]}
    assert fb[0]["delta"]["panel_passes"] == [1, 2]


def test_a_declined_and_a_failed_candidate_are_reported_with_their_reason():
    fake = Fake([reply(call_of("propose", {"candidates": [cand(), cand(params={"max_deg": 60})]})), reply()])
    p = make(fake)
    c1, c2 = p.propose(state([site("S1", "proximity")]), k=3)
    p.feedback([P.Outcome(candidate=c1, status="declined", message="nothing to split"),
                P.Outcome(candidate=c2, status="failed", message="timeout: worker died")])
    p.propose(state([site("S1", "proximity")]), k=3)
    text = json.dumps(fake.calls[1].messages[-1])
    assert "nothing to split" in text and "worker died" in text


# --- what the model is shown --------------------------------------------------------------------------------


def images_in(message) -> list[dict]:
    content = message["content"]
    return [] if isinstance(content, str) else [p for p in content if p["type"] == "image_url"]


def test_images_attached_when_site_has_view():
    fake = Fake([reply()])
    make(fake).propose(state([site("S1", "overlap"), site("S2", "open")]), k=3)
    user = fake.calls[0].messages[-1]
    assert len(images_in(user)) == 2
    assert images_in(user)[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("kind", ["proximity", "self_overlap", "intersection", "invalid", "sliver",
                                  "periodic", "pole", "timeout", "unlocated", "unbounded"])
def test_images_are_not_attached_for_kinds_that_need_no_intent_call(kind):
    fake = Fake([reply()])
    make(fake).propose(state([site("S1", kind)]), k=3)
    assert images_in(fake.calls[0].messages[-1]) == []


def test_no_image_when_the_site_has_no_view():
    fake = Fake([reply()])
    make(fake).propose(state([site("S1", "overlap", png=None)]), k=3)
    assert images_in(fake.calls[0].messages[-1]) == []


def test_a_site_view_is_sent_once_per_conversation():
    fake = Fake([reply(call_of("propose", {"candidates": [cand()]})), reply()])
    p = make(fake)
    p.propose(state([site("S1", "overlap")]), k=3)
    p.propose(state([site("S1", "overlap")]), k=3)
    assert len(images_in(fake.calls[0].messages[-1])) == 1
    assert images_in(fake.calls[1].messages[-1]) == []


def test_unfixable_site_excluded_from_det_sweeps():
    registry = P.UnfixableRegistry()
    fake = Fake([reply(call_of("declare_unfixable", {"site_id": "S2", "reason": "needs 0.4 mm > bound"})),
                 reply(), reply()])
    p = make(fake, unfixable=registry)
    sites = [site("S1", "proximity"), site("S2", "overlap")]
    p.propose(state(sites), k=3)

    assert registry.is_declared("S2") and registry.reasons[("", "S2")] == "needs 0.4 mm > bound"
    assert [s.id for s in registry.filter(sites)] == ["S1"]         # what a det generator sweeps
    [ack] = tool_results(fake.calls[1].messages)
    assert ack["ok"] is True
    p.propose(state(sites), k=3)
    assert "S2" not in json.dumps(fake.calls[2].messages[-1]["content"])  # and the AI stops being shown it


def test_declaring_an_unknown_site_is_a_structured_error():
    fake = Fake([reply(call_of("declare_unfixable", {"site_id": "S404", "reason": "x"})), reply()])
    registry = P.UnfixableRegistry()
    make(fake, unfixable=registry).propose(state([site("S1", "proximity")]), k=3)
    [res] = tool_results(fake.calls[1].messages)
    assert res["ok"] is False and "S404" in json.dumps(res) and not registry.is_declared("S404")


def test_declare_unfixable_requires_a_reason():
    fake = Fake([reply(call_of("declare_unfixable", {"site_id": "S1", "reason": " "})), reply()])
    registry = P.UnfixableRegistry()
    make(fake, unfixable=registry).propose(state([site("S1", "proximity")]), k=3)
    assert not registry.is_declared("S1")


# --- the static prefix, and the system prompt ---------------------------------------------------------------


def test_the_static_prefix_is_identical_across_files_and_rounds_and_keyed_for_the_cache():
    f1, f2 = Fake([reply(), reply()]), Fake([reply()])
    a, b = make(f1, file_id="a"), make(f2, file_id="b")
    a.propose(state([site("S1", "proximity")]), k=3)
    a.propose(state([site("S1", "proximity")]), k=3)
    b.propose(state([site("S9", "overlap")]), k=3)
    sys_msgs = {json.dumps(c.messages[0]) for c in (*f1.calls, *f2.calls)}
    tools = {json.dumps(c.tools, sort_keys=False) for c in (*f1.calls, *f2.calls)}
    keys = {c.cache_key for c in (*f1.calls, *f2.calls)}
    assert len(sys_msgs) == 1 and len(tools) == 1 and len(keys) == 1 and "" not in keys
    assert f1.calls[0].messages[0]["role"] == "system"             # the prefix is first


def test_the_cache_key_changes_when_the_prefix_does():
    f1, f2 = Fake([reply()]), Fake([reply()])
    make(f1).propose(state([site("S1", "proximity")]), k=3)
    make(f2, stage_names=("faces",)).propose(state([site("S1", "proximity")]), k=3)
    assert f1.calls[0].cache_key != f2.calls[0].cache_key


def test_the_system_prompt_states_the_contract():
    text = P.system_prompt(stage_names=())
    low = text.lower()
    for must in ("clean", "round trip", "every mesher", "reported, not required", "overlap", "hole", "missing face",
                 "budget", "order", "stop", "declare_unfixable", "malformed", "costs nothing but a turn"):
        assert must in low, must
    assert "split_angle" in text                                   # the op catalogue is part of the prefix


def test_every_registered_op_is_explained_in_the_catalogue_not_just_named():
    """A schema gives parameter names and bounds, not the action: the model must be told what each op DOES.
    A new op without a summary fails here instead of reaching the model as a bare name."""
    from meshpipeline.cad.repair_v4.ops import REGISTRY

    assert set(P.OP_SUMMARIES) == set(REGISTRY), (
        f"missing: {sorted(set(REGISTRY) - set(P.OP_SUMMARIES))}; stale: {sorted(set(P.OP_SUMMARIES) - set(REGISTRY))}")
    text = P.system_prompt(stage_names=())
    assert "(no summary)" not in text
    for name, summary in P.OP_SUMMARIES.items():
        assert f"- {name}: {summary}" in text, name
    # the checker's SOFT codes must not be presented to the model as failing "valid"
    from meshpipeline.cad.repair_v4.verdict.valid import SOFT_CODES
    valid_para = text.split("1. Valid:")[1].split("2. Meshable:")[0].lower()
    assert "do not fail" in valid_para and {"small_edge", "small_face", "large_tolerance"} == set(SOFT_CODES)
    assert "small-edge or small-face errors" not in valid_para
    # the open-loop repair is two ops in sequence; the catalogue must say so
    assert "make_volume" in P.OP_SUMMARIES["fill_loop"]


def test_the_job_id_route_and_tools_reach_the_model_call():
    fake = Fake([reply()])
    make(fake, job_id="job-123").propose(state([site("S1", "proximity")]), k=3)
    c = fake.calls[0]
    assert c.job_id == "job-123" and c.route == ROUTE
    assert [t["function"]["name"] for t in c.tools] == ["propose", "declare_unfixable"]


def test_propose_may_be_called_from_inside_a_running_event_loop():
    fake = Fake([reply(call_of("propose", {"candidates": [cand()]}))])
    p = make(fake)

    async def main():
        return p.propose(state([site("S1", "proximity")]), k=3)

    assert len(asyncio.run(main())) == 1


# --- the probe, which runs before any paid wave -------------------------------------------------------------


def _probe_model(*, use_tool=True, colour="red"):
    async def call(messages, tools, *, route, job_id="", user_id="", tool_choice="auto", cache_key=""):
        assert tool_choice == "required"
        assert any(p["type"] == "image_url" for p in messages[-1]["content"])      # one image
        assert len(tools) == 1                                                       # one tool
        calls = (call_of("report_colour", {"colour": colour}),) if use_tool else ()
        return ProposeReply(reply=ModelRoundResult(tool_calls=calls, assistant_text="no"), cost_usd=0.001,
                            provider="openai", model=route.split(":", 1)[1])
    return call


def test_probe_passes_when_the_model_calls_the_tool_and_sees_the_image():
    ok = asyncio.run(P.probe_route(ROUTE, call=_probe_model()))
    assert ok.ok and ok.route == ROUTE


@pytest.mark.parametrize("kw", [{"use_tool": False}, {"colour": "green"}])
def test_probe_fails_when_the_tool_or_the_image_is_not_understood(kw):
    assert not asyncio.run(P.probe_route(ROUTE, call=_probe_model(**kw))).ok


@pytest.mark.parametrize("colour", ["red", "Salmon", "crimson", "scarlet red", "coral", "bright red (#FF0000)"])
def test_probe_accepts_any_name_in_the_red_hue_family(colour):
    """Snapshot v1's dev6 shard was refused because astra called the solid-red swatch "salmon" - a colour NAME, not
    a failure to see the image. Any red-family name passes."""
    assert asyncio.run(P.probe_route(ROUTE, call=_probe_model(colour=colour))).ok


@pytest.mark.parametrize("colour", ["green", "blue", "orange", "yellow", "", "I cannot see an image"])
def test_probe_still_fails_an_answer_outside_the_red_family(colour):
    res = asyncio.run(P.probe_route(ROUTE, call=_probe_model(colour=colour)))
    assert not res.ok and "not red" in res.detail


def test_one_wrong_colour_is_retried_once_and_a_second_right_answer_passes():
    answers = iter(["blue", "red"])
    calls: list = []

    async def flaky(messages, tools, *, route, **kw):
        calls.append(route)
        return await _probe_model(colour=next(answers))(messages, tools, route=route, **kw)

    res = asyncio.run(P.probe_route(ROUTE, call=flaky))
    assert res.ok and len(calls) == 2


def test_two_wrong_colours_fail_the_probe_after_exactly_two_calls():
    calls: list = []

    async def wrong(messages, tools, *, route, **kw):
        calls.append(route)
        return await _probe_model(colour="green")(messages, tools, route=route, **kw)

    assert not asyncio.run(P.probe_route(ROUTE, call=wrong)).ok and len(calls) == P.PROBE_ATTEMPTS == 2


def test_the_probe_image_is_large_enough_to_be_unambiguous():
    import struct
    png = P._solid_png((255, 0, 0), P.PROBE_IMAGE_PX)
    width, height = struct.unpack(">II", png[16:24])
    assert width == height == P.PROBE_IMAGE_PX >= 64


def test_probe_reports_an_exception_as_a_failure_not_a_crash():
    async def boom(*a, **k):
        raise RuntimeError("model gpt-6-astra does not exist")
    res = asyncio.run(P.probe_route(ROUTE, call=boom))
    assert not res.ok and "does not exist" in res.detail


def test_select_route_falls_back_to_terra_when_astra_fails_the_probe():
    async def call(messages, tools, *, route, **kw):
        if "astra" in route:
            raise RuntimeError("image input not supported")
        return await _probe_model()(messages, tools, route=route, **kw)

    chosen = asyncio.run(P.select_route(("openai:gpt-6-astra", "openai:gpt-5.6-terra"), call=call))
    assert chosen == "openai:gpt-5.6-terra"


def test_select_route_refuses_when_nothing_passes_rather_than_guessing():
    async def call(*a, **k):
        raise RuntimeError("down")
    with pytest.raises(P.NoUsableRoute):
        asyncio.run(P.select_route(("openai:a", "openai:b"), call=call))


# --- the cap is not optional, the estimate is an upper bound, and the stop is recorded ---------------------


def test_a_ledger_that_did_not_come_from_for_shard_is_refused_by_the_proposer(monkeypatch):
    ledger = WaveLedger.for_test(10.0)
    ledger.provenance = "unset"                            # as a hand-built one would be
    with pytest.raises(TypeError, match="for_shard"):
        P.AIProposer(ROUTE, max_turns=3, usd_ceiling=1.0, ledger=ledger, call=Fake([]), prices=ASTRA)


def test_a_round_that_ran_out_of_output_on_reasoning_is_counted():
    cut = ModelRoundResult(finish_reason="length")
    fake = Fake([cut])
    p = make(fake)
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert p.result()["truncated_rounds"] == 1 and p.stop_reason is None


def test_the_output_limit_is_one_number_for_the_request_and_the_reservation():
    from meshpipeline.adapters.model_inference import router
    from meshpipeline.contracts import model_inference as mi
    assert router._PROPOSE_SPEC.max_tokens == mi.REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS == 4096
    mi.set_model_router(router)
    p = P.AIProposer(ROUTE, max_turns=3, usd_ceiling=1.0, call=Fake([]), ledger=WaveLedger.for_test(10.0))
    assert p._output_tokens == router._PROPOSE_SPEC.max_tokens


def test_a_ledger_is_required_and_the_cap_cannot_be_forgotten():
    with pytest.raises(TypeError):
        P.AIProposer(ROUTE, max_turns=3, usd_ceiling=1.0, call=Fake([]), observe=fake_observe, prices=ASTRA)   # type: ignore[call-arg]
    with pytest.raises(TypeError):
        P.AIProposer(ROUTE, max_turns=3, usd_ceiling=1.0, ledger=None, call=Fake([]), prices=ASTRA)  # type: ignore[arg-type]


def test_the_estimate_grows_with_the_prompt_and_counts_every_image_every_call():
    small = [{"role": "user", "content": "x" * 3000}]
    big = [{"role": "user", "content": "x" * 300_000}]
    with_image = small + [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "d"}}]}]
    prices = (10.0, 50.0, 1.0)
    kw = {"prices": prices, "output_tokens": 0}
    e_small, t_small = P.estimate_call_usd(small, [], **kw)
    e_big, t_big = P.estimate_call_usd(big, [], **kw)
    e_img, t_img = P.estimate_call_usd(with_image, [], **kw)
    assert t_small == 1000 and t_big == 100_000 and e_big == pytest.approx(1.0)
    assert t_img - t_small == P.IMAGE_TOKENS and e_img > e_small
    # output is reserved in full
    assert P.estimate_call_usd(small, [], prices=prices, output_tokens=8192)[0] == pytest.approx(e_small + 0.4096)


def test_the_estimate_never_assumes_a_cache_hit():
    msgs = [{"role": "user", "content": "x" * 3000}]
    cheap_cache = P.estimate_call_usd(msgs, [], prices=(10.0, 50.0, 0.0), output_tokens=0)
    dear_cache = P.estimate_call_usd(msgs, [], prices=(10.0, 50.0, 10.0), output_tokens=0)
    assert cheap_cache == dear_cache and cheap_cache[0] == pytest.approx(0.01)   # 1000 tokens at $10/Mtok


def test_the_proposer_sizes_reservations_from_its_routes_own_prices(monkeypatch):
    from meshpipeline.adapters.model_inference import router
    from meshpipeline.contracts import model_inference as mi
    mi.set_model_router(router)
    astra = P.AIProposer("openai:gpt-6-astra", max_turns=3, usd_ceiling=1.0, call=Fake([]),
                         ledger=WaveLedger.for_test(cap_usd=10.0))
    terra = P.AIProposer("openai:gpt-5.6-terra", max_turns=3, usd_ceiling=1.0, call=Fake([]),
                         ledger=WaveLedger.for_test(cap_usd=10.0))
    assert astra._prices == (12.5, 50.0, 1.0) and terra._prices == (2.0, 12.0, 0.2)
    with pytest.raises(UnpricedModel, match="MODEL_PRICE_OVERRIDES"):
        P.AIProposer("openai:gpt-9-mystery", max_turns=3, usd_ceiling=1.0, call=Fake([]),
                     ledger=WaveLedger.for_test(cap_usd=10.0))


def test_first_call_reservation_on_both_models_with_the_real_prefix():
    """The numbers the per-file ceiling decision rests on: a first call carrying the real system prompt, the real
    tool schemas and a 20 kB observation, reserved at each route's own prices and the real output limit."""
    from meshpipeline.contracts.model_inference import REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS as LIMIT
    out = {}
    for name, prices in (("astra", ASTRA), ("terra", TERRA)):
        p = make(Fake([]), prices=prices, output_tokens=LIMIT)
        p._messages.append({"role": "user", "content": "x" * 20_000})
        usd, tokens = P.estimate_call_usd(p.messages, p._tools, prices=prices, output_tokens=LIMIT)
        out[name] = usd
        assert 3000 < tokens < 12_000                       # prefix + observation, in estimated tokens
    assert 0.28 < out["astra"] < 0.32 and 0.05 < out["terra"] < 0.08
    assert out["astra"] / out["terra"] > 3.5


def test_a_first_call_above_59k_input_tokens_cannot_fit_a_one_dollar_astra_ceiling():
    # at the 8192-token limit this review first assumed; at the real 4096 the threshold is higher (below)
    usd, tokens = P.estimate_call_usd([{"role": "user", "content": "x" * 3 * 60_000}], [],
                                      prices=ASTRA, output_tokens=8192)
    assert tokens == 60_000 and usd > 1.0
    usd, tokens = P.estimate_call_usd([{"role": "user", "content": "x" * 3 * 58_000}], [],
                                      prices=ASTRA, output_tokens=8192)
    assert usd < 1.0
    assert (1.0 - 4096 * 50 / 1e6) / 10e-6 > 79_000        # with the real limit, ~79.5k input tokens fit


def test_a_call_whose_estimate_exceeds_the_file_ceiling_is_refused_before_it_is_made():
    fake = Fake([reply()])
    big_observe = lambda st, sites: {"blob": "x" * 150_000}          # noqa: E731  ~ 50k tokens ~ $0.5 at astra
    p = P.AIProposer(ROUTE, max_turns=3, usd_ceiling=0.2, call=fake, observe=big_observe,
                     ledger=WaveLedger.for_test(cap_usd=100.0), file_id="f1", prices=ASTRA)
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert fake.calls == [] and p.stop_reason == "no_affordable_turn" and p.ai_capped


def test_an_oversize_input_is_refused_at_admit_even_with_budget_to_spare():
    fake = Fake([reply()])
    huge = lambda st, sites: {"blob": "x" * 700_000}                 # noqa: E731  ~ 233k tokens
    p = P.AIProposer(ROUTE, max_turns=3, usd_ceiling=1.0, call=fake, observe=huge,
                     ledger=WaveLedger.for_test(cap_usd=300.0), file_id="f1", prices=ASTRA)
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert fake.calls == [] and p.stop_reason == "input_too_large" and not p.ai_capped


def test_the_history_that_is_resent_is_what_is_estimated():
    # round 1 is cheap, but the conversation (including the first round's image) is re-sent in round 2
    fake = Fake([reply(call_of("propose", {"candidates": [cand()]})), reply()], cost=0.01)
    p = make(fake)
    p.propose(state([site("S1", "overlap")]), k=3)
    before = P._message_tokens(p.messages)
    p.propose(state([site("S1", "overlap")]), k=3)
    assert P._message_tokens(p.messages) > before and before >= P.IMAGE_TOKENS


def test_the_stop_is_written_to_the_ledger_and_to_the_result(tmp_path):
    ledger = WaveLedger.for_test(cap_usd=0.05, path=tmp_path / "w.jsonl")
    fake = Fake([reply()])
    p = make(fake, ledger=ledger, file_id="fileZ")
    assert p.propose(state([site("S1", "proximity")]), k=3) == []
    assert ledger.file_statuses()["fileZ"] == {"stop_reason": "no_affordable_turn", "ai_capped": True}
    assert p.result() == {"file_id": "fileZ", "ai_capped": True, "stop_reason": "no_affordable_turn", "turns": 0,
                          "truncated_rounds": 0, "answered_rounds": 0, "failed_rounds": 0,
                          "model_unavailable": False, "spent_usd": 0.0, "tool_errors": 0, "candidate_errors": 0,
                          "declared_unfixable": 0}
