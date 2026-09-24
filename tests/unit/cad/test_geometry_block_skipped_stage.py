# Responsibility: Verify that a stage of the measurement the clock took reaches the builder as a refusal, and that a stage which ran and found nothing does not.
# Boundaries: the block composition over two REAL stored measurements of one real part; the database and the planner's own prompt are other tests.
#
# THE PART IS `tests/fixtures/geometry/cht_enclosing_2region.step`, measured twice by the real package:
# once with the full 120-second budget and once with a 1-second budget, so the clock actually bites. The two
# documents are checked in beside the other stored measurements because a measurement is a tessellation and
# a unit test may not run one.
#
# WHAT THE TWO DOCUMENTS DIFFER BY, and it is the whole of this file. The complete one carries
# `passage_ends: []` - the stage RAN and this part has no passage that stops at a wall with no mouth. The
# short one carries no `passage_ends` key at all and six warning lines, one of them "time budget of 1s
# exhausted after 1s: passage_ends skipped". Before this, both composed a block with 18 or 19 keys,
# `status: ok`, `places_refused` null and the same empty closed-end list: the builder was handed a part
# whose closed ends were never measured as though it were a part that has none, which is a fact that lies,
# and not one of the six lines saying so reached it.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.contract.deliver", needs="the builder's block the refusal has to reach")

from meshpipeline.application import geometry_survey as gs  # noqa: E402
from meshpipeline.cad import regions  # noqa: E402
from meshpipeline.contracts.geometry_source import GeometrySourceRef  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE = "cht_enclosing_2region"
#: The stage whose absence reads as a measured zero, and the kind of place it costs.
STAGE, KIND = "passage_ends", "closed_end"


