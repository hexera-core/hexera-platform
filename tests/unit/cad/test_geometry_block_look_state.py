# Responsibility: Verify the look's state reaches the builder on the third path too - the one with a stored
#                 measurement and NO survey row, where the planner gets the measurement's own block.
# Boundaries: the block composition over a real stored measurement of a real part, with the row read stood in.
#             No database, no planner prompt, no network.
#
# THE THREE PATHS A BLOCK REACHES THE BUILDER BY, and the look's state now travels all three:
#
#   1. the geometry agent PLANNED the job          geometry_step.builder_handoff        (the normal case)
#   2. no plan, but a survey row exists            geometry_survey.builder_block
#   3. no survey row at all                        hexera.planner_block, composed here
#
# Path 3 is the fail-open `cad/regions.agent_block_for_state` documents: a job whose bytes have a measurement
# but no survey. The block it hands over carries `looked`, a BOOLEAN, so a look that FAILED and a look nobody
# took arrive as the same False beside the same empty `seen`. There is no row here, so `look_queued` does not
# exist and PENDING cannot be told from never - and this path deliberately does not claim to tell them apart.
# What it can tell apart, and now does, is a look that failed from a look that was never taken.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.agent.hexera", needs="the measurement's own block the look's state has to reach")

from meshpipeline.application import geometry_survey as gs  # noqa: E402
from meshpipeline.cad import regions  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE = "cht_enclosing_2region"


def _doc(**extra) -> dict:
    return {**json.loads((FIXTURES / f"{CASE}.json").read_text(encoding="utf-8")), **extra}


def _async(fn):
    async def _call(*a, **k):
        return fn(*a, **k)
    return _call


def _block_for(document: dict, monkeypatch) -> dict | None:
    """What `agent_block_for_state` hands the planner for this document with no survey row stored."""
    monkeypatch.setattr(regions, "_stored_document", _async(lambda _ref, _digest: document))
    monkeypatch.setattr(gs, "load", _async(lambda *_a, **_k: None))
    state = {"geometry": {"ref": {"source_id": "11111111-1111-4111-8111-111111111111",
                                 "owner_id": "owner-7f3a",
                                 "object_key": "sources/11111111-1111-4111-8111-111111111111",
                                 "sha256": str(document["source"]["sha256"]),
                                 "size_bytes": int(document["source"]["size_bytes"]),
                                 "original_filename": "cht_enclosing_2region.step",
                                 "suffix_hint": ".step"}}}
    return asyncio.run(regions.agent_block_for_state(state))


def _look_rows(block: dict | None) -> list[str]:
    survey = (block or {}).get("survey")
    return [r["why"] for r in ((survey or {}).get("unsettled") or []) if r.get("about") == "look"]


def test_the_document_alone_says_ok_failed_or_never_and_never_claims_pending():
    """`look_state_of_document` is the reader for a path with no row. Three answers, not four: the queue's
    answer lives on the survey row, so nothing here can tell a look on its way from one never taken."""
    assert gs.look_state_of_document(_doc()) == gs.LOOK_NONE
    assert gs.look_state_of_document(_doc(look={"status": "failed", "reason": "no answer"})) == gs.LOOK_FAILED
    assert gs.look_state_of_document(_doc(look={"status": "refused"})) == gs.LOOK_FAILED
    assert gs.look_state_of_document(_doc(look={"status": "ok", "seen": {}})) == gs.LOOK_OK
    assert gs.look_state_of_document(None) == gs.LOOK_NONE
    assert gs.look_state_of_document(_doc(look="not a dict")) == gs.LOOK_NONE
    answers = {gs.look_state_of_document(_doc(look={"status": s}))
               for s in ("", "pending", "queued", "ok", "failed", "refused", "error")}
    assert answers == {gs.LOOK_OK, gs.LOOK_FAILED, gs.LOOK_NONE}, (
        f"it answered {sorted(answers)}: a document with no row cannot say PENDING, and every state it does "
        f"say has to be one of LOOK_STATES")
    assert answers < set(gs.LOOK_STATES), "one of these answers is not a look state the product names"


def test_a_block_with_no_survey_row_says_no_look_was_taken(monkeypatch):
    block = _block_for(_doc(), monkeypatch)
    assert block is not None, "the fixture composes no block, so this file proves nothing"
    assert block["survey"]["looked"] is False
    assert _look_rows(block) == [gs.LOOK_BECAUSE[gs.LOOK_NONE]]


def test_a_block_with_no_survey_row_says_the_look_FAILED(monkeypatch):
    """The collapse this closes: before, both this and the block above carried `looked: false` and nothing
    else, so a part whose look had broken and a part nobody had looked at were the same block."""
    block = _block_for(_doc(look={"status": "failed", "reason": "the reader returned nothing"}), monkeypatch)
    assert block is not None
    assert block["survey"]["looked"] is False
    (why,) = _look_rows(block)
    assert why == gs.LOOK_BECAUSE[gs.LOOK_FAILED]
    assert "FAILED" in why and "not a clear passage" in why
    assert why != gs.LOOK_BECAUSE[gs.LOOK_NONE], "a failed look is not an absent one"


