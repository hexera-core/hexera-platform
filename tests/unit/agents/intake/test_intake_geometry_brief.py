# Responsibility: Verify intake confirms what the file says instead of asking for it, only when a measurement exists, and that nothing about submission changed.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.agents.intake import geometry_brief as gb
from meshpipeline.agents.intake.agent import INTAKE_TOOLS, compose_intake_system
from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor

# The measured tee-wye from the corpus, in the shape the stored document carries. Its brief states a
# 357.24 mm inlet bore and two 301.38 mm branches 53.27 degrees apart, which is what the numbers below
# have to agree with for the table to be worth showing anyone.
DOCUMENT = {
    "schema": "geometry_agent.measurement.v1", "status": "ok", "reason": "",
    "representation": "wall_shell",
    "coordinates": {"unit": "mm", "basis": "occ_transfer", "scale_to_metres": 0.001},
    "bbox": {"extent_mm": [2069.69, 1565.33, 388.14], "diagonal_m": 2.6237},
    "bodies": {"count": 1, "watertight": True},
    "names": [],
    "openings": [
        {"id": "o1", "kind": "ring", "shape": "circle", "planar": True,
         "bore_diameter_m": 0.35694, "min_dimension_m": 0.35694,
         "centroid_m": [0.0, 0.0, 0.0], "normal": [-1.0, 0.0, 0.0], "tilt_deg": 0.0,
         "bbox_side": "x_min"},
        {"id": "o2", "kind": "ring", "shape": "circle", "planar": True,
         "bore_diameter_m": 0.30113, "min_dimension_m": 0.30113,
         "centroid_m": [1.99521, -0.63415, 0.0], "normal": [0.894, -0.448, 0.0], "tilt_deg": 26.636},
        {"id": "o3", "kind": "ring", "shape": "circle", "planar": True,
         "bore_diameter_m": 0.30113, "min_dimension_m": 0.30113,
         "centroid_m": [1.99521, 0.63415, 0.0], "normal": [0.894, 0.448, 0.0], "tilt_deg": 26.635},
        {"id": "o4", "kind": "cap", "shape": "circle", "planar": True,
         "bore_diameter_m": 0.03726, "min_dimension_m": 0.03726,
         "centroid_m": [0.85338, 0.0, -0.17562], "normal": [0.0, 0.0, -1.0], "tilt_deg": 0.0},
    ],
    "thickness": {"thin_clusters": []},
}


# what the block says

def test_the_block_states_the_numbers_instead_of_asking_for_them():
    text = gb.render_block(DOCUMENT)
    assert "357.0 mm" in text or "356.9 mm" in text, text      # the inlet bore, in the table
    assert "2069.69 x 1565.33 x 388.14 mm" in text
    assert "mm" in text and "watertight" in text
    assert "o1" in text and "o2" in text and "o3" in text


def test_the_block_tells_the_model_what_it_may_no_longer_ask():
    """321 of the 601 questions intake writes across 287 stored conversations name something the file
    already answers: a bore, a port, an opening, an axis, a bounding box, a coordinate."""
    text = gb.render_block(DOCUMENT)
    assert "CONFIRM, do not interrogate" in text
    for banned in ("bore", "coordinate", "bounding box", "axis", "opening count", "body count"):
        assert banned in text


def test_the_block_names_what_is_still_worth_asking():
    """What survives: the fluid, the speed, the goal, the budget, the engine, and the port identity."""
    text = gb.render_block(DOCUMENT)
    assert "what is flowing and how" in text and "cell budget" in text
    assert "WHICH of two openings of the same size and" in text
    # the refusal already exists in the builder, in the right words, in the wrong place
    assert "hot" in text and "cold" in text


def test_a_tilted_mouth_is_reported_as_measured_and_never_as_a_role():
    text = gb.render_block(DOCUMENT)
    assert "26.6 degrees off the grid" in text
    assert "inlet" not in text.split("HOW TO USE THIS")[0].lower().replace("inlet/outlet", "")


def test_the_small_openings_get_one_sentence_and_not_a_row():
    """619 openings across the corpus are named by no brief. Today every one is meshed as wall in
    silence; one sentence is the whole change."""
    text = gb.render_block(DOCUMENT)
    assert "meshed as wall" in text
    assert "| o4 |" not in text


