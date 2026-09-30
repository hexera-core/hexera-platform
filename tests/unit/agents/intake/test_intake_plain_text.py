# Responsibility: Verify the intake is told to write plain engineering text, and that LaTeX it writes anyway reaches the user plain.
# Boundaries: the prompt block and the one place the model's reply enters the turn; the normaliser's own rules live in tests/unit/contracts.
from __future__ import annotations

import asyncio
from unittest.mock import patch

import meshpipeline.agents.intake.agent as intake
from meshpipeline.agents.intake.loop_policy import repeats_a_question
from meshpipeline.contracts.model_inference import ModelRoundResult

# a reply the intake really wrote (rehearsal, 2026-09-29), with a patch name added in backticks
_LATEX_REPLY = ("For the remaining external-CFD setup, shall I use **air at 30 m/s**, sea-level "
                "properties, **k–ω SST with wall functions targeting \\(y^+=30\\text{–}300\\)**, and "
                "the wall patch `wing_1`? Reply ok or tell me what differs.")
_PLAIN_REPLY = ("For the remaining external-CFD setup, shall I use **air at 30 m/s**, sea-level "
                "properties, **k–ω SST with wall functions targeting y⁺ = 30–300**, and "
                "the wall patch `wing_1`? Reply ok or tell me what differs.")


def _run(state, replies):
    import meshpipeline.contracts.model_inference as llm
    it = iter(replies)

    async def _call(**kw):
        return ModelRoundResult(tool_calls=(), assistant_text=next(it), finish_reason="stop")
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


_STATE = {"job_id": "j", "session_id": "s", "user_id": "u",
          "messages": [{"role": "user", "content": "external aero on a wing, snappy"}]}


# the rule, stated to the model

def test_the_plain_text_block_is_registered_and_composed():
    names = [b[0] for b in intake.INTAKE_PROMPT_BLOCKS]
    assert names[-1] == "plain_text"
    assert intake._block_plain_text() in intake.compose_intake_system()


def test_the_block_forbids_math_markup_and_shows_the_unicode_to_use():
    block = intake._block_plain_text()
    assert "never write it" in block and "no math renderer" in block
    for markup in ("\\( \\)", "\\[ \\]", "$ $", "^{...}", "_{...}", "\\text{...}", "backslash command"):
        assert markup in block, markup
    for unicode in ("y⁺ = 30–300", "y⁺ ≈ 1", "k-ω SST", "10⁵", "× 10⁶", "m²", "s⁻¹", "15 °C", "Δp", "μm"):
        assert unicode in block, unicode
    assert "Backticks are ONLY for patch names and file names" in block


def test_the_prompts_own_examples_are_written_the_way_the_model_should_write():
    # the model copies its examples: an example in y+ 30-300 teaches y+ 30-300
    system = intake.compose_intake_system()
    for stale in ("y+ 30", "30-300", "k-omega", "at 15 C "):
        assert stale not in system, stale


# the safety net, where the model's words enter the turn

def test_latex_the_model_writes_anyway_is_delivered_and_stored_plain():
    out = _run(dict(_STATE), [_LATEX_REPLY])
    assert out["messages"][-1] == {"role": "assistant", "content": _PLAIN_REPLY}


def test_a_reply_without_markup_is_delivered_as_written():
    reply = "Which engine do you work in - snappyHexMesh, cfMesh or not sure?"
    out = _run(dict(_STATE), [reply])
    assert out["messages"][-1]["content"] == reply


def test_a_question_asked_in_latex_is_the_same_question_in_unicode():
    # a session stored before replies were made plain still carries the LaTeX spelling
    asked = "Wall treatment? I would use wall functions at \\(y^+ \\approx 30\\text{–}300\\) - ok?"
    again = "Wall treatment? I would use wall functions at y⁺ ≈ 30–300 - ok?"
    assert repeats_a_question(again, (asked,))