def test_the_words_are_the_survey_paths_words(monkeypatch):
    """ONE WORDING for all three paths. `geometry_survey.LOOK_BECAUSE` is read, never restated."""
    for state_name, look in ((gs.LOOK_NONE, None), (gs.LOOK_FAILED, {"status": "failed"})):
        document = _doc() if look is None else _doc(look=look)
        assert _look_rows(_block_for(document, monkeypatch)) == [gs.LOOK_BECAUSE[state_name]]


def test_a_refused_look_row_costs_the_row_and_never_the_block(monkeypatch):
    """This path is the planner's fail-open, so a row the survey contract refuses must cost the ROW. A block
    with no sentence is where the builder already was; no block at all is worse, and a plan is never failed
    for this."""
    monkeypatch.setitem(gs.LOOK_BECAUSE, gs.LOOK_NONE, "the mesh will collapse here")
    block = _block_for(_doc(), monkeypatch)
    assert block is not None, "the refusal took the whole block, which this path may never do"
    assert _look_rows(block) == [], "the row that breaks the contract was handed over anyway"
    assert block.get("bbox") or block.get("representation"), "the block lost the measurement's own content"


@pytest.mark.parametrize("bad", [None, {}, [], "block", 7])
def test_a_block_that_is_not_a_block_comes_back_exactly_as_it_went_in(bad):
    """Every path through the geometry reading fails open, this one included."""
    assert regions._with_the_look_state(bad, _doc()) is bad


def test_a_stored_status_this_side_does_not_know_is_read_as_a_LOOK_THAT_FAILED():
    """THE CAREFUL DIRECTION, in place of branches for two values nothing has ever written.

    Both readers used to name `"refused"` and `"error"` explicitly and read everything else as "nobody
    looked". MEASURED: `hexera.look_block` raises `ContractError` on any value outside `LOOK_STATES` and is the
    package's only composer of a look block; the vendored wheel writes only `ok` and `failed` out of
    `vision/look.py`, and its `refused` occurrences are MEASUREMENT statuses in `facts/`; `git log -S` over the
    whole agent history finds neither word as a look status in any version, and no `LOOK_REFUSED` or
    `LOOK_ERROR` identifier ever existed. So the two branches were dead, and what was live was the fall-through
    - which read a status this side does not recognise as a look nobody took, turning a look into silence.

    ABSENT IS STILL ABSENT. No `look` key and an empty status are the one thing that means nobody looked, and
    the package's own `not_attempted` says so in words. Everything else is a look that was taken.
    """
    assert gs.look_state_of_document(_doc(look={"status": "refused"})) == gs.LOOK_FAILED
    assert gs.look_state_of_document(_doc(look={"status": "error"})) == gs.LOOK_FAILED
    assert gs.look_state_of_document(_doc(look={"status": "a word from a later version"})) == gs.LOOK_FAILED
    assert gs.look_state_of_document(_doc(look={"status": ""})) == gs.LOOK_NONE
    assert gs.look_state_of_document(_doc(look={"status": gs.LOOK_NONE})) == gs.LOOK_NONE
    assert gs.look_state_of_document(_doc()) == gs.LOOK_NONE


def test_the_row_reader_reads_an_unknown_status_the_same_careful_way():
    """`look_state` reads the ROW and has one more answer than the document reader, so the order matters: the
    queue's own answer is more informative than a status nobody recognises and still has to win."""
    def _row(status, queued=""):
        return {"composed_for": {"look_status": status}, "look_queued": queued}
    assert gs.look_state(_row("refused")) == gs.LOOK_FAILED
    assert gs.look_state(_row("a word from a later version")) == gs.LOOK_FAILED
    assert gs.look_state(_row("a word from a later version", gs.LOOK_QUEUED)) == gs.LOOK_PENDING
    assert gs.look_state(_row("")) == gs.LOOK_NONE
    assert gs.look_state(_row(gs.LOOK_NONE)) == gs.LOOK_NONE
    assert gs.look_state(_row(gs.LOOK_OK)) == gs.LOOK_OK
    assert gs.look_state(None) == gs.LOOK_NONE


def test_neither_reader_names_a_status_no_version_has_written():
    """A branch for a specific value nothing writes is a branch nobody can test and everybody has to read."""
    source = (Path(regions.__file__).parents[1] / "application" / "geometry_survey.py").read_text(
        encoding="utf-8")
    body = source[source.index("def look_state(state:"):source.index("def _noted_queue(")]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    for dead in ('"refused"', '"error"', "'refused'", "'error'"):
        assert dead not in code, f"{dead} is still branched on where nothing can write it"
