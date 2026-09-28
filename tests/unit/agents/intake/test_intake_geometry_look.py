# Responsibility: Verify what the look may say to intake, and that what it may not say cannot arrive.
# Boundaries: the prompt block only; the tiers themselves are measured and owned by the measurement package.
from __future__ import annotations

from meshpipeline.agents.intake.geometry_brief import look_at_it, look_lines, render_block

#: The shape the measurement package composes for a model: the relied-on fields, the candidates marked
#: as candidates, and not one recorded field. `defects` is absent because the package does not put it in.
FOR_MODEL = {
    "is_measurement": False,
    "read_as": "words from rendered views, not a measurement",
    "relied_on": {"attachments": ["a bolt flange at each end"], "confidence": "high",
                  "openings_seen": ["o1", "o2", "o3"], "inside_is_plain": True},
    "candidates": {"looks_like": "a branched distribution manifold",
                   "sharp_edges": ["a sharp mouth rim at o2"],
                   "opening_mouths": {"o1": "protruding"}},
    "withheld": {"defects": "3 to 5 false alarms on clean files"},
    "identity_confidence_max": 0.6,
    "model": "a-vision-model",
}

#: The stored impression still holds everything the model said, because a person reads the row.
IMPRESSION = {"looks_like": "a branched distribution manifold", "confidence": "high",
              "orientation": "the long axis runs along +X", "symmetry": "mirror symmetric",
              "defects": ["SENTINEL-A-STRAY-BODY"], "notes": "SENTINEL-A-DEAD-LEG",
              "openings_seen": [{"id": "o1", "looks_like": "flange face", "mouth": "protruding",
                                 "likely_role": "SENTINEL-INLET", "why": ""}],
              "internal_features": [], "sharp_edges": [], "attachments": ["a bolt flange at each end"],
              "thin_parts": []}

MEASURED = {
    "schema": "geometry_agent.measurement.v1", "status": "ok", "representation": "wall_shell",
    "coordinates": {"unit": "mm", "basis": "occ_transfer", "scale_to_metres": 0.001},
    "bbox": {"extent_mm": [100.0, 50.0, 50.0], "diagonal_m": 0.12},
    "bodies": {"count": 1, "watertight": True},
    "openings": [{"id": "o1", "bore_diameter_m": 0.05, "centroid_m": [0.0, 0.0, 0.0],
                  "normal": [1.0, 0.0, 0.0], "planar": True, "kind": "cap", "tilt_deg": 0.0,
                  "bbox_side": "-x"}],
}


def _with_look(status: str = "ok") -> dict:
    return {**MEASURED,
            "look": {"status": status, "impression": IMPRESSION if status == "ok" else None,
                     "model": "a-vision-model"},
            "planner_block": {"schema": "geometry_agent.planner_block.v1", "status": "ok",
                              "look": FOR_MODEL}}


# NOTHING AT ALL WHEN THERE IS NOTHING TO SAY


def test_no_look_renders_the_prompt_that_ships_today():
    """Character for character. This is the fail-open at the intake boundary."""
    assert look_lines(MEASURED) == []
    assert look_at_it(MEASURED) is None
    assert "WHAT IT LOOKS LIKE" not in render_block(MEASURED)


def test_a_look_that_failed_or_never_ran_renders_nothing():
    for status in ("failed", "not_attempted"):
        document = _with_look(status)
        document["planner_block"].pop("look")
        assert look_lines(document) == [], status
        assert "WHAT IT LOOKS LIKE" not in render_block(document)


def test_a_measurement_that_did_not_succeed_renders_no_table_and_no_look():
    """It used to render nothing at all, and this test was named for that. MEASURED on the 36-file run of
    28 September: three parts timed out at the measurement ceiling, an empty block is the prompt with no
    measurement in the product at all, and on all three the model asked the CUSTOMER for the opening sizes
    and which mouth was the inlet. So a failed measurement is now DISCLOSED.

    What must not change is what the block may contain: there is no measurement, so there is no table, no
    opening ids and not one word of the look. That is what this test is really about, and it is stricter
    than the empty string was, because an empty string cannot leak and a sentence can."""
    document = {**_with_look(), "status": "measurement_failed"}
    block = render_block(document)
    assert block, "silence is what made the model ask the customer to measure their own file"
    assert "did not finish" in block
    assert look_lines(document) == [], "no look content reaches a conversation with no measurement"
    for leaked in ("WHAT IT LOOKS LIKE", "a bolt flange at each end", "o1", "branched distribution"):
        assert leaked not in block, f"{leaked!r} is a measured or seen fact and there are none"


def test_nothing_at_all_is_not_an_exception():
    assert look_lines(None) == [] and look_at_it(None) == look_at_it({}) is None


# WHAT MAY BE SAID, AND WHAT MAY NOT


def test_the_block_is_rendered_in_two_groups_and_says_which_is_which():
    text = "\n".join(look_lines(_with_look()))
    assert "YOU MAY STATE THESE" in text
    assert "ASK, NEVER ASSERT" in text
    assert "a bolt flange at each end" in text
    assert "o1; o2; o3" in text
    assert "plain, nothing across it" in text
    assert "a branched distribution manifold" in text


def test_not_one_recorded_field_can_reach_the_conversation():
    """Three false alarms on clean files is a customer told their file is broken when it is not."""
    text = "\n".join(look_lines(_with_look()))
    assert "SENTINEL-A-STRAY-BODY" not in text
    assert "SENTINEL-A-DEAD-LEG" not in text
    assert "SENTINEL-INLET" not in text
    assert "mirror symmetric" not in text
    assert "+X" not in text


def test_the_stored_row_still_holds_everything_a_person_would_want_to_read():
    document = _with_look()
    assert document["look"]["impression"]["defects"] == ["SENTINEL-A-STRAY-BODY"]


def test_the_block_says_out_loud_what_the_picture_does_not_settle():
    text = "\n".join(look_lines(_with_look()))
    assert "Put nothing from here in a patch." in text
    assert "which opening" in text and "inlet" in text


def test_the_look_is_taken_from_the_block_the_package_composed_when_there_is_one():
    """Composed once, by the code that owns the tiers. Never recomposed on this side."""
    document = _with_look()
    document["planner_block"]["look"] = {**FOR_MODEL, "relied_on": {"attachments": ["SENTINEL-FROM-BLOCK"]}}
    assert "SENTINEL-FROM-BLOCK" in "\n".join(look_lines(document))


def test_the_look_lands_after_the_measured_table_and_not_inside_it():
    text = render_block(_with_look())
    assert text.index("WHAT THE FILE IS") < text.index("WHAT IT LOOKS LIKE")
    measured_half = text[:text.index("WHAT IT LOOKS LIKE")]
    assert "a branched distribution manifold" not in measured_half