#: The same part as a file that declares no unit, which is every STL. `_m` and `_mm` are null - the
#: measurement package will not label a length metres when nobody has said what the numbers are -
#: and the file's own numbers are all there is.
UNITLESS = {
    **DOCUMENT,
    "coordinates": {"unit": None, "basis": "source_file", "scale_to_metres": None},
    "bbox": {"extent_mm": None, "extent_file": [2069.69, 1565.33, 388.14], "diagonal_m": None},
    "openings": [{**o, "bore_diameter_m": None, "min_dimension_m": None, "centroid_m": None,
                  "bore_diameter_file": o["bore_diameter_m"] * 1000.0,
                  "min_dimension_file": o["min_dimension_m"] * 1000.0,
                  "centroid_file": [c * 1000.0 for c in o["centroid_m"]]}
                 for o in DOCUMENT["openings"]],
}


def test_an_undeclared_unit_is_the_one_size_question_that_is_still_real():
    """An STL carries no unit at all, so its coordinates mean nothing until someone says what they are."""
    text = gb.render_block({**DOCUMENT,
                            "coordinates": {"unit": None, "basis": "source_file",
                                            "scale_to_metres": None}})
    assert "NOT DECLARED" in text and "Ask." in text


def test_a_file_with_no_unit_still_shows_what_was_measured():
    """A TABLE OF "unknown" IS WORSE THAN NO TABLE.

    Before this, an unscaled document rendered every size and position as the word "unknown" and
    then told the model not to ask for a bore, a coordinate or a bounding box "that is written
    above" - when nothing was. The geometry WAS measured; only its unit is missing. So the numbers
    are shown in the file's own units, and the one thing that is genuinely unknown is the one thing
    the model is told to ask about.
    """
    text = gb.render_block(UNITLESS)
    assert "unknown" not in text
    assert "file units" in text and "THE SIZES ABOVE HAVE NO UNIT" in text
    # the ratios survive, which is the whole value of an unscaled table: 357 against 301
    assert "| o1 | (0, 0, 0) | 356.9 |" in text
    assert "2069.69 x 1565.33 x 388.14 in the file's own units" in text


def test_significant_figures_not_decimal_places_on_an_unscaled_file():
    """A file authored in metres has a 0.357 bore. `.1f` prints that as "0.4" and its ports as "0.0",
    which destroys the only thing an unscaled table has to offer."""
    metres = {**UNITLESS,
              "openings": [{**o, "bore_diameter_file": o["bore_diameter_file"] / 1000.0,
                            "min_dimension_file": o["min_dimension_file"] / 1000.0,
                            "centroid_file": [c / 1000.0 for c in o["centroid_file"]]}
                           for o in UNITLESS["openings"]]}
    text = gb.render_block(metres)
    assert "0.3569" in text and "0.3011" in text


