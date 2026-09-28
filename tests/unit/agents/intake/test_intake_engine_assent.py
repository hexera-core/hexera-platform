# Responsibility: Verify a plain yes to the application's engine question confirms the PROPOSED engine, and only that one.
# Boundaries: the assent grammar and the confirm rule; what a proposal is comes from test_intake_selection_lifecycle.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es

_INVENTED_QUOTE = "Yes, select snappyHexMesh."     # words the model wrote, not the user


def _proposed(engine="snappy", msg_count=1):
    return es.propose(engine, session_id="s", owner_id="u", revision="r1",
                      user_msg_count=msg_count)


def _confirm(said, quote=_INVENTED_QUOTE, sel=None, msg_count=2):
    return es.confirm(sel or _proposed(), session_id="s", owner_id="u", revision="r2",
                      user_msg_count=msg_count, quote=quote, latest_user_message=said)


# the grammar

@pytest.mark.parametrize("said", [
    "yes", "Yes.", "ok", "OK", "okay", "sure", "Sure!", "fine", "go ahead", "go with that",
    "yes please", "Yes, snappyHexMesh.", "select snappyHexMesh", "use it", "yes, go ahead",
    "yes - proceed", "sounds good, yes", "confirmed",
])
def test_a_plain_yes_is_assent(said):
    assert es.plain_assent("snappy", said) is True


@pytest.mark.parametrize("said", [
    "maybe", "I think so", "not sure", "hmm", "what is snappyHexMesh?", "no", "not that one",
    "yes, but the inlet is 40 mm", "yes but use cfmesh", "the wall is called pipe", "",
])
def test_anything_else_is_not_assent(said):
    assert es.plain_assent("snappy", said) is False


@pytest.mark.parametrize("said", [
    "no", "No.", "nope", "not that one", "no thanks", "don't", "no, don't use snappyHexMesh",
    "definitely not", "I don't think so", "please don't", "wait, what?", "stop",
    "I'd rather not", "don't select it", "yes, but I don't want snappyHexMesh",
    "I don't want to use it", "can't use that", "I won't go with it", "absolutely not, thanks",
    "yes, I don't think so", "hmm, not really", "I don't want this engine",
    "don't want that one", "I've changed my mind",
])
def test_a_refusal_is_read_as_one(said):
    # A negation anywhere refuses, whatever assent words sit beside it: "no, don't use
    # snappyHexMesh" contains "use" and is still a no.
    assert es.declines("snappy", said) is True


@pytest.mark.parametrize("said", ["no problem, go ahead", "yes, why not", "sure, no worries"])
def test_an_idiom_that_means_yes_is_not_a_refusal(said):
    assert es.declines("snappy", said) is False


@pytest.mark.parametrize("said", [
    "yes, use snappyHexMesh, but don't worry about mesh density",
    "ok, but do not add a ground patch",
    "yes - the wall is not smooth, by the way",
    "yes, don't go overboard on the layers",
    "yes, I don't need anything else",
    "yes, not sure about the units though",
    "yes, but I don't want that ground patch",
    "sure - I don't want this run to take hours",
    "yes, but not yet on the refinement",
    "yes, I changed my mind about the ground patch",
    "ok - not really sure the inlet is 40 mm, check it",
])
def test_a_negation_aimed_at_another_clause_is_not_a_refusal(said):
    # The user accepted the engine and went on to say something else with a "not" in it. The
    # engine answer is the yes; the negation belongs to the other clause.
    assert es.declines("snappy", said) is False


def test_a_yes_that_goes_on_to_refuse_the_engine_is_never_confirmed_from_its_yes():
    # "yes" is in the message, and so is "I don't want snappyHexMesh". The quote path must not
    # confirm the engine the user went on to reject.
    said = "yes, but I don't want snappyHexMesh"
    c, why = _confirm(said, quote="yes")
    assert c is None and "declined" in why
    assert es.confirm_by_assent(_proposed(), session_id="s", owner_id="u", revision="r2",
                                latest_user_message=said, user_msg_count=2) is None


