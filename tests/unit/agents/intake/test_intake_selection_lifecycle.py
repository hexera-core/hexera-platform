# Responsibility: Verify an engine is proposed, confirmed, invalidated and expired on its own; only confirmed admits.
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es

_PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 40},
            {"name": "outlet", "type": "outlet", "diameter_mm": 60},
            {"name": "wall", "type": "wall"}]
_DECL = {"purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
         "patches": _PATCHES, "engine_params": {"element_order": "2"}}
_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion. " * 2)


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


def _state(msg, gate=None):
    s = {"job_id": "j", "session_id": "s", "user_id": "u",
         "messages": [{"role": "user", "content": msg}]}
    if gate is not None:
        s["intake_gate"] = gate
    return s


# the lifecycle unit

def test_lifecycle_states_and_expiry():
    assert es.state_of(None) == es.NO_SELECTION
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    assert es.state_of(p) == es.PROPOSED
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                        quote="yes, select gmsh", latest_user_message="Yes, select gmsh.")
    assert why == "" and es.state_of(c) == es.CONFIRMED
    assert es.state_of({**c, "expires_at": time.time() - 1}) == es.NO_SELECTION


def test_confirmation_requires_words_the_user_actually_wrote():
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                        quote="yes use gmsh", latest_user_message="Actually, what about cfmesh?")
    assert c is None and "not in the user's latest message" in why


def test_a_new_user_revision_leaves_a_pending_selection_stale():
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r4", user_msg_count=3,
                        quote="yes", latest_user_message="yes")
    assert c is None and "stale" in why


def test_only_a_confirmed_selection_authorizes_admission():
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    assert es.verify_confirmed(None, "gmsh", session_id="s", owner_id="u")[0] is False
    assert es.verify_confirmed(p, "gmsh", session_id="s", owner_id="u")[0] is False   # proposed only
    c, _ = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                      quote="yes", latest_user_message="yes")
    assert es.verify_confirmed(c, "gmsh", session_id="s", owner_id="u")[0] is True
    assert es.verify_confirmed(c, "cfmesh", session_id="s", owner_id="u")[0] is False  # other engine
    assert es.verify_confirmed(c, "gmsh", session_id="other", owner_id="u")[0] is False


# the explicit selection flow, end to end

def test_naming_an_engine_only_proposes_it_and_renders_the_canonical_statement():
    state = _state("Use gmsh.")
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="never reached - the app asks for confirmation")])
    reply = out["messages"][-1]["content"]
    # the statement shows the name a user reads; `gmsh` is the internal key
    assert "Selected engine: Gmsh" in reply
    assert "not a selection" in reply and "nothing will be meshed" in reply
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.PROPOSED and gate["selection"]["engine"] == "gmsh"
    assert gate["admission"] is None
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_confirming_establishes_selection_and_unlocks_a_fresh_admission_preview():
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision="r-earlier",
                          user_msg_count=0)   # the state below carries exactly one user message
    state = _state("Yes, select gmsh.", gate={"selection": proposed, "admission": None})
    out = _run(state, [
        _resp([_tc("confirm_engine_selection", json.dumps({"user_agreed_verbatim": "Yes, select gmsh."}))]),
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
        _resp(content="Admission is fine - now, what are the flow conditions?")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.CONFIRMED
    assert gate["admission"] and gate["admission"]["verdict"] == "supported"
    assert gate["admission"]["selection_id"] == gate["selection"]["id"]
    # a supported preview still does NOT submit or dispatch anything
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_requirements_still_need_canonical_approval_after_a_confirmed_selection():
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u",
                                          revision=at.revision_of([{"role": "user", "content": "go on"}]))
    state = _state("go on", gate={"selection": sel, "admission": None})

    def _submit_with_token():
        tok = json.loads(_SEEN[-1]["content"])["preview_token"]
        return _resp([_tc("submit_requirements", json.dumps({
            "domain": "duct internal", "request_txt": _R, "review_brief_txt": _B,
            "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
            "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
            "engine_params": {"element_order": "2"}, "patches": _PATCHES, "preview_token": tok}))])

    import meshpipeline.contracts.model_inference as llm
    seq = [_resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
           _submit_with_token, _resp(content="unreached")]
    it = iter(seq)
    _SEEN.clear()

    async def _call(**kw):
        _SEEN[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict)
                    and m.get("role") == "tool"]
        nxt = next(it)
        return nxt() if callable(nxt) else nxt
    with patch.object(llm, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))

    reply = out["messages"][-1]["content"]
    assert out.get("request_txt"), "the submission was authorized"
    assert "confirm the requirements" in reply and "Shall I proceed" in reply   # app-rendered
    assert out.get("dispatch_confirmed") is False, "approval is a separate user turn"


