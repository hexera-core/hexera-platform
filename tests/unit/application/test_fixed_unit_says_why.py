# Responsibility: Pin that a unit fixed by the upload's conversion is stated WITH its reason, so the source's own unit reads as consistent rather than wrong.
# Boundaries: the confirmation sentence alone; no request, no database.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.application.geometry_confirmation import unit_sentence
from meshpipeline.contracts.intake_formats import format_for_key

# THE TAKE (dev-areen set_top_box, 2026-10-11): the chat said "The file is in millimetres", and a
# user who knows an ECXML model is in metres corrected it at the approval. Both are true.


def test_a_fixed_unit_says_why_in_the_formats_own_words():
    reason = format_for_key("ecxml").fixed_unit_reason
    body = SimpleNamespace(unit="mm", size_mm=[120.0, 90.0, 35.0], unit_fixed=True,
                           unit_fixed_reason=reason)
    said = unit_sentence(body)
    assert "The file is in millimetres" in said
    assert "fixed by the conversion of the uploaded file" in said
    assert "metres" in said.split("millimetres", 1)[1], said
    assert reason.rstrip(".") in said


def test_without_a_reason_the_sentence_is_as_before():
    body = SimpleNamespace(unit="mm", size_mm=[120.0, 90.0, 35.0], unit_fixed=True)
    said = unit_sentence(body)
    assert "fixed by the conversion of the uploaded file, so it is settled." in said