@pytest.mark.parametrize("said", [
    "yes, but I don't want that ground patch",        # "that" points at the ground patch
    "yes, but not yet on the refinement",              # the idiom is aimed at the refinement
    "yes, I changed my mind about the ground patch",   # so is this one
])
def test_a_yes_that_objects_to_something_else_still_confirms_when_quoted(said):
    c, why = _confirm(said, quote="yes")
    assert why == "" and es.state_of(c) == es.CONFIRMED


def test_a_yes_with_an_unrelated_negation_still_confirms_when_quoted():
    said = "yes, use snappyHexMesh, but don't worry about mesh density"
    assert es.plain_assent("snappy", said) is False        # more than a yes: the model reads it
    c, why = _confirm(said, quote="yes, use snappyHexMesh")
    assert why == "" and es.state_of(c) == es.CONFIRMED


def test_a_negated_selection_is_never_confirmed_from_the_words_beside_the_no():
    # "use snappyHexMesh" is genuinely in the user's message - after "don't". The quote path
    # must not confirm the engine the user just rejected.
    c, why = _confirm("no, don't use snappyHexMesh", quote="use snappyHexMesh")
    assert c is None and "declined" in why
    assert es.confirm_by_assent(_proposed(), session_id="s", owner_id="u", revision="r2",
                                latest_user_message="no, don't use snappyHexMesh",
                                user_msg_count=2) is None


# confirm: the user's yes binds to the engine the question named

@pytest.mark.parametrize("said", ["yes", "ok", "sure", "go with that", "Yes please.", "fine"])
def test_a_plain_yes_confirms_the_proposed_engine_whatever_the_model_quoted(said):
    # The model's quote is not in the user's message - it wrote "Yes, select snappyHexMesh." for a
    # user who wrote "ok". That used to be refused as words the user never wrote, after which the
    # model asked the same question again. The user answered the application's question; the
    # application reads the answer.
    c, why = _confirm(said)
    assert why == "", why
    assert es.state_of(c) == es.CONFIRMED and c["engine"] == "snappy"


@pytest.mark.parametrize("said", ["yes, but use cfmesh", "no, cfMesh", "cfmesh please",
                                  "yes - Gmsh", "ok, switch to cfMesh"])
def test_naming_another_engine_never_confirms_the_proposed_one(said):
    # "yes" is in "yes, but use cfmesh", so a quote of "yes" is genuinely the user's words - and
    # still not a yes to snappyHexMesh. The protection against confirming an engine the user did
    # not pick holds whichever words the model quotes.
    c, why = _confirm(said, quote="yes")
    assert c is None
    assert "different engine" in why and "propose_engine_selection" in why


@pytest.mark.parametrize("said", ["no", "No.", "nope", "not that one"])
def test_a_refusal_never_confirms_even_when_quoted_verbatim(said):
    c, why = _confirm(said, quote=said)
    assert c is None and "declined" in why


@pytest.mark.parametrize("said", ["maybe", "I think so", "what is snappyHexMesh?", "hmm"])
def test_a_hedge_or_question_with_an_invented_quote_is_still_refused(said):
    c, why = _confirm(said)
    assert c is None and "not in the user's latest message" in why


def test_a_hesitant_users_own_words_still_confirm_when_quoted():
    # The quote path is unchanged: the model may quote agreement the grammar does not recognise,
    # and the application checks only that the user actually wrote it.
    said = "snappy is what we use in the group, go with it"
    c, why = _confirm(said, quote="go with it")
    assert why == "" and es.state_of(c) == es.CONFIRMED


def test_assent_answers_only_the_message_right_after_the_question():
    c, why = _confirm("yes", msg_count=3)      # the user said something else since
    assert c is None and "stale" in why


# the application reads the answer before the model runs

def test_confirm_by_assent_reads_a_plain_yes_in_code():
    sel = _proposed()
    got = es.confirm_by_assent(sel, session_id="s", owner_id="u", revision="r2",
                               latest_user_message="ok", user_msg_count=2)
    assert es.state_of(got) == es.CONFIRMED and got["engine"] == "snappy"
    assert got["confirmed_revision"] == "r2" and got["id"] == sel["id"]