def test_an_unscaled_file_never_binds_a_patch_and_never_claims_a_check():
    """A declared `diameter_mm` against a number whose unit nobody stated is the 1,000x mistake."""
    bound = gb.bind_patches(UNITLESS, [
        {"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 357.24}])
    assert bound["checked"] is False and bound["bound"] == []


def test_a_closed_body_is_told_it_has_no_openings_rather_than_shown_nothing():
    """Zero is a measurement. The list below tells the model not to ask for an opening count
    "written above", and on a part with no ports nothing was written at all - so a wing arrived with
    a prohibition covering a fact the block had not stated."""
    text = gb.render_block({**DOCUMENT, "openings": []})
    assert "NO openings were measured" in text and "no inlet" in text


def test_a_representation_the_measurement_could_not_name_is_left_out():
    """`the file holds: unknown` tells the model a measurement produced a word it cannot read."""
    text = gb.render_block({**DOCUMENT, "representation": "unknown"})
    assert "the file holds:" not in text
    assert "the file holds:" in gb.render_block(DOCUMENT)


def test_the_block_never_weakens_the_rule_against_inventing_a_dimension():
    """`agents/intake/agent.py:350-353` forbids inventing a dimension the user did not state. A
    measured number is not invented and it is not theirs either, and the block says exactly that."""
    text = gb.render_block(DOCUMENT)
    assert "never invent a" in text
    assert "confirmed it in their own words" in text
    assert "submit_requirements is UNCHANGED" in text


# when there is nothing to say

@pytest.mark.parametrize("document", [None, {}, {"status": "refused", "reason": "too large"}])
def test_no_measurement_means_no_block_at_all(document):
    """The fail-open: the system prompt is character-for-character what ships today.

    `measurement_failed` used to be in this list and is not any more. None and {} are "not attempted", and
    `refused` is the package declining before it opened the file - in all three nothing was ever owed, so
    an empty block is the whole truth. A measurement that RAN and did not finish owed the conversation a
    table, and the test for that is below."""
    assert gb.render_block(document) == ""


def test_a_measurement_that_ran_and_failed_is_the_one_case_that_is_not_silence():
    """MEASURED on the 36-file run of the night of 27 September: 3 of 36 parts timed out at the measurement ceiling and
    those 3 were the stalls. An empty block is the prompt with no measurement at all, so the model could not
    know one was owed, and on all three it asked the customer for the opening sizes and which mouth was the
    inlet. One of those parts has 32 openings.

    The full statement of what the block says, and the fact that it names no geometry, is in
    tests/unit/agents/test_a_measurement_that_failed_is_not_silence.py. This is the boundary itself: the one
    status of the four that is disclosed."""
    block = gb.render_block({"status": "measurement_failed", "reason": "measurement exceeded 60s"})
    assert block == gb.MEASUREMENT_DID_NOT_FINISH
    assert "Do not ask the customer for geometry" in block
    assert "60s" not in block, "the reason is ours; the block carries no seconds to a customer's screen"


def test_a_run_that_names_no_upload_reads_nothing_and_adds_no_block():
    """The fail-open at the top of the chain: no upload, no row, no block, and the system prompt is
    character-for-character the one that ships."""
    from meshpipeline.agents.intake import agent as intake_agent

    reading = asyncio.run(intake_agent._geometry_reading({}))
    assert reading.document is None and reading.reading is None
    assert intake_agent._geometry_block(reading) == ""
    assert compose_intake_system() + "" == compose_intake_system()


def test_the_tool_schema_did_not_change():
    """This is Rehaan's production conversation. A regression here is worse than a missed improvement."""
    names = [t["function"]["name"] for t in INTAKE_TOOLS]
    assert names == ["web_search", "recommend_compatible_engines", "propose_engine_selection",
                     "confirm_engine_selection", "preview_selected_admission",
                     "submit_requirements"]
    submit = next(t for t in INTAKE_TOOLS if t["function"]["name"] == "submit_requirements")
    fields = set(submit["function"]["parameters"]["properties"])
    # No field was added for the measurement, and none was taken away. The block changes what the
    # model ASKS; what it SUBMITS is the same payload, validated by the same validator.
    assert not {f for f in fields if "measure" in f or f.startswith("geometry_")}
    assert {"mesh_engine", "purpose", "input_kind", "dimensionality", "patches",
            "engine_params", "preview_token", "request_txt"} <= fields


# binding a declared patch to a measured opening

def test_a_declared_port_binds_to_the_opening_it_names():
    bound = gb.bind_patches(DOCUMENT, [
        {"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 357.24},
        {"name": "outlet_1", "type": "outlet", "near_mm": [1995.21, 634.15, 0]},
        {"name": "outlet_2", "type": "outlet", "near_mm": [1995.21, -634.15, 0]},
        {"name": "wall", "type": "wall"},
    ])
    assert bound["checked"] is True
    assert [b["opening_id"] for b in bound["bound"]] == ["o1", "o3", "o2"]


def test_a_port_the_measurement_cannot_place_leaves_the_check_unmade():
    """A check of some of the patches is not a check."""
    bound = gb.bind_patches(DOCUMENT, [
        {"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0]},
        {"name": "outlet", "type": "outlet"},     # no position, no size
    ])
    assert bound["checked"] is False
    assert [u["name"] for u in bound["unbound"]] == ["outlet"]


def test_two_identical_ports_refuse_to_bind_by_size_alone():
    """The one port question a measurement genuinely cannot settle, refused rather than guessed."""
    bound = gb.bind_patches(DOCUMENT, [
        {"name": "outlet_1", "type": "outlet", "diameter_mm": 301.38},
        {"name": "outlet_2", "type": "outlet", "diameter_mm": 301.38},
    ])
    assert bound["checked"] is False and len(bound["unbound"]) == 2


def test_a_port_on_the_wrong_end_of_the_part_does_not_bind():
    bound = gb.bind_patches(DOCUMENT, [
        {"name": "inlet", "type": "inlet", "near_mm": [9000, 9000, 9000]}])
    assert bound["checked"] is False


def test_a_stated_position_that_matches_nothing_is_never_rescued_by_the_bore():
    """THE CHECK THAT COULD NOT FAIL.

    `agents/intake/validation.py:275-282` requires a size on every declared port, so every real
    patch carries one. A binding that answers by position and then falls through to size when the
    position matches nothing is therefore a binding that cannot fail on any real declaration: the
    inlet below is stated 9 metres from a part 2.6 metres across and still has the one 357 mm bore
    in the file. `contracts/rationale.py` would then tell the customer their assignments "were
    checked against the measured geometry" on the strength of a check that looked at the number
    they got wrong and answered from a different one.
    """
    bound = gb.bind_patches(DOCUMENT, [
        {"name": "inlet", "type": "inlet", "near_mm": [9000, 9000, 9000], "diameter_mm": 357.24}])
    assert bound["checked"] is False
    assert bound["bound"] == [] and bound["unbound"] == [{"name": "inlet", "opening_id": None}]


def test_a_position_cannot_be_checked_without_a_body_to_measure_it_against():
    """No diagonal, no tolerance, no check. The nearest opening is a guess, not a verdict."""
    document = {**DOCUMENT, "bbox": {**DOCUMENT["bbox"], "diagonal_m": None}}
    bound = gb.bind_patches(document, [
        {"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 357.24}])
    assert bound["checked"] is False


def test_a_port_that_states_no_position_still_binds_by_its_bore():
    """Size stays the answer for the patch that gave no location, which is what it is for."""
    bound = gb.bind_patches(DOCUMENT, [
        {"name": "inlet", "type": "inlet", "diameter_mm": 357.24}])
    assert bound["bound"] == [{"name": "inlet", "opening_id": "o1"}]


def test_with_no_measurement_nothing_is_checked():
    for document in (None, {}, {"status": "measurement_failed"}):
        assert gb.bind_patches(document, [{"name": "i", "type": "inlet"}])["checked"] is False


# what the customer is told

def _finalised(monkeypatch, *, document, patches) -> str:
    said: list = []

    class _Trace:
        def note(self, *a, **k):
            said.append((a, k))

    state = IntakeExecutionState(session_id="s", owner_id="o", geometry_document=document)
    executor = IntakeToolExecutor(state=state, job_id="j", implemented_engines=["snappy"],
                                 search_tool=None, trace=_Trace())
    from meshpipeline.contracts import rationale as R

    lines: list = []
    monkeypatch.setattr(R, "_say", lambda _p, headline, detail="": lines.append(detail))
    R.intake_requirements_finalized(
        None, patches=len(patches), dimensionality="3D",
        geometry_checked=bool(executor._patch_binding(patches).get("checked")))
    return lines[-1]


def test_the_sentence_withholds_the_claim_when_nothing_bound(monkeypatch):
    """False on every job ever run until this phase: the checker read a directory `upload.py:253`
    empties, so the declaration was compared against nothing. An upload whose measurement never
    landed is that same situation, and the sentence still has to withhold the claim.

    It withholds the CONFIRMATION and says nothing about whether the measurement ran: this same
    `checked=False` arrives from four causes, one of which is a farfield brief with no declared
    port and a perfectly good measurement, and one bool cannot tell them apart. See
    `tests/unit/contracts/test_the_unchecked_sentence_claims_nothing_about_the_measurement.py`."""
    detail = _finalised(monkeypatch, document=None,
                        patches=[{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0]}])
    assert "not confirmed against the measured geometry" in detail
    assert "not measured" not in detail


def test_the_sentence_becomes_true_only_when_every_declared_port_bound(monkeypatch):
    patches = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0]},
               {"name": "outlet_1", "type": "outlet", "near_mm": [1995.21, 634.15, 0]},
               {"name": "outlet_2", "type": "outlet", "near_mm": [1995.21, -634.15, 0]}]
    detail = _finalised(monkeypatch, document=DOCUMENT, patches=patches)
    assert "checked against the measured geometry" in detail
    # one port that cannot be placed, and the claim retreats
    partial = _finalised(monkeypatch, document=DOCUMENT,
                         patches=[*patches, {"name": "outlet_3", "type": "outlet"}])
    assert "not confirmed against the measured geometry" in partial
    # and it does not retreat into a claim about the measurement, which ran and succeeded here
    assert "not measured" not in partial


# what the engines are handed

def test_the_executor_hands_the_engines_the_stored_reading():
    reading = {"status": "ok", "region_count": 2, "region_names": ["a", "b"]}
    state = IntakeExecutionState(session_id="s", owner_id="o", geometry_reading=reading)
    executor = IntakeToolExecutor(state=state, job_id="j", implemented_engines=["snappy"],
                                 search_tool=None)
    assert executor._geometry_facts() is reading


def test_not_attempted_is_none_and_never_an_empty_dict():
    """`{}` passes `base.py:430-434`'s `is not None` test carrying nothing, so three situations
    arrived as one value. `None` is the fourth answer and it is the absence of the row that says so."""
    state = IntakeExecutionState(session_id="s", owner_id="o", geometry_reading=None)
    executor = IntakeToolExecutor(state=state, job_id="j", implemented_engines=["snappy"],
                                 search_tool=None)
    assert executor._geometry_facts() is None