def test_admission_is_refused_without_a_confirmed_selection():
    for gate in (None,
                 {"selection": es.propose("gmsh", session_id="s", owner_id="u", revision="r1",
                                          user_msg_count=1),
                  "admission": None}):
        state = _state("My duct has three patches.", gate=gate)
        out = _run(state, [
            _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
            _resp(content="Which engine would you like to use?")])
        assert (out["intake_gate"].get("admission")) is None, "no token without a confirmed selection"
        assert "Cannot check admission" in _SEEN[-1]["content"]


def test_admission_is_refused_for_an_engine_other_than_the_confirmed_one():
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r1")
    state = _state("go on", gate={"selection": sel, "admission": None})
    out = _run(state, [
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "cfmesh", **_DECL}))]),
        _resp(content="I need you to confirm the engine change first.")])
    assert out["intake_gate"].get("admission") is None
    assert "the confirmed engine is 'gmsh'" in _SEEN[-1]["content"]


# correction invalidates selection, preview and pending confirmation together

def test_an_engine_correction_invalidates_the_previous_selection_and_its_admission():
    old_sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r1")
    old_tok = at.issue(session_id="s", owner_id="u", revision="r1",
                       canonical=at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                                                      _PATCHES, {"element_order": "2"}),
                       verdict="supported", mode=at.SELECTED, selection_id=old_sel["id"])
    state = _state("Actually, switch the engine to cfmesh.",
                   gate={"selection": old_sel, "admission": old_tok})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
                       _resp(content="unreached")])
    gate = out["intake_gate"]
    assert gate["selection"]["engine"] == "cfmesh" and gate["selection"]["state"] == es.PROPOSED
    assert gate["selection"]["id"] != old_sel["id"]
    assert gate["admission"] is None, "the correction must void the previous admission preview"
    assert "Selected engine: cfMesh" in out["messages"][-1]["content"]
    assert out.get("dispatch_confirmed") is False


def test_a_stale_token_from_the_previous_selection_cannot_dispatch():
    old_sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r1")
    tok = at.issue(session_id="s", owner_id="u", revision="r1",
                   canonical=at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                                                  _PATCHES, {"element_order": "2"}),
                   verdict="supported", mode=at.SELECTED, selection_id=old_sel["id"])
    new_sel = es.select_from_structured_input("cfmesh", session_id="s", owner_id="u", revision="r2")
    ok, why = at.verify_for_confirm(tok, tok["token"], selection=new_sel)
    assert ok is False and "selection changed" in why


# the question must be answerable - see the commit that added these


def test_a_yes_to_the_standing_question_selects_it_without_the_model_quoting_anything():
    # The whole defect in one test: the user answers the question on their screen, the model calls
    # propose again and passes no `user_named_verbatim`, and this used to re-ask - spending the
    # answer on the question instead of on the selection.
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision="r", user_msg_count=0)
    state = _state("yes", gate={"selection": proposed, "admission": None})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="Good - now the flow conditions.")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.CONFIRMED
    assert gate["selection"]["id"] == proposed["id"], "the standing question was answered, not replaced"
    assert "not a selection" not in out["messages"][-1]["content"], "the question must not come back"