def _doc(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _brief() -> str:
    return (FIXTURES / f"{CASE}.brief.txt").read_text(encoding="utf-8")


def _block(document: dict) -> dict:
    """The block this platform hands the builder for this document, with the survey on it."""
    state = gs.carry_answers(None, gs.compose(document, purpose="internal_cfd", brief=_brief()))
    block = gs.builder_block(state)
    assert block is not None, "the fixture composes no block, so this file proves nothing"
    return regions.with_the_stages_that_did_not_run(block, document)


def _refusals(block: dict, kind: str) -> list[dict]:
    return [r for r in (block.get("places_refused") or []) if r.get("kind") == kind]


# WHAT THE TWO DOCUMENTS ACTUALLY SAY, pinned before anything is concluded from them


def test_the_two_stored_measurements_differ_by_the_stage_the_clock_took():
    """The premise of every test below, read off the fixtures rather than assumed. `[]` and an absent key
    are the measurement's own two different answers (`hexera.passage_ends_block`)."""
    complete, short = _doc(CASE), _doc(f"{CASE}_over_budget")
    assert complete[STAGE] == [], "the complete measurement ran the stage and found no closed end"
    assert short.get(STAGE) is None, "the short measurement did not run the stage at all"
    assert (complete.get("warnings") or []) == []
    said = [w for w in short["warnings"] if STAGE in w]
    assert len(said) == 2, said
    assert any("skipped" in w and "time budget" in w for w in said)
    assert any("the record is missing" in w for w in said)


# THE REFUSAL REACHES THE BLOCK


def test_a_stage_the_clock_took_becomes_a_refusal_the_builder_reads():
    block = _block(_doc(f"{CASE}_over_budget"))
    refused = _refusals(block, KIND)
    assert len(refused) == 1, block.get("places_refused")
    why = refused[0]["why"]
    assert "did not run on this part" in why
    assert "not a measurement that there are none" in why
    # THE MEASUREMENT'S OWN WORDS, verbatim, so the reason travels rather than being paraphrased here
    assert "time budget of 1s exhausted after 1s: passage_ends skipped" in why
    assert "the record is missing" in why


def test_a_stage_that_ran_and_found_nothing_is_never_refused():
    """A DEFAULT IS NOT A CONFIRMATION, and its opposite matters just as much: a measured zero is a
    measurement, and refusing it would tell the builder to treat a settled part as unknown."""
    block = _block(_doc(CASE))
    assert _refusals(block, KIND) == [], block.get("places_refused")


def test_the_two_blocks_were_indistinguishable_here_before_the_refusal_existed():
    """The defect in one assertion. Strip the refusal from the over-budget block and the two agree about
    the closed ends, which is what the builder was reading."""
    complete, short = _doc(CASE), _doc(f"{CASE}_over_budget")
    for document in (complete, short):
        state = gs.carry_answers(None, gs.compose(document, purpose="internal_cfd", brief=_brief()))
        block = gs.builder_block(state)
        assert [p for p in (block.get("places") or []) if p.get("kind") == KIND] == []
        assert _refusals(block, KIND) == [], "this is the block before the refusal is added"
        assert str(block.get("status")) == "ok", "and nothing about its status said the record was short"


def test_the_measurements_own_words_are_matched_on_the_stage_name_and_nothing_else():
    """No sentence is parsed for meaning: `facts.measure` writes the stage's own identifier into every line
    it writes about that stage, so the join is on a name both distributions own."""
    short = _doc(f"{CASE}_over_budget")
    said = regions.what_the_measurement_said_about(short, STAGE)
    assert said == [w for w in short["warnings"] if STAGE in w]
    assert regions.what_the_measurement_said_about(short, "a_stage_that_does_not_exist") == []
    assert regions.what_the_measurement_said_about(_doc(CASE), STAGE) == []
    assert regions.what_the_measurement_said_about(None, STAGE) == []


def test_a_document_that_says_nothing_still_gets_the_refusal_and_claims_no_reason():
    """The key is also absent on a row measured before the stage existed, which is what every survey fixture
    in this repository is. Which of the two it was is not on the record, so neither is claimed."""
    short = {**_doc(f"{CASE}_over_budget"), "warnings": []}
    why = _refusals(_block(short), KIND)[0]["why"]
    assert "did not run on this part" in why
    assert "whether the clock took it or the row predates the stage is not on the record" in why
    assert "time budget" not in why


# THE SURVEYOR'S OWN RULES, ON THE PLATFORM'S OWN SENTENCE


def test_the_refusal_names_a_place_and_never_a_builder_setting_or_an_outcome():
    """The two rules `check_the_survey_block` runs, over the words this platform wrote itself. The Surveyor
    names places and measurements, never a builder setting, and never predicts how the mesh turns out."""
    sentence = regions.STAGE_DID_NOT_RUN.format(stage="passage ends", kind="closed end")
    assert gs.what_the_surveyor_may_not_say([sentence, regions.STAGE_SAID_NOTHING]) == ""
    block = _block(_doc(f"{CASE}_over_budget"))
    assert gs.what_the_surveyor_may_not_say(block["places_refused"]) == ""


def test_a_quote_that_would_break_those_rules_is_dropped_and_the_refusal_is_not():
    """The quote is another distribution's prose. If it ever names a builder control or predicts a mesh, the
    platform keeps its own sentence rather than losing the refusal, and says so in the log."""
    liar = {**_doc(f"{CASE}_over_budget"),
            "warnings": ["passage_ends skipped, so the mesh will collapse at n_layers"]}
    why = _refusals(_block(liar), KIND)[0]["why"]
    assert "did not run on this part" in why
    assert "the mesh will" not in why and "n_layers" not in why


# FOLLOW THE CALL: A KEY THAT IS WRITTEN IS NOT A KEY THAT IS READ


def test_the_channel_the_refusal_uses_is_one_the_planner_actually_shows_a_model():
    """THE CHECK THIS PROJECT KEEPS NEEDING. `engines/snappy/planner.py` holds an allowlist of the block keys
    it will put in front of a model and drops the rest with a log line, so a key invented in `cad/regions.py`
    would be the same silent loss one boundary further on. `places_refused` is on that list and the planner's
    own note already tells a model how to read it."""
    from meshpipeline.engines.snappy.planner import (
        _AGENT_BLOCK_NOTE,
        GEOMETRY_AGENT_BLOCK_KEYS,
        _validated_agent_block,
    )

    assert "places_refused" in GEOMETRY_AGENT_BLOCK_KEYS
    assert "places_refused" in _AGENT_BLOCK_NOTE
    block = _block(_doc(f"{CASE}_over_budget"))
    shown = _validated_agent_block(block, "job-test")
    assert shown is not None, "the planner refused the whole block"
    assert _refusals(shown, KIND), "the refusal did not survive the planner's allowlist"
    assert STAGE in _refusals(shown, KIND)[0]["why"]


def test_the_seam_that_composes_the_block_for_a_run_is_the_one_that_was_changed(monkeypatch):
    """`agent_block_for_state` is what the planner is handed whenever the geometry step did not run, and the
    refusal is worth nothing unless it is applied there. The row read is stood in; the composition is real."""
    short = _doc(f"{CASE}_over_budget")
    sha = str(short["source"]["sha256"])
    ref = GeometrySourceRef(source_id="11111111-1111-4111-8111-111111111111", owner_id="owner-7f3a",
                            object_key="sources/11111111-1111-4111-8111-111111111111", sha256=sha,
                            size_bytes=int(short["source"]["size_bytes"]),
                            original_filename="cht_enclosing_2region.step", suffix_hint=".step")
    monkeypatch.setattr(regions, "_stored_document",
                        _async(lambda _ref, _digest: short))
    # no survey is stored for these bytes, so the platform composes the measurement's own block, which is
    # the path every job takes before intake has surveyed
    monkeypatch.setattr(gs, "load", _async(lambda *_a, **_k: None))
    state = {"geometry": {"ref": {"source_id": ref.source_id, "owner_id": ref.owner_id,
                                 "object_key": ref.object_key, "sha256": ref.sha256,
                                 "size_bytes": ref.size_bytes,
                                 "original_filename": ref.original_filename,
                                 "suffix_hint": ref.suffix_hint}}}
    block = asyncio.run(regions.agent_block_for_state(state))
    assert block is not None
    assert _refusals(block, KIND), "the run's own block carries no refusal for the stage that did not run"


def _async(fn):
    async def _call(*a, **k):
        return fn(*a, **k)
    return _call


@pytest.mark.parametrize("bad", [None, {}, [], "block", 7])
def test_a_block_that_is_not_a_block_comes_back_exactly_as_it_went_in(bad):
    """Every path through the geometry reading fails open. A refusal is never worth a plan."""
    assert regions.with_the_stages_that_did_not_run(bad, _doc(f"{CASE}_over_budget")) is bad
