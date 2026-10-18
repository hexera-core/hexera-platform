# Responsibility: Verify model math markup reaches a person as plain engineering text, and nothing else changes.
# Boundaries: the normaliser alone, on the shared cases the console is held to as well; where it is applied is tested beside those callers.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.contracts.engineering_text import plain

CASES_FILE = Path(__file__).parents[2] / "fixtures" / "engineering_text_cases.json"
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def _id(case: dict) -> str:
    return f"{case['group']}: {case['why']}"


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_each_case_reads_as_a_person_writes_it(case):
    assert plain(case["in"]) == case["out"]


@pytest.mark.parametrize("case", CASES, ids=_id)
def test_plain_text_is_left_alone(case):
    # Idempotent: a stored reply read back, or a reply the console renders again, never changes
    # a second time - so `10^0.8` kept as `x^0.8` does not become x⁰.8 on the next pass.
    assert plain(case["out"]) == case["out"]


def test_the_cases_cover_what_the_model_actually_wrote():
    # every distinct LaTeX span found in the harness transcripts (2026-09 sweep and rehearsal)
    ins = "\n".join(c["in"] for c in CASES)
    for seen in (r"\(y^+=30\text{-}300\)", r"\(y^+ \approx 30\text{–}300\)", r"\(y^+ \approx 1\)",
                 r"\(y^+\approx30\text{–}300\)", r"\(y^+ \approx 30\!-\!300\)", r"\(k\text{-}\omega\)",
                 r"\(y^+ = 30\text{–}300\)", r"\(k\)", r"\(\omega\)", r"\(y^+=30\)", r"\(k\text{–}\omega\)",
                 r"\((-38, 0, 0)\)", r"\((0, 0, 48)\)", r"\((44, 0, 0)\)", r"\(+x\)", r"\(2\times10^6\)",
                 r"\(y^+\)"):
        assert seen in ins, seen


def test_no_case_the_harness_saw_leaves_markup_behind():
    for case in CASES:
        if case["group"] != "real":
            continue
        out = case["out"]
        for marker in ("\\(", "\\)", "\\text", "\\approx", "\\omega", "\\times", "^+", "y+"):
            assert marker not in out, (marker, out)


@pytest.mark.parametrize("value", [None, "", 0, "no markup here at all."])
def test_nothing_to_do_returns_the_value_itself(value):
    assert plain(value) is value


def test_code_is_byte_for_byte_whatever_surrounds_it():
    code = "`inlet_1`, `wing.step` and `y^+ 30-300 \\(x\\)`"
    text = f"\\(y^+ \\approx 1\\) on {code}, 30-300"
    assert plain(text) == f"y⁺ ≈ 1 on {code}, 30–300"


def test_the_demo_line_reads_as_areen_asked():
    # "y+ = 30-300 must read as y⁺ = 30–300"
    assert plain(r"wall functions at \(y^+=30\text{-}300\)") == "wall functions at y⁺ = 30–300"
