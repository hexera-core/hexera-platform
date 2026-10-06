# Responsibility: Verify the intake presents the MEASURED engine recommendation - the one question proposes the
# best-ranked engine with its evidence and lists the others with their fit - and that the user's pick wins:
# a yes takes the recommendation, naming any listed engine takes that one, before the model runs.
# Boundaries: agents/intake/measured_choice.py, the executor's proposal and engine_selection.choose_listed, plus
# whole intake turns with the model's rounds scripted (as test_engine_asked_once does).
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import measured_choice as mc
from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
from meshpipeline.cad.shape_traits import TRAITS_VERSION, ShapeTraits
from meshpipeline.engines import fitness as F

GAP = {"flow": "internal", "form": "cad", "input_kind": "fluid-domain", "n_triangles": 100,
       "closed": True, "genus": 1, "ports": 2, "gap_vs_port": 0.5, "slenderness": 900.0}


def _rec(engines=("cfmesh", "snappy", "gmsh")) -> F.Recommendation:
    rows = []
    for i in range(5):
        rows += [{"shape": f"g{i}", "case": f"g{i}", "engine": "snappy", "format": "step", "status": "pass",
                  "layers_pct": 96.0, "seconds": 300.0, "traits": GAP},
                 {"shape": f"g{i}", "case": f"g{i}", "engine": "cfmesh", "format": "step",
                  "status": "pass" if i == 0 else "fail", "traits": GAP},
                 {"shape": f"g{i}", "case": f"g{i}", "engine": "gmsh", "format": "step",
                  "status": "pass" if i < 3 else "fail", "traits": GAP}]
    table = F.table_from({"version": "t", "traits_version": TRAITS_VERSION, "rows": rows})
    return F.recommend(ShapeTraits.from_dict(GAP), list(engines), table=table)


# ------------------------------------------------------------------- the words ----
def test_the_question_names_the_best_engine_with_its_evidence_and_lists_the_others():
    text = mc.statement(_rec(), "snappy")
    assert text.startswith("This looks like a thin gap or annulus. I'd mesh it with snappyHexMesh - in our "
                           "lab it passed 5 of 5 similar shapes (thin gaps or annuli, from CAD)")
    assert "near-wall layers on 96% of the wall" in text
    assert "Also able to mesh it: Gmsh (" in text and "passed 3 of 5, no reliable near-wall layers)" in text
    assert "cfMesh (poor fit, passed 1 of 5" in text
    assert text.endswith("OK, or pick another engine?")
    assert text.count("?") == 1


def test_an_engines_own_declared_weak_spot_is_said_as_a_heads_up():
    rec = _rec()
    fit = rec.fit_of("snappy")
    import dataclasses
    rec = dataclasses.replace(rec, fits=tuple(
        dataclasses.replace(f, heads_up="The narrowest passage gets fewer than 12 cells across.")
        if f is fit else f for f in rec.fits))
    text = mc.statement(rec, "snappy")
    assert "Heads-up: The narrowest passage gets fewer than 12 cells across." in text
    assert "Heads-up: The narrowest" in mc.prompt_block(rec, settled=False)


def test_the_prompt_note_ranks_every_engine_and_tells_the_model_to_propose_the_recommendation():
    note = mc.prompt_block(_rec(), settled=False)
    assert "MEASURED ENGINE RECOMMENDATION" in note
    assert "1. snappyHexMesh - best fit" in note and "[RECOMMENDED]" in note
    assert "call propose_engine_selection with snappyHexMesh" in note
    assert mc.prompt_block(None, settled=False) == ""


