# Responsibility: Verify that when the conjugate engine declines a part on size, the words it
# wrote reach the user's own message - not just the marker.
# Boundaries: no mesher, no workspace of a real run. It writes the refusal record the driver
# writes and reads the sentence `failure_cause.describe` builds from it.
"""THE ENGINE'S BEST WORK WAS GOING ONLY TO A LOG FILE.

Measured end to end on the first customer part, 2026-11-12, job 0904c8bc. The engine declined and
wrote, precisely:

    this part has two scales of passage - about 2.11 mm and 0.0982 mm - and no pair of cell sizes
    resolves the thin ones at 4 cells across inside a limit of 400,000,000 cells and this mesher's
    own ceiling of 178,956,970; a configuration that fits the cell limit was refused on the octree
    ceiling alone and would build on 2 ranks

That is a measured, specific, customer-grade account of their own geometry, and it is the whole
value of refusing rather than building something bad. What the customer was shown instead:

    No mesh was built: Conjugate (coolant + metal) stopped while preparing the mesh, before the
    mesher started, so there was nothing to check. THE NOTES ABOVE SAY WHAT IT STOPPED ON.

There were no notes above. Not one of "2.11 mm", "0.0982", "178,956,970", "400,000,000" or
"2 ranks" appeared anywhere on the page - the sentence pointed at an explanation that did not
exist. The cause and the retry policy were already right; only the words were missing.

So these tests pin the one thing that was broken: a refusal's words survive into the sentence the
user reads. They are deliberately about the SEAM rather than about one engine's arithmetic - the
arithmetic has its own tests, and it was not the arithmetic that was wrong.
"""
from __future__ import annotations

import json

import pytest

from meshpipeline.contracts.failure_cause import FailureCause, describe
from meshpipeline.engines.preflight import PREFLIGHT_RECORD, read_refusal

#: What the engine actually wrote on the customer part, kept verbatim.
WORDS = ("this part has two scales of passage - about 2.11 mm and 0.0982 mm - and no pair of cell "
         "sizes resolves the thin ones at 4 cells across inside a limit of 400,000,000 cells and "
         "this mesher's own ceiling of 178,956,970; a configuration that fits the cell limit was "
         "refused on the octree ceiling alone and would build on 2 ranks")

#: The figures a reader needs to act. Each one is in the engine's sentence, and NONE of them was
#: on the page.
FIGURES = ("2.11 mm", "0.0982", "400,000,000", "178,956,970", "2 ranks")


def _record(tmp_path, words: str = WORDS):
    """The record the driver writes, written the way the driver writes it."""
    from meshpipeline.engines.cfmesh_conjugate.driver import _say_why_before_meshing

    _say_why_before_meshing(tmp_path, words, job_id="test")
    return read_refusal(tmp_path)


def test_the_driver_writes_a_refusal_the_executor_can_read(tmp_path):
    ref = _record(tmp_path)
    assert ref is not None, "the executor reads this file; without it the words are lost"
    assert ref.cause == FailureCause.NOT_BUILT.value
    assert ref.gate, "a refusal with no gate is dropped by read_refusal"
    assert ref.facts.get("reason") == WORDS
    assert ref.facts.get("deterministic") is True, (
        "without this the pipeline offers a retry that would stop identically")


def test_the_users_sentence_carries_the_engines_own_words(tmp_path):
    """The whole point: describe() must say the measured reason, not the empty-handed fallback."""
    ref = _record(tmp_path)
    what, nxt = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert what, "an unknown cause returns empty strings and the caller keeps its generic headline"
    assert WORDS.rstrip(".") in what or WORDS in what, (
        "the engine's own sentence is not in the user's message: %r" % what)
    assert "The notes above say what it stopped on" not in what, (
        "the sentence that points at notes that do not exist is still being used")
    assert nxt, "a refusal must say what the user can do next"


@pytest.mark.parametrize("figure", FIGURES)
def test_every_figure_a_reader_needs_is_in_the_sentence(tmp_path, figure):
    """Each of these was measured, and each was absent from the page the customer saw."""
    ref = _record(tmp_path)
    what, _ = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert figure in what, "%r is not in the user's sentence" % figure


def test_a_refusal_with_no_words_writes_nothing(tmp_path):
    """Silence is better than a record that promises a reason and carries none.

    `read_refusal` drops a record with no gate or cause, but a record with an EMPTY reason would be
    read, and `describe` would fall back to the very sentence this work removed - while the retry
    policy now believed a refusal had been explained. Writing nothing leaves the old behaviour
    exactly as it was.
    """
    from meshpipeline.engines.cfmesh_conjugate.driver import _say_why_before_meshing

    _say_why_before_meshing(tmp_path, "   ", job_id="test")
    assert not (tmp_path / PREFLIGHT_RECORD).exists()
    assert read_refusal(tmp_path) is None


def test_the_record_is_json_the_executor_can_parse(tmp_path):
    """It crosses a process boundary as a file, so it has to be plain JSON with the keys read."""
    _record(tmp_path)
    d = json.loads((tmp_path / PREFLIGHT_RECORD).read_text(encoding="utf-8"))
    assert set(d) >= {"gate", "cause", "builder_text", "facts"}
    assert d["facts"]["reason"] == WORDS


def test_all_four_size_refusals_go_through_it():
    """Four places price this part and decline, and each had the same silence.

    A source check, deliberately: the alternative is to drive four different geometries through a
    whole driver, and what is being guarded is that nobody adds a fifth refusal that only logs.
    """
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as D

    lines = inspect.getsource(D).split("\n")
    logged = [ln for ln in lines
              if 'logger.info("conjugate: refusing to start - %s - job_id=%s"' in ln]
    # call sites only: the function's own `def` line names it too
    carried = [ln for ln in lines
               if "_say_why_before_meshing(ws," in ln and not ln.lstrip().startswith("def ")]
    assert len(logged) >= 4, "the refusal sites have changed shape; this test needs rewriting"
    assert len(carried) == len(logged), (
        "%d refusal(s) are logged but %d carry their words to the user"
        % (len(logged), len(carried)))
    # ...and each one carries ITS OWN words, not a neighbour's
    said = [ln.split("_say_why_before_meshing(ws,")[1].split(",")[0].strip() for ln in carried]
    assert len(set(said)) == len(said), (
        "two refusals carry the same variable, so one of them tells the user the wrong reason: %s"
        % said)
