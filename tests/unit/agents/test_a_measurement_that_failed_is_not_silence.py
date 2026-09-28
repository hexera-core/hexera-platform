# Responsibility: a measurement that was ATTEMPTED and did not finish is disclosed to intake, not rendered as
#                 an absence.
# Boundaries: the prompt block only. What the customer reads is the model's own words; the measurement path
#             itself is tests/unit/application/.
"""MEASURED, on the 36-file run of the night of 2026-09-27. Three parts of 36 hit the 60-second measurement ceiling -
blade_row_rotor_001, shell_and_tube_7, shell_and_tube_7_unshared - and those three were the stalls. All 33
parts that measured either submitted or were correctly refused.

With the block empty the system prompt is character for character the prompt with no measurement at all, so
the model cannot know one was owed. On all three it went looking for the missing facts in the only other
place there is, and asked the CUSTOMER for them:

    I don't actually have measurements of the openings yet [...] Which opening is the inlet, and which is
    the outlet? [...] The inlet opening diameter (or width x height), even roughly.

    I can't act on that [...] I won't guess your openings for you.

The shell and tube has 32 openings. Nobody reads a bore off a STEP file by hand, so our timeout became the
customer's homework and none of the three submitted.

`shell_and_tube_7` measured successfully at 15:57 and failed at 00:37 the next morning under the load of the
batch, which is what makes the empty block expensive rather than merely wrong: the same part gives two
different customers two different products.
"""
from __future__ import annotations

from meshpipeline.agents.intake import geometry_brief as gb
from meshpipeline.contracts.geometry_measurement import (
    MEASUREMENT_SCHEMA,
    STATUS_MEASUREMENT_FAILED,
    STATUS_REFUSED,
    STATUS_UNSUPPORTED_FORMAT,
)


def _failed(reason: str = "measurement exceeded 60s and was killed") -> dict:
    """The document the failure path actually stores: schema, status, reason, no plan."""
    return {"schema": MEASUREMENT_SCHEMA, "status": STATUS_MEASUREMENT_FAILED, "reason": reason,
            "plan": None, "seconds": 60.36}


def test_a_measurement_that_did_not_finish_is_said_rather_than_left_empty():
    block = gb.render_block(_failed())
    assert block, "an empty block is the prompt with no measurement at all, which is the defect"
    assert block == gb.MEASUREMENT_DID_NOT_FINISH
    assert "did not finish" in block
    assert "not a property of" in block, "it has to say the limit is ours, or a retry reads as pointless"


def test_it_shuts_the_door_the_transcripts_show_the_model_walking_through():
    """The specific asks the three stalls made, each named so the block cannot be softened into advice."""
    block = gb.render_block(_failed())
    for forbidden in ("opening sizes", "bores or diameters", "which mouth is the inlet",
                      "how many openings", "coordinates"):
        assert forbidden in block, f"the block has to name {forbidden!r} as a thing not to ask for"
    assert "Do not ask the customer for geometry" in block
    assert "never promise a table you cannot produce" in block, (
        "one stall promised 'about half a minute to read the shape' and then reported it had not read it")


def test_it_still_names_what_a_person_actually_holds():
    """A block that only forbids leaves the turn with nothing to do, and the conversation still needs the
    fluid and the purpose - which are the things a person does hold."""
    block = gb.render_block(_failed())
    for allowed in ("the purpose", "the fluid", "the flow conditions", "the cell "):
        assert allowed in block


def test_nothing_measured_at_all_is_still_the_empty_block():
    """None is "not attempted", and there the empty block is the whole truth: nothing was owed."""
    assert gb.render_block(None) == ""
    assert gb.render_block({}) == ""
    assert gb.render_block("not a document") == ""  # type: ignore[arg-type]


def test_the_other_two_failures_keep_todays_behaviour():
    """`refused` is the package declining before it opened the file; `unsupported_format` is the customer's
    to act on and `pipeline/geometry_admission` already says so at submission. Both are separate decisions
    from this one, and a test that let them drift would make this change bigger than it is."""
    for status in (STATUS_REFUSED, STATUS_UNSUPPORTED_FORMAT):
        doc = {**_failed(), "status": status}
        assert gb.render_block(doc) == "", status


def test_the_block_leaks_nothing_of_ours_to_the_screen_it_ends_up_on():
    """It is a prompt block, but whatever is in it can be paraphrased to a customer, so it carries no path,
    no module name, no seconds and no internal identifier."""
    block = gb.render_block(_failed())
    for leak in (".py", "meshpipeline", "60s", "timeout", "source_id", "sha256", "STATUS_"):
        assert leak not in block, f"{leak!r} has no business on a customer's screen"