@pytest.mark.parametrize("said,count,session", [
    ("use cfmesh", 2, "s"),          # another engine: a new proposal, not a confirmation
    ("no", 2, "s"),                  # declined
    ("maybe", 2, "s"),               # a hedge is the model's to read and quote, never assumed
    ("ok", 3, "s"),                  # stale: the user said something else since
    ("ok", 2, "other"),              # another session's proposal
])
def test_confirm_by_assent_confirms_nothing_else(said, count, session):
    assert es.confirm_by_assent(_proposed(), session_id=session, owner_id="u", revision="r2",
                                latest_user_message=said, user_msg_count=count) is None


def test_confirm_by_assent_never_touches_a_selection_that_is_not_proposed():
    confirmed = es.select_from_structured_input("snappy", session_id="s", owner_id="u",
                                                revision="r1")
    assert es.confirm_by_assent(confirmed, session_id="s", owner_id="u", revision="r2",
                                latest_user_message="yes", user_msg_count=2) is None
    assert es.confirm_by_assent(None, session_id="s", owner_id="u", revision="r2",
                                latest_user_message="yes", user_msg_count=2) is None


# the node, end to end

_PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 40},
            {"name": "outlet", "type": "outlet", "diameter_mm": 60},
            {"name": "wall", "type": "wall"}]
_DECL = {"purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
         "patches": _PATCHES, "engine_params": {"element_order": "2"}}


def _tc(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


_SEEN: list = []


def _run(state, responses):
    import meshpipeline.contracts.model_inference as llm
    it = iter(responses)
    _SEEN.clear()

    async def _call(**kw):
        _SEEN[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict)
                    and m.get("role") == "tool"]
        return next(it)
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


def _state(msg, gate):
    return {"job_id": "j", "session_id": "s", "user_id": "u",
            "messages": [{"role": "assistant", "content": es.render_selection_statement("gmsh")},
                         {"role": "user", "content": msg}],
            "intake_gate": gate}


def _proposed_gmsh():
    # asked before the one user message the state carries
    return es.propose("gmsh", session_id="s", owner_id="u", revision="r-earlier", user_msg_count=0)


def test_a_plain_yes_confirms_in_code_and_unlocks_admission_without_a_confirm_call():
    state = _state("ok", {"selection": _proposed_gmsh(), "admission": None})
    out = _run(state, [
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
        _resp(content="Admission is fine. Fluid and speed? I would go with water at 2 m/s - ok?")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.CONFIRMED and gate["selection"]["engine"] == "gmsh"
    assert gate["admission"] and gate["admission"]["verdict"] == "supported"
    assert "Cannot check admission" not in _SEEN[-1]["content"]
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_a_confirm_call_with_an_invented_quote_after_a_plain_yes_is_accepted_as_done():
    state = _state("sure", {"selection": _proposed_gmsh(), "admission": None})
    out = _run(state, [
        _resp([_tc("confirm_engine_selection",
                   json.dumps({"user_agreed_verbatim": "Yes, select Gmsh."}))]),
        _resp(content="Gmsh it is. What is the flow through the duct?")])
    assert "Confirmed: the user selected Gmsh" in _SEEN[-1]["content"]
    assert "Not confirmed" not in _SEEN[-1]["content"]
    assert out["intake_gate"]["selection"]["state"] == es.CONFIRMED
    assert out["messages"][-1]["content"].startswith("Gmsh it is")


def test_a_user_who_names_another_engine_instead_is_not_confirmed_on_the_proposal():
    state = _state("no, use cfmesh", {"selection": _proposed_gmsh(), "admission": None})
    out = _run(state, [
        _resp([_tc("confirm_engine_selection", json.dumps({"user_agreed_verbatim": "use cfmesh"}))]),
        _resp(content="Which engine would you like, then?")])
    assert "Not confirmed" in _SEEN[-1]["content"]
    assert out["intake_gate"]["selection"]["state"] == es.PROPOSED
    assert out["intake_gate"]["selection"]["engine"] == "gmsh", "the proposal itself is untouched"
