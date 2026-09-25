# Responsibility: Verify an impossibility caused by OUR OWN engine pick, under the customer's delegation,
# is fixed by the application instead of handed back - and that an engine the CUSTOMER named still is not.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
import meshpipeline.agents.intake.recommendation as rec
from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
from meshpipeline.agents.intake.validation import ADMIT_IMPOSSIBLE, ADMIT_SUPPORTED, preview_admission

# THE MEASURED DEAD END, reduced to its two values. Internal CFD from a Fluid domain geometry is
# `impossible` on cfMesh, snappyHexMesh, snappyHexMesh multi-region and VMTK, and needs one more value
# on Gmsh. Neither value was the customer's: the geometry kind is the model's reading of their file and
# snappyHexMesh is the model's own pick.
_PURPOSE, _KIND = "internal_cfd", "fluid-domain"
_PORTS = [{"name": "pipe", "type": "wall"},
          {"name": "in", "type": "inlet", "diameter_mm": 40.0},
          {"name": "out", "type": "outlet", "diameter_mm": 25.0}]
_PREVIEW = {"selected_engine": "snappy", "purpose": _PURPOSE, "input_kind": _KIND,
            "dimensionality": "3D", "patches": _PORTS}

_DELEGATED = "internal cfd, air through it - you decide everything"
_NAMED_IT = "internal cfd, air through it - use snappyHexMesh"


def _state(engine: str, *, said: tuple[str, ...], chosen_by: str,
           deferred: bool = True) -> IntakeExecutionState:
    sel = es.propose(engine, session_id="s", owner_id="u", revision="r1", user_msg_count=len(said),
                     chosen_by=chosen_by)
    sel = {**sel, "state": es.CONFIRMED, "confirmed_revision": "r1"}
    return IntakeExecutionState(
        session_id="s", owner_id="u", revision="r1", user_msg_count=len(said),
        latest_user_msg=said[-1], choice_deferred=deferred, selection=sel,
        user_messages=said, customer_messages=said)


def _preview(st: IntakeExecutionState, args: dict | None = None):
    ex = IntakeToolExecutor(state=st, job_id="j",
                            implemented_engines=list(intake._IMPLEMENTED_ENGINES),
                            search_tool=lambda *a, **k: "")
    return asyncio.run(ex.run("preview_selected_admission", dict(args or _PREVIEW)))


# the facts the row now carries


def test_the_row_records_who_chose_the_engine_and_nothing_defaults_to_us():
    # The customer's own words name it: theirs.
    assert es.who_chose("snappy", quote="", latest_user_message=_NAMED_IT,
                        user_messages=(_NAMED_IT,)) == es.CHOSEN_BY_CUSTOMER
    # NAMED IN AN EARLIER MESSAGE IS STILL NAMED. A choice does not expire any more than a delegation
    # does, and this is the reading under which nothing changes - so it must be reachable from history.
    assert es.who_chose("snappy", quote="", latest_user_message="go",
                        user_messages=(_NAMED_IT, "go")) == es.CHOSEN_BY_CUSTOMER
    # They said only what it is for: ours.
    assert es.who_chose("snappy", quote="", latest_user_message="go",
                        user_messages=(_DELEGATED, "go")) == es.CHOSEN_BY_US
    # THE MODEL'S OWN PROSE IS NOT THE CUSTOMER NAMING IT. A quote that is not in what they wrote is
    # already refused by `user_named_engine`, and this is the field that would otherwise launder it.
    assert es.who_chose("snappy", quote="I'll use snappyHexMesh", latest_user_message="go",
                        user_messages=(_DELEGATED, "go")) == es.CHOSEN_BY_US
    # "" IS A THIRD VALUE AND IT IS NOT "us": a selection stored before this field existed says nothing.
    assert es.chosen_by({"engine": "snappy"}) == ""
    assert es.chosen_by(None) == ""
    assert es.chosen_by(es.propose("snappy", session_id="s", owner_id="u", revision="r",
                                   user_msg_count=1)) == ""
    # The typed engine field the customer submitted themselves is them naming it.
    assert es.chosen_by(es.select_from_structured_input(
        "snappy", session_id="s", owner_id="u", revision="r")) == es.CHOSEN_BY_CUSTOMER


