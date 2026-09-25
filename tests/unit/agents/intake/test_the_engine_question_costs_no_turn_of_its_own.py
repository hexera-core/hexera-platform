"""The engine question replaced the turn, so it needed a turn of its own.

MEASURED on a structural run of ahmed_variant_001: turn 2 said "here's what I'm going with... Anything
to change, or shall I go?" and the customer said "go". Turn 3 was then

    Selected engine: Gmsh
    This is a proposal, not a selection - nothing has been selected yet and nothing will be meshed.
    Do you want to select Gmsh?

on its own, after they had already said go. The cause is structural, not a prompt failing: whichever
round called `propose_engine_selection` had its composed words thrown away, because `close_out` made
the terminal text the whole payload. So the model could not put the proposal and the engine in one
reply, and split them across two.

The rule this breaks is a stated one: ask "anything to change, or shall I go?" once, and the next word
meshes.
"""
from __future__ import annotations

import asyncio

from meshpipeline.agents.intake.engine_selection import render_selection_statement
from meshpipeline.agents.intake.executor import IntakeExecutionState
from meshpipeline.agents.intake.loop_policy import TERMINAL_PRIORITY, IntakeLoopPolicy


def _resolve(said: str, **terminals) -> str:
    st = IntakeExecutionState(owner_id="o", session_id="s")
    for k, v in terminals.items():
        setattr(st, k, v)
    pol = IntakeLoopPolicy(exec_state=st, executor=None)
    pol.plaintext_text = said
    out = asyncio.run(pol.close_out(tally=None))
    return "" if out is None else str(out.payload)


PROPOSAL = ("Here's what I'm going with:\n- Load face -> the x max lip\n"
            "- Elements -> second-order tets")


def test_the_proposal_and_the_engine_question_arrive_in_one_reply():
    out = _resolve(PROPOSAL, selection_prompt=render_selection_statement("gmsh"))
    assert "Load face" in out, "the turn's own words were thrown away, which is what cost the turn"
    assert "Selected engine: Gmsh" in out
    assert out.index("Load face") < out.index("Selected engine"), "the ask goes last"


def test_the_question_still_stands_alone_when_the_model_wrote_nothing():
    out = _resolve("", selection_prompt=render_selection_statement("gmsh"))
    assert out.strip().startswith("Selected engine: Gmsh")


def test_an_admission_block_still_replaces_the_turn():
    """It voids the authorization it reports on, so the model's words must not survive beside it."""
    out = _resolve(PROPOSAL, admission_block="Cannot mesh: the part declares no unit.")
    assert "Load face" not in out
    assert out == "Cannot mesh: the part declares no unit."


def test_an_admission_block_outranks_the_engine_question_as_before():
    out = _resolve(PROPOSAL, admission_block="Cannot mesh: no unit.",
                   selection_prompt=render_selection_statement("gmsh"))
    assert out == "Cannot mesh: no unit."
    assert TERMINAL_PRIORITY.index("admission_block") < TERMINAL_PRIORITY.index("selection_prompt")


def test_the_statement_no_longer_announces_and_denies_a_selection_at_once():
    said = render_selection_statement("snappy")
    assert "Selected engine:" in said, "the deterministic marker is read by four tests and the prompt"
    assert "nothing has been selected yet" not in said, (
        "it announced a selection and denied one in consecutive lines")
    assert "Nothing is meshed until you say so." in said, "the guarantee is worth one clause"
    assert said.count("?") == 1, "one ask"


# AND THE LAST SCREEN BEFORE COMPUTE NAMES THE MESHER, which it never did.
#
# `answers_the_selection_question` accepts "you decide" against a FRESH proposal - re-asking a
# decision the customer just handed over is a question with one answer - and it justifies that in
# writing: "the engine is named in the setup block they confirm before anything is submitted".
#
# MEASURED on a structural run: the engine was confirmed from "you decide", the application's own
# "Selected engine: X" was never shown, the only place "Gmsh" appeared was a sentence the MODEL wrote,
# and the pre-dispatch confirmation named no mesher at all. The guarantee that justifies skipping the
# question was being kept by nothing.

def test_the_confirmation_names_the_engine_the_platform_will_actually_run():
    from meshpipeline.agents.intake import engine_selection as es
    assert es.engine_label("gmsh") == "Gmsh", "the label the customer reads"


def test_the_engine_is_read_off_the_confirmed_selection_not_the_model_s_arguments():
    """A model that wrote a different name upstream must be contradicted here, not agreed with."""
    import inspect

    from meshpipeline.agents.intake import executor as ex

    src = inspect.getsource(ex.IntakeToolExecutor)
    i = src.index("MESHING WITH")
    window = src[i - 400:i + 400]
    assert 'st.selection or {}).get("engine")' in window, (
        "the engine on this screen must be the confirmed selection, not args")
    assert 'args.get("engine")' not in window

