# Responsibility: Verify verdict normalisation and quality classification across their full truth table.
from __future__ import annotations

import pytest

from meshpipeline.pipeline.outcome import classify_quality, normalize_verdict


@pytest.mark.parametrize(
    "raw, expected",
    [
        # the EXACT wire forms the current Reviewer writes, plus surrounding whitespace
        ("PASS", "PASS"),
        ("FAIL", "FAIL"),
        ("  PASS  ", "PASS"),
        ("  FAIL  ", "FAIL"),
        # everything else is "no verdict" - the boundary parser is deliberately not permissive
        ("pass", ""),
        ("fail", ""),
        ("passed", ""),
        ("<<PASS>>", ""),          # an api_failure MARKER form, never a verdict
        ("<<FAIL>>", ""),
        ("", ""),
        ("nonsense", ""),
        (None, ""),
    ],
)
def test_normalize_verdict(raw, expected):
    assert normalize_verdict(raw) == expected



_TRUTH_TABLE = [
    ("PASS",  "",        False, True,  "succeeded"),
    ("PASS",  "x",       False, True,  "api_failure"),
    ("PASS",  "x",       True,  True,  "api_failure"),
    ("PASS",  "",        True,  True,  "unsolvable"),
    ("FAIL",  "x",       False, True,  "api_failure"),
    ("FAIL",  "x",       True,  True,  "api_failure"),
    ("",      "x",       True,  False, "api_failure"),
    ("FAIL",  "",        True,  False, "unsolvable"),
    ("FAIL",  "",        True,  True,  "unsolvable"),
    ("FAIL",  "",        False, False, "pipeline_failure"),
    ("",      "",        False, False, "pipeline_failure"),
    ("PASS",  "",        False, False, "pipeline_failure"),
    ("FAIL",  "",        False, True,  "reviewer_rejection"),
    # A marker-wrapped string is NOT a verdict. It never reaches SUCCEEDED - a failure marker
    # must never be readable as a PASS.
    ("<<PASS>>", "",     False, True,  "reviewer_rejection"),
    ("<<FAIL>>", "",     False, True,  "reviewer_rejection"),
    ("<<PASS>>", "",     True,  True,  "unsolvable"),
]


@pytest.mark.parametrize(
    "verdict, api_failure, solvability_failed, executor_ever_succeeded, expected",
    _TRUTH_TABLE,
)
def test_classify_quality_truth_table(
    verdict, api_failure, solvability_failed, executor_ever_succeeded, expected
):
    actual = classify_quality(
        reviewer_verdict=verdict,
        api_failure=api_failure,
        solvability_failed=solvability_failed,
        executor_ever_succeeded=executor_ever_succeeded,
    )
    assert actual == expected, (
        f"verdict={verdict!r} api_failure={api_failure!r} "
        f"solvability_failed={solvability_failed} "
        f"executor_ever_succeeded={executor_ever_succeeded}: "
        f"got {actual!r}, expected {expected!r}"
    )