def test_the_engine_that_needs_one_more_value_is_in_the_admissible_set():
    # The blind spot this set exists to avoid: every OTHER engine is impossible here, and the one that
    # can do the job is `incomplete` rather than `supported`, so a set built from ADMIT_SUPPORTED alone
    # is EMPTY and the case reads as impossible while a working engine sits one field short.
    supported = [e for e in intake._IMPLEMENTED_ENGINES
                 if preview_admission(e, _PURPOSE, _KIND, dimensionality="3D",
                                      patches=_PORTS)["verdict"] == ADMIT_SUPPORTED]
    assert supported == [], supported
    can = rec.admissible_engines(_PURPOSE, _KIND, dimensionality="3D", patches=_PORTS,
                                 exclude=("snappy",))
    assert can == ["gmsh"], can
    # and it is genuinely the catalog answering, not this function: with the value supplied, Gmsh is
    # SUPPORTED and the set is unchanged.
    assert rec.admissible_engines(_PURPOSE, _KIND, dimensionality="3D", patches=_PORTS,
                                  engine_params={"element_order": "2"},
                                  exclude=("snappy",)) == ["gmsh"]


# the replacement itself


def test_our_own_pick_that_cannot_mesh_is_replaced_and_the_turn_is_not_a_dead_end():
    st = _state("snappy", said=(_DELEGATED, "go"), chosen_by=es.CHOSEN_BY_US)
    result = _preview(st)
    assert st.admission_block is None, "the turn still ended in the application's dead end"
    body = json.loads(result.content)
    assert body["engine_changed_from"] == "snappyHexMesh"
    assert body["engine_changed_to"] == "Gmsh"
    assert body["why_it_changed"], "the model was not told why its own pick was refused"
    assert st.selection["engine"] == "gmsh"
    assert es.state_of(st.selection) == es.CONFIRMED, "the replacement needs a second yes to be usable"
    # STILL OURS, so a second impossibility on it is swapped again rather than handed back.
    assert es.chosen_by(st.selection) == es.CHOSEN_BY_US
    # NOTHING OF THE CUSTOMER'S MOVED. The only value that changed is the one nobody chose but us.
    assert body["verdict"] != ADMIT_IMPOSSIBLE
    assert _PREVIEW["purpose"] == _PURPOSE and _PREVIEW["input_kind"] == _KIND
    assert [p["name"] for p in _PORTS] == ["pipe", "in", "out"]


def test_the_replaced_engine_reaches_a_token_once_its_own_value_is_given():
    st = _state("snappy", said=(_DELEGATED, "go"), chosen_by=es.CHOSEN_BY_US)
    first = json.loads(_preview(st).content)
    # Gmsh wants `element_order`, which is a value to gather - so the first answer is not a token, and
    # it says what is missing rather than that the job is impossible.
    assert first["verdict"] != ADMIT_IMPOSSIBLE and "preview_token" not in first
    second = json.loads(_preview(st, {**_PREVIEW, "selected_engine": "gmsh",
                                      "engine_params": {"element_order": "2"}}).content)
    assert second["verdict"] == ADMIT_SUPPORTED and second["preview_token"]


# and every case in which nothing may change


def test_an_engine_the_customer_named_is_never_replaced():
    st = _state("snappy", said=(_NAMED_IT, "go"), chosen_by=es.CHOSEN_BY_CUSTOMER)
    result = _preview(st)
    assert st.admission_block, "an engine the customer named was quietly swapped"
    assert st.selection["engine"] == "snappy"
    assert "revise" in result.content


def test_a_customer_who_never_handed_over_the_choice_is_still_asked():
    st = _state("snappy", said=("internal cfd, air through it", "go"),
                chosen_by=es.CHOSEN_BY_US, deferred=False)
    _preview(st)
    assert st.admission_block, "the engine was changed with no delegation to change it under"
    assert st.selection["engine"] == "snappy"