def test_the_console_payload_exists_only_while_the_question_is_open():
    sel = es.propose("snappy", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    sel["recommendation"] = mc.compact(_rec())
    payload = mc.choice_payload({"selection": sel})
    assert payload["proposed"] == "snappy" and payload["recommended"] == "snappy"
    assert [e["engine"] for e in payload["engines"]] == ["snappy", "gmsh", "cfmesh"]
    assert [e["reply"] for e in payload["engines"]] == ["Use snappyHexMesh", "Use Gmsh", "Use cfMesh"]
    assert payload["engines"][0]["recommended"] and not payload["engines"][1]["recommended"]
    assert payload["shape"] == "a thin gap or annulus"
    confirmed = {**sel, "state": es.CONFIRMED}
    assert mc.choice_payload({"selection": confirmed}) is None
    assert mc.choice_payload({"selection": es.propose("snappy", session_id="s", owner_id="u",
                                                      revision="r", user_msg_count=1)}) is None
    assert mc.choice_payload(None) is None


# ------------------------------------------------------------------- the user picks ----
def _proposed():
    sel = es.propose("snappy", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    sel["recommendation"] = mc.compact(_rec())
    return sel


def _pick(message):
    return es.choose_listed(_proposed(), session_id="s", owner_id="u", revision="r2",
                            latest_user_message=message, user_msg_count=2)


@pytest.mark.parametrize("said,engine", [
    ("Use cfMesh", "cfmesh"), ("cfMesh", "cfmesh"), ("go with Gmsh", "gmsh"), ("Use Gmsh.", "gmsh"),
    ("I'll take cfMesh", "cfmesh"),
])
def test_naming_a_listed_engine_selects_it_before_the_model_runs(said, engine):
    got = _pick(said)
    assert got is not None and got["state"] == es.CONFIRMED and got["engine"] == engine, said
    assert got["recommendation"]["engine"] == "snappy"           # the evidence stays on the record


@pytest.mark.parametrize("said", [
    "what about cfMesh?", "not cfMesh", "cfMesh or Gmsh", "use VMTK", "I don't want Gmsh",
    "ok", "Use snappyHexMesh",                                     # the proposed one is a plain yes
])
def test_anything_but_a_plain_pick_of_another_listed_engine_is_left_to_the_yes_reader_or_the_model(said):
    assert _pick(said) is None


def test_the_recommended_engines_button_is_a_plain_yes():
    got = es.confirm_by_assent(_proposed(), session_id="s", owner_id="u", revision="r2",
                               latest_user_message="Use snappyHexMesh", user_msg_count=2)
    assert got is not None and got["state"] == es.CONFIRMED and got["engine"] == "snappy"


# ------------------------------------------------------------------- the proposal ----
def _executor(recommendation, *, user_texts=(), latest="internal flow, air"):
    st = IntakeExecutionState(session_id="s", owner_id="u", revision="r1", user_msg_count=1,
                              latest_user_msg=latest, user_texts=tuple(user_texts),
                              recommendation=recommendation)
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["cfmesh", "snappy", "gmsh", "vmtk"],
                            search_tool=lambda *a, **k: "")
    return st, ex


def test_the_model_presents_the_recommendation_it_does_not_pick_its_own():
    st, ex = _executor(_rec())
    res = asyncio.run(ex.run("propose_engine_selection", {"engine": "cfMesh", "reason": "it is fast"}))
    assert res.accepted
    assert st.selection["engine"] == "snappy" and st.selection["state"] == es.PROPOSED
    assert st.selection["recommendation"]["engine"] == "snappy"
    assert st.selection_prompt == mc.statement(_rec(), "snappy")
    assert "the measured recommendation for this geometry is snappyHexMesh" in res.content


def test_an_engine_the_user_named_is_theirs_whatever_the_recommendation():
    st, ex = _executor(_rec(), user_texts=("use cfMesh",), latest="use cfMesh")
    asyncio.run(ex.run("propose_engine_selection", {"engine": "cfMesh", "user_named_verbatim": "use cfMesh"}))
    assert st.selection["engine"] == "cfmesh" and st.selection["state"] == es.CONFIRMED


def test_without_a_measurement_the_old_question_is_asked():
    st, ex = _executor(None)
    asyncio.run(ex.run("propose_engine_selection", {"engine": "snappyHexMesh", "reason": "it fits the walls"}))
    assert st.selection["engine"] == "snappy" and "recommendation" not in st.selection
    assert st.selection_prompt == es.render_selection_statement("snappy", "it fits the walls")


# ------------------------------------------------------------- whole turns ----
def _tc(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


class _Model:
    def __init__(self, first, after="Which fluid is it?"):
        self.first, self.after, self.systems = first, after, []

    async def __call__(self, **kw):
        messages = kw.get("messages") or []
        self.systems.append(messages[0]["content"] if messages else "")
        if messages and messages[-1].get("role") == "tool":
            return _resp(content=self.after)
        return self.first


def _turn(messages, gate, model, rec):
    import meshpipeline.adapters.model_inference.router as llm_router

    async def measured(state, msgs):
        return rec

    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": list(messages)}
    if gate is not None:
        state["intake_gate"] = gate
    with patch.object(llm_router, "call_intake_model", model), \
            patch.object(intake, "_measured_recommendation", measured):
        return asyncio.run(intake.node_intake(state))


def test_a_conversation_where_the_user_takes_another_engine_from_the_list():
    msgs = [{"role": "assistant", "content": "Got your file. What will you use the mesh for?"},
            {"role": "user", "content": "internal flow of water through this annulus"}]
    model = _Model(_resp([_tc("propose_engine_selection", {"engine": "cfMesh", "reason": "fast"})]))
    out = _turn(msgs, None, model, _rec())
    reply = out["messages"][-1]["content"]
    assert reply == mc.statement(_rec(), "snappy")
    assert "MEASURED ENGINE RECOMMENDATION" in model.systems[0]
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.PROPOSED and gate["selection"]["engine"] == "snappy"

    # the user clicks "Use Gmsh": selected before the model runs, and the model goes on
    msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": "Use Gmsh"}]
    out = _turn(msgs, gate, _Model(_resp(content="Which fluid is it?")), _rec())
    sel = out["intake_gate"]["selection"]
    assert sel["state"] == es.CONFIRMED and sel["engine"] == "gmsh"
    assert out["messages"][-1]["content"] == "Which fluid is it?"


def test_a_comparison_the_user_asked_for_carries_the_measured_ranking():
    st, ex = _executor(_rec(), latest="which engine would you recommend?")
    st.rec_authorized = True
    res = asyncio.run(ex.run("recommend_compatible_engines",
                             {"purpose": "internal_cfd", "input_kind": "fluid-domain"}))
    rows = json.loads(res.content)["measured_ranking"]
    assert [r["engine"] for r in rows] == ["snappyHexMesh", "Gmsh", "cfMesh"]
    assert rows[0]["recommended"] and rows[0]["reason"].startswith("passed 5 of 5")
