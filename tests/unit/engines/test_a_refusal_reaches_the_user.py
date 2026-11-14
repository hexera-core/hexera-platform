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
    # the reason is spliced after a full stop, so it is capitalised; the SUBSTANCE must be verbatim
    body = WORDS[1:].rstrip(".")
    assert body in what, (
        "the engine's own sentence is not in the user's message: %r" % what)
    assert what.count(body) == 1, "the reason is said twice"
    assert "The notes above say what it stopped on" not in what, (
        "the sentence that points at notes that do not exist is still being used")
    assert nxt, "a refusal must say what the user can do next"


@pytest.mark.parametrize("figure", FIGURES)
def test_every_figure_a_reader_needs_is_in_the_sentence(tmp_path, figure):
    """Each of these was measured, and each was absent from the page the customer saw."""
    ref = _record(tmp_path)
    what, _ = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert figure in what, "%r is not in the user's sentence" % figure


def test_the_sentence_does_not_blame_a_sound_file(tmp_path):
    """All four of these price a mesh against a limit of OURS, and the file is sound.

    The sentence NOT_BUILT otherwise reaches is "could not prepare its input from your file", which
    sends a customer hunting for a fault that does not exist: measured on the first real cold
    plate, where the file was perfect and the number that stopped it was a 32-bit label in our own
    build of OpenFOAM. The driver's own standard, from the comment beside its archive check, is
    "the reason names the setting rather than blaming the part".
    """
    ref = _record(tmp_path)
    assert ref.facts.get("priced_out") is True
    what, nxt = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert "from your file" not in what, "the refusal still blames the customer's file: %r" % what
    assert "limit on our side" in what
    assert "no time was spent on a mesh" in what, "a refusal before meshing costs nothing and should say so"
    assert nxt and "coarser" in nxt, "the way forward is not offered"


def test_the_engine_is_named_the_way_a_human_reads_it(tmp_path):
    """`describe` prefers facts["engine"] over the label the caller passes, and the caller passes
    the readable one (registry.engine_label). Putting the registry key in the facts made the page
    say "cfmesh_conjugate" to a customer."""
    ref = _record(tmp_path)
    assert "engine" not in ref.facts, (
        "a registry key in the facts overrides the human label the caller supplies")
    what, _ = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert "Conjugate (coolant + metal)" in what
    assert "cfmesh_conjugate" not in what


def test_the_reason_reads_as_a_sentence(tmp_path):
    """It is spliced mid-paragraph after a full stop, so it starts with a capital."""
    ref = _record(tmp_path)
    what, _ = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert "starting. This part has two scales" in what, (
        "the reason is spliced in lower-case after a full stop: %r" % what[:220])


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



# #
# ...AND A REFUSAL ABOUT THE SHAPE MUST NOT BORROW THE SIZE REFUSALS' SENTENCES
# #
#: What the engine wrote on job ca246a07, 2026-11-13, the first part refused for its shape.
SHAPE_WORDS = (
    "the inlet and the outlet open into different pieces of the coolant, 0.580 mm apart at their "
    "closest, and no face joins them: inlet opens into a piece of 52,140.3 mm3 and outlet into "
    "one of 1,496.8 mm3, out of 2 the surface is in. Nothing can flow from one to the other, so "
    "no mesh of this surface can be solved however well it is built. A gap under a millimetre is "
    "a wall the assembly leaves in and a hole has to be opened through it; a wide one means the "
    "two are separate circuits, and each needs its own inlet and outlet")

#: The three claims the customer was shown instead, every one of them false here: nothing was
#: priced, the limit is not ours, and no cell count makes a severed passage flow. Matched as the
#: claims they are and not as loose words - the shape refusal is allowed to SAY "a coarser mesh"
#: in the course of ruling one out, and must not OFFER one.
WRONG = ("priced the mesh this part would need", "limit on our side",
         "Tell me if a coarser mesh would do", "raising the limit")


def _shape_record(tmp_path):
    from meshpipeline.engines.cfmesh_conjugate.driver import _say_why_before_meshing

    _say_why_before_meshing(tmp_path, SHAPE_WORDS, job_id="test",
                            priced_out=False, geometry=True)
    return read_refusal(tmp_path)


def test_a_shape_refusal_is_not_filed_as_a_price(tmp_path):
    ref = _shape_record(tmp_path)
    assert ref.facts.get("priced_out") is False
    assert ref.facts.get("geometry_refusal") is True
    assert ref.facts.get("deterministic") is True


def test_the_shape_refusal_says_none_of_the_three_false_things(tmp_path):
    ref = _shape_record(tmp_path)
    what, nxt = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    page = f"{what} {nxt}"
    for wrong in WRONG:
        assert wrong not in page, f"the shape refusal still says {wrong!r}: {page}"


def test_the_shape_refusal_still_carries_every_measurement(tmp_path):
    ref = _shape_record(tmp_path)
    what, _nxt = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    for figure in ("0.580 mm", "52,140.3 mm3", "1,496.8 mm3"):
        assert figure in what, f"{figure} is missing from the sentence the user reads: {what}"


def test_the_shape_refusal_does_not_blame_the_file_vaguely_either(tmp_path):
    """"could not prepare its input from your file" is the fallback, and it is no better: it sends
    the reader hunting without saying what to look for. The reason itself says what to look for."""
    ref = _shape_record(tmp_path)
    what, nxt = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert "could not prepare its input" not in what
    assert "Nothing was spent on a mesh" in what
    assert "about the shape" in nxt and "one connected passage" in nxt


def test_a_size_refusal_is_untouched_by_any_of_this(tmp_path):
    """The four that ARE priced keep their own sentence, word for word."""
    ref = _record(tmp_path)
    what, nxt = describe(ref.cause, ref.facts, engine="Conjugate (coolant + metal)")
    assert "priced the mesh this part would need" in what
    assert "limit on our side" in what
    assert "coarser" in nxt
