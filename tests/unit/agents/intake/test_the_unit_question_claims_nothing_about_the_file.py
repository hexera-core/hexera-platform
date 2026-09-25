# Responsibility: Verify the unit question states no claim about what the uploaded file declares.
# Boundaries: the sentence and which files reach it; the unit vocabulary is contracts/geometry_units'.
from __future__ import annotations

import ast
import pathlib

import pytest
from tests.cad_fixtures import write_step

from meshpipeline.agents.intake import unit_clarification as uc
from meshpipeline.cad.unit_evidence import read_declared_unit
from meshpipeline.contracts.geometry_units import LengthUnit

SOURCE = pathlib.Path(uc.__file__).read_text(encoding="utf-8")


class _Session:
    """A session with an uploaded geometry and no confirmed interpretation - upload.py's own
    outcome for every file whose declared unit did not resolve."""

    geometry_source_id = "11111111-1111-4111-8111-111111111111"
    geometry_interpretation_id = None


def test_a_declared_si_metre_is_refused_by_construction_so_the_file_always_reaches_the_question():
    """THE INPUT THAT EXPOSED IT, at the line that decides it. A STEP declaring SI metre can never
    resolve: `unit_evidence` refuses a declared metre outright because OCC's FileUnits answers
    "metre" for a MALFORMED unit entity too and the two are indistinguishable, and believing the
    wrong one transfers the shape a thousand times too large. No interpretation is recorded, so the
    question is put - to a customer whose file states its unit in its own text."""
    from meshpipeline.cad import unit_evidence as ue

    assert ue._step_unit_entity_is_well_formed(None, LengthUnit.metre) is False
    # not a general distrust of declarations: inch is conversion-based and IS believed
    assert ue._step_unit_entity_is_well_formed(None, LengthUnit.inch) is True
    # ... and with nothing recorded, this session is the one that gets the question
    assert uc.needs_confirmation(_Session()) is True


def test_the_declared_metre_step_really_does_arrive_here(tmp_path):
    """The same claim end to end on a file OCC wrote, because the unit test above asserts a branch
    and this asserts the outcome. Skipped only where the STEP reader cannot be imported at all -
    the OCP build on a dev box is not the one in the image."""
    try:
        from OCP.TColStd import TColStd_SequenceOfAsciiString  # noqa: F401
    except ImportError as exc:                 # pragma: no cover - environment, not behaviour
        pytest.skip(f"this OCP build cannot read STEP units: {exc}")
    step = write_step(tmp_path / "box_m.step", "M")
    text = step.read_text(errors="replace").upper()
    assert "SI_UNIT" in text and "METRE" in text, "the fixture wrote no unit declaration"

    evidence = read_declared_unit(step)
    assert not evidence.resolved and evidence.unit is None
    assert uc.needs_confirmation(_Session()) is True


def test_the_question_makes_no_claim_about_what_the_file_states():
    """It said "because this file does not state it". Asserting only the absence of that one
    spelling would be satisfied by "the file states no unit", which is the same lie reworded, so
    every phrasing of the claim is named - in both directions, because asserting the file DOES
    declare a unit would be equally false for the STL and VTP that carry none."""
    q = uc.QUESTION.lower()
    for claim in ("does not state", "does not record", "does not declare", "states no unit",
                  "records no unit at all", "carries no unit", "has no unit",
                  "this file states", "this file declares", "the file declares a unit"):
        assert claim not in q, f"the question claims {claim!r} about a file it has not read: {q!r}"


def test_the_question_still_asks_the_thing_it_exists_to_ask():
    """The fix is not to stop asking. Nothing has confirmed the scale, and a meshing job that
    guesses it produces a result that looks plausible and is wrong."""
    q = uc.QUESTION.lower()
    assert "nothing has confirmed the scale" in q
    # the COORDINATES are the question, not the declaration - that distinction is the whole point
    assert "coordinates" in q
    for unit in ("millimetres", "centimetres", "metres", "inches"):
        assert unit in q
    # and every offered answer is one the parser accepts, so the question cannot ask for a word
    # that would come back through REFUSAL
    from meshpipeline.contracts.geometry_units import parse_unit
    assert {parse_unit(u) for u in ("millimetres", "centimetres", "metres", "inches")} == set(
        LengthUnit)


@pytest.mark.parametrize("unitless", ["shape.stl", "shape.vtp"])
def test_the_formats_that_really_state_nothing_get_the_same_sentence(tmp_path, unitless):
    """One sentence covers both halves of the disjunction, which is why it asserts neither: an STL
    records no unit, and the metre STEP above records one that could not be verified."""
    p = tmp_path / unitless
    p.write_text("not really geometry")
    assert not read_declared_unit(p).resolved
    assert uc.needs_confirmation(_Session()) is True


def test_no_string_in_this_module_claims_the_file_is_silent():
    """READS THE PARSED CONSTANTS, NOT THE SOURCE TEXT. The claim was spelled across two source
    lines - `"...because this file does "` `"not state it."` - so a search of the file for "does not
    state it" found nothing while the sentence the customer read contained it. Python joins adjacent
    literals into one ast.Constant; the comment above, which quotes the old claim deliberately, is
    not a Constant and is correctly ignored."""
    offenders = []
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        low = node.value.lower()
        for claim in ("does not state it", "does not state a unit", "file does not record",
                      "states no unit"):
            if claim in low:
                offenders.append((node.lineno, node.value))
    assert offenders == [], offenders