def test_the_same_engine_is_never_put_to_the_user_twice():
    # A question still waiting for its answer is not asked again. Repeating it is what consumed the
    # pending proposal and made the conversation unable to move.
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision="r", user_msg_count=0)
    state = _state("hold on, what does that change?",
                   gate={"selection": proposed, "admission": None})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="It changes how the boundary layers are built.")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.PROPOSED, "a question is not an answer"
    assert gate["selection"]["id"] == proposed["id"]
    assert "Selected engine" not in out["messages"][-1]["content"]


def test_proposing_the_engine_already_selected_does_not_discard_the_selection():
    # Re-proposing used to overwrite a CONFIRMED selection with a fresh question, throwing away
    # consent this application had already recorded and verified.
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r")
    state = _state("ok", gate={"selection": sel, "admission": None})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="Still gmsh - what are the flow conditions?")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.CONFIRMED
    assert gate["selection"]["id"] == sel["id"], "the recorded selection must survive"


def test_a_question_about_an_engine_still_selects_nothing():
    # The proof requirement is unchanged. Naming an engine while asking about it is not choosing it,
    # whether or not a question about that engine is outstanding.
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision="r", user_msg_count=0)
    state = _state("what is gmsh?", gate={"selection": proposed, "admission": None})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="It is a tetrahedral volume mesher.")])
    assert out["intake_gate"]["selection"]["state"] == es.PROPOSED


def test_a_confirmation_the_model_failed_to_quote_is_still_the_users_confirmation():
    # The sibling of the propose defect, and the one that kept a real conversation stuck after the
    # first was fixed: confirm_engine_selection read ONLY the model's quote, so a paraphrased or
    # invented quote hid a confirmation the user had plainly written.
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                        quote="the user agreed to gmsh", latest_user_message="yes, select gmsh")
    assert why == "" and es.state_of(c) == es.CONFIRMED


def test_a_quote_the_user_never_wrote_still_confirms_nothing():
    # The proof requirement is unchanged: neither the model's words nor a message that does not
    # answer the question can select an engine.
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                        quote="yes use gmsh", latest_user_message="Actually, what about cfmesh?")
    assert c is None and "have not confirmed" in why


def test_the_budget_nudge_is_not_the_customer_speaking():
    # THE reason a real conversation could not select an engine at all. Past MAX_TURNS the
    # application appends its own BUDGET_NUDGE to the transcript as a `user` turn, and every check
    # that asks "did the customer say this?" then read that text instead of theirs.
    import meshpipeline.agents.intake.turn as turn

    msgs = [{"role": "user", "content": "yes, select snappyHexMesh"},
            {"role": "assistant", "content": "Selected engine: snappyHexMesh ..."}]
    _llm, nudged = turn.apply_budget_nudge([], msgs)
    assert nudged[-1]["content"] == turn.BUDGET_NUDGE and nudged[-1]["role"] == "user"

    ctx = turn.hydrate({"job_id": "j", "session_id": "s", "user_id": "u"}, nudged)
    assert ctx.latest_user_msg == "yes, select snappyHexMesh", "the customer's words, not ours"
    assert ctx.user_msg_count == 1, "a synthetic turn must not make the pending question stale"
    assert es.quote_is_from_user("yes, select snappyHexMesh", ctx.latest_user_msg)


def test_handing_over_the_choice_is_asking_for_a_recommendation():
    # A customer who says "you decide" was told "would you like engine recommendations?" - the one
    # person who had already said they did not want to be asked. Three round trips for one choice.
    import meshpipeline.agents.intake.recommendation as rec

    for said in ("u decide best case", "you decide", "you tell me", "your call", "up to you",
                 "whatever you think", "best option", "doesn't matter", "pick for me"):
        assert rec.choice_deferred(said), said
        assert rec.recommendation_requested(said), said

    # Deferring a CHOICE, not being ignorant of a FACT. These are how someone answers "what
    # velocity?", and reading them as "pick an engine for me" volunteers alternatives nobody asked
    # for - the false positive this module's own header warns about.
    for said in ("i dont know", "no idea", "not sure", "what velocity is it", "yes"):
        assert not rec.choice_deferred(said), said

    # the original phrasing still works and is still not a deferral
    assert rec.recommendation_requested("which engine should I use")
    assert not rec.choice_deferred("which engine should I use")