def test_an_engine_they_named_is_not_ours_even_after_we_replaced_it_ourselves():
    """MEASURED live on shell_and_tube_7_unshared. The customer said "use snappyHexMesh" and then "you
    decide everything else"; the catalog refused snappyHexMesh for their file, the model was told "go"
    six times, and on turn 8 it proposed cfMesh ITSELF. The row then said "ours" about cfMesh - which is
    true - and swapping on from there moved the customer off an engine they had named. `chosen_by` answers
    who chose what is on the row; this answers whether the choice was ever ours to make."""
    st = _state("cfmesh", said=(_NAMED_IT, "you decide everything else", "go"),
                chosen_by=es.CHOSEN_BY_US)
    assert es.chosen_by(st.selection) == es.CHOSEN_BY_US, "the row does say the pick was ours"
    _preview(st, {**_PREVIEW, "selected_engine": "cfmesh"})
    assert st.admission_block, "the swap helped the model past an engine the customer had named"
    assert st.selection["engine"] == "cfmesh"


def test_a_selection_from_before_the_field_existed_is_not_ours_to_change():
    st = _state("snappy", said=(_DELEGATED, "go"), chosen_by="")
    assert es.chosen_by(st.selection) == ""
    _preview(st)
    assert st.admission_block, "an unrecorded provenance was read as ours"
    assert st.selection["engine"] == "snappy"


def test_when_no_engine_can_do_it_the_dead_end_is_real_and_is_handed_back():
    # Internal CFD from a Solid assembly: every registered engine refuses it, so there is nothing to
    # swap to and the finding is the customer's to answer - exactly as before this existed.
    args = {**_PREVIEW, "input_kind": "solid-assembly"}
    assert rec.admissible_engines("internal_cfd", "solid-assembly", dimensionality="3D",
                                  patches=_PORTS, exclude=("snappy",)) == []
    st = _state("snappy", said=(_DELEGATED, "go"), chosen_by=es.CHOSEN_BY_US)
    _preview(st, args)
    assert st.admission_block, "a real impossibility stopped being reported"
    assert st.selection["engine"] == "snappy"


# the whole turn, through node_intake


def _call(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=c.id, name=c.function.name, arguments=c.function.arguments)
                         for c in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion for this case. " * 2)


def test_the_whole_turn_submits_instead_of_asking_them_to_revise_what_they_never_chose():
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": _DELEGATED}, {"role": "user", "content": "go"}]}
    rounds = iter([
        # the model picks the mesher itself, on their delegation, and previews it
        _resp([_call("propose_engine_selection", {"engine": "snappyHexMesh"}),
               _call("preview_selected_admission", _PREVIEW)]),
        # told its own pick cannot do this and that Gmsh now is the selection, it carries on IN THE
        # SAME TURN with every value the customer declared untouched
        _resp([_call("preview_selected_admission", {**_PREVIEW, "selected_engine": "Gmsh",
                                                    "engine_params": {"element_order": "2"}})]),
        _resp(content="TOKEN"),
    ])

    async def _model(**kw):
        nxt = next(rounds)
        if nxt.assistant_text != "TOKEN":
            return nxt
        token = ""
        for m in kw["messages"]:
            if m.get("role") == "tool" and "preview_token" in str(m.get("content")):
                token = json.loads(m["content"])["preview_token"]
        return _resp([_call("submit_requirements", {
            "domain": "pipe internal flow", "request_txt": _R, "review_brief_txt": _B,
            "dimensionality": "3D", "purpose": _PURPOSE, "input_kind": _KIND,
            "mesh_engine": "Gmsh", "mesh_fidelity": "standard", "engine_source": "suggested_confirmed",
            "engine_params": {"element_order": "2"}, "patches": _PORTS,
            "preview_token": token})], content="Switched to Gmsh - snappyHexMesh cannot mesh a fluid "
                                               "domain for internal flow. Everything else stands.")

    async def _measured(_s):
        return intake._GeometryReading(reading=None)

    import meshpipeline.adapters.model_inference.router as llm_router
    with patch.object(llm_router, "call_intake_model", _model), \
            patch.object(intake, "_geometry_reading", _measured):
        out = asyncio.run(intake.node_intake(state))

    assert out.get("mesh_engine") == "gmsh", out.get("mesh_engine")
    assert out.get("purpose") == _PURPOSE and out.get("input_kind") == _KIND
    assert [p["name"] for p in out.get("intake_patches") or []] == ["pipe", "in", "out"]
    reply = out["messages"][-1]["content"]
    assert "cannot" not in reply.split("Switched")[0]
    assert "revise" not in reply.lower(), reply
    assert at.CONFIRM_REQUIREMENTS_ASK in reply