# AND THE SUMMARY IS THE SAME CASE, WHICH COST THE OTHER TURN.
#
# `close_out` made `submit_summary` the whole payload too, so the setup the customer is being asked to
# confirm was deleted from the turn that asks them to confirm it. MEASURED on ahmed_variant_001, the
# whole of what turn 5 of six showed: "MESHING WITH snappyHexMesh", the geometry agent's findings, five
# risks, "Please confirm the requirements above before I mesh anything." - a sentence pointing back at
# the risks, with the ports, the fluid, the y+ treatment and the engine's reason nowhere on screen. The
# prompt file's SOFT LIMITATIONS rule is the second guarantee it broke: the "Heads-up:" line it requires
# after submit_requirements and before asking to proceed reached nobody, ever.

SUMMARY = (chr(10) * 2).join([
    "MESHING WITH snappyHexMesh",
    "WHAT THE GEOMETRY AGENT FOUND" + chr(10) + "Concluded: a wall-shell passage.",
    "Please confirm the requirements above before I mesh anything.",
    "Shall I proceed with mesh generation?"])


def test_the_setup_and_the_confirmation_arrive_in_one_reply():
    """"confirm the requirements above" has to have requirements above it, and this is the only place they
    can be: the application does not compose the setup, the model does."""
    out = _resolve(PROPOSAL, submit_summary=SUMMARY)
    assert "Load face" in out, "the setup the customer is asked to confirm was thrown away"
    assert out.index("Load face") < out.index("Please confirm the requirements above")
    assert out.rstrip().endswith("Shall I proceed with mesh generation?"), "the ask goes last"


def test_the_summary_still_stands_alone_when_the_model_wrote_nothing():
    out = _resolve("", submit_summary=SUMMARY)
    assert out.strip().startswith("MESHING WITH snappyHexMesh")


def test_a_new_engine_selection_still_supersedes_a_submission_in_the_same_batch():
    """The priority is unchanged: a customer choosing again outranks a submission, and the submission's
    arguments are dropped rather than delivered."""
    st = IntakeExecutionState(owner_id="o", session_id="s")
    st.submit_summary, st.submit_args = SUMMARY, {"purpose": "internal_cfd"}
    st.selection_prompt = render_selection_statement("gmsh")
    pol = IntakeLoopPolicy(exec_state=st, executor=None)
    pol.plaintext_text = PROPOSAL
    out = asyncio.run(pol.close_out(tally=None))
    assert "Selected engine: Gmsh" in str(out.payload)
    assert "Please confirm the requirements above" not in str(out.payload)
    assert st.submit_args is None and st.submit_summary is None


# A DELEGATION DOES NOT EXPIRE, AND READING IT AS A ONE-TURN UTTERANCE COST THE ENGINE ITS OWN TURN.


def _es_answers(engine: str, latest: str, earlier) -> bool:
    from meshpipeline.agents.intake import engine_selection as es
    return es.answers_the_selection_question(engine, "", latest, outstanding=False,
                                             earlier_user_messages=earlier)


def test_a_delegation_given_two_turns_ago_still_accepts_the_engine_we_propose():
    """MEASURED on ahmed_variant_001 with the script "cfd" / "the fluid is air, everything else you decide"
    / "go": by the time the engine was proposed the delegation was two messages back and the latest message
    was the bare "go", so nothing read it - and turn 4 of six was "Selected engine: snappyHexMesh ... Go
    with snappyHexMesh?" on its own, after the customer had already said go to the setup.

    Narrow in the same way `geometry_survey.said_by_customer` is: only a DEFERRAL may be read from an
    earlier message.
    """
    from meshpipeline.agents.intake import engine_selection as es
    earlier = ("cfd", "the fluid is air, everything else you decide", "go")
    assert not es.answers_the_selection_question("snappy", "", "go", outstanding=False)
    assert es.answers_the_selection_question("snappy", "", "go", outstanding=False,
                                             earlier_user_messages=earlier)


def test_an_earlier_refusal_is_never_spent_as_agreement_to_an_engine():
    """Only a deferral may be read from an earlier message, so two turns of "no" settle nothing - and the
    third message, which IS a deferral, settles it, which is what makes the first assertion about the
    refusals rather than about the reader being switched off."""
    earlier = ("not that one", "no, something else", "whatever's best")
    assert not _es_answers("snappy", "what does it cost?", earlier[:2])
    assert _es_answers("snappy", "what does it cost?", earlier)