def test_the_turn_carries_whether_the_customer_handed_over_the_choice():
    import meshpipeline.agents.intake.turn as turn

    ctx = turn.hydrate({"job_id": "j", "session_id": "s", "user_id": "u"},
                       [{"role": "user", "content": "you decide, whatever's best"}])
    assert ctx.choice_deferred is True and ctx.rec_authorized is True
    ctx2 = turn.hydrate({"job_id": "j", "session_id": "s", "user_id": "u"},
                        [{"role": "user", "content": "use gmsh"}])
    assert ctx2.choice_deferred is False


def test_an_instruction_to_proceed_is_not_another_question():
    # "use best practice" and "go ahead with what you decided" are a customer telling us to get on
    # with it. Read as anything else they become one more question, which is precisely what a
    # customer who wrote them is trying to avoid.
    import meshpipeline.agents.intake.recommendation as rec

    for said in ("use best practice", "best practice", "go ahead with what you decided",
                 "whatever is standard", "your best call", "what you think is best",
                 "intenral cfd and u decide patches"):
        assert rec.choice_deferred(said), said
        assert rec.recommendation_requested(said), said

    # a real answer is not a deferral - these carry information and must be read as answers
    for said in ("o1 is inlet", "through the bore and o1 is inlet", "others are outlet",
                 "air", "500 mm"):
        assert not rec.choice_deferred(said), said

    # "carry on" is not "you choose". These are among the commonest things anyone types, and
    # reading them as a request for alternatives makes the gate fail open on ordinary words.
    for said in ("proceed", "go ahead", "just go", "carry on"):
        assert not rec.choice_deferred(said), said


def test_deferring_to_an_engine_we_have_already_proposed_accepts_it():
    # Driving a real conversation, "you decide" was answered "I can't answer this one for you"
    # SEVEN times in a row and the run never reached a mesh. The question on screen names ONE
    # engine, and a customer answering it with "you decide" has delegated that decision twice.
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision="r", user_msg_count=0)
    state = _state("you decide", gate={"selection": proposed, "admission": None})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="Gmsh it is - what are the flow conditions?")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.CONFIRMED
    assert gate["selection"]["id"] == proposed["id"]


def test_deferring_settles_the_engine_the_model_is_proposing():
    # THIS TEST ONCE ASSERTED THE OPPOSITE, and the change was deliberate. It pinned "a deferral
    # with no question on screen selects nothing", on the reasoning that a deferral needs something
    # to accept. But the fresh-proposal case IS the customer's own deferral being acted on: they
    # said "you decide", the model picked, and showing them "Selected engine: X - do you want X?"
    # asks them to make the decision they just handed over. That round trip bought nothing.
    #
    # What replaced the property is not nothing. The engine is named in the setup block they confirm
    # before anything is submitted, and the dispatch asks again after that, so it is still theirs to
    # stop - and everything below still confirms nothing at all.
    assert es.answers_the_selection_question("gmsh", "", "you decide", outstanding=False) is True
    assert es.answers_the_selection_question("gmsh", "", "you decide", outstanding=True) is True

    for said in ("what is gmsh?", "no", "not yet", "use cfmesh instead", "Use gmsh."):
        assert es.answers_the_selection_question("gmsh", "", said, outstanding=False) is False, said


def test_the_words_people_actually_use_to_agree_are_read_as_agreement():
    # "go" was missing, and "go" is the commonest way this product's owner accepts a proposal: a
    # driven conversation answered the engine question with "go" five times running and was told
    # each time to say yes or no. A word list without the customer's words is a wall, not a reader.
    for said in ("go", "go ahead", "yes", "yep", "ok", "okayyyyyyyyy", "yees", "sure",
                 "looks good", "send it", "lock it in", "fine", "perfect"):
        assert es.affirms(said), said

    # and the half that has to keep working, or it is not a check
    for said in ("no", "nope", "don't go", "not yet", "wait", "use cfmesh instead",
                 "what is it?", "yes but a different one"):
        assert not es.affirms(said), said

