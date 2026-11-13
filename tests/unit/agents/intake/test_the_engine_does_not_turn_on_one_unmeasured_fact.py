# Responsibility: Verify the measured engine recommendation for a cooled solid is the same whether
# the coolant was measured or not, and that it is the format filter rather than a turf tie that
# makes it so.
# Boundaries: it runs the real ranker through agents/intake/measured_choice.rank - no stub table,
# no stub registry - and asserts on the engine it returns. Nothing is read out of source text.
"""The same cold plate got two different meshers depending on one fact nobody had measured.

MEASURED ON THE LIVE RANKER, 2026-11-12, same six-solid cold plate, same case, one fact changed:

    coolant_inside=True   1. cfmesh_conjugate [RECOMMENDED]  2. snapgrid  3. snappy_multiregion
    coolant_inside=None   1. snapgrid [RECOMMENDED]          2. cfmesh_conjugate  3. snappy_multiregion

The snap grid is the ECXML electronics mesher. It was being recommended for a STEP cold plate,
which is the exact shape already on the record in cfmesh_conjugate/spec's own comments - fixed
there for the MEASURED case only, by making `coolant_inside` the fact the turf rows turn on.

WHY THE UNMEASURED CASE WENT THE OTHER WAY. Three things lined up:

  * the snap grid declares NO home_turf rows at all, so home_turf.verdict calls it "home" with
    nothing unmeasured, and fitness reads that as a blank turf rather than as a claim;
  * every one of the conjugate mesher's rows goes silent without the fact, so its turf is blank
    too, and the two tie;
  * the sort key then falls through to the declared ladder_rank, where the snap grid's 60 beats
    the conjugate mesher's 65 - a tie broken by a number that was never about this case.

`agent._coolant_inside` returns None whenever the stored check cannot be read: the check disabled,
the check failed, or a solid assembly settled in chat rather than measured. So this was not a rare
path.

THE FIX IS THE ENGINE'S OWN DECLARATION, FIRING. The snap grid already declares
reads_source_formats=("ecxml",) and base._admit_measured already refuses any other format - but
only when the facts NAME the format, and `measured_choice.able_engines` passed no facts at all.
It passes the format now, so the snap grid is refused for a STEP file before any turf is compared,
and the two readings agree. The second half is in the engine spec: the `n_solids == 1` home row
went, because one solid is not the coolant and the row claimed home on parts the driver refuses.

WHAT THIS FILE WILL NOT ACCEPT: a test that greps a source file. This branch already shipped a
feature whose guard test read inspect.getsource and passed while the code path was dead.
"""
from __future__ import annotations

import pytest

from meshpipeline.agents.intake import measured_choice as mc

CONJUGATE = "cfmesh_conjugate"
SNAPGRID = "snapgrid"
MULTIREGION = "snappy_multiregion"
MULTI_ENGINES = (CONJUGATE, SNAPGRID, MULTIREGION)

#: The case a cold plate arrives as: the kind and purpose the geometry check confirms for one
#: channelled solid, and the file it comes in.
CASE = {"purpose": "conjugate_heat_transfer", "input_kind": "solid-assembly", "form": "cad"}


def _rank(coolant, source_format="step", engines=MULTI_ENGINES):
    """The real ranker, with no file to measure: the flow, the kind and the format alone. That is
    the leanest input it will take a decision on, so a difference here is a difference in the
    DECISION and not in the measurement."""
    return mc.rank(path="", engines=list(engines), source_format=source_format,
                   coolant_inside=coolant, **CASE)


# #
# the defect: one unmeasured fact, two different meshers
# #
@pytest.mark.parametrize("coolant", [True, None])
def test_a_step_cold_plate_gets_the_conjugate_mesher_either_way(coolant):
    rec = _rank(coolant)
    assert rec is not None and rec.engine == CONJUGATE, (
        f"coolant_inside={coolant!r} recommended {rec.engine!r}: {rec.reason}")


def test_the_two_readings_give_the_same_ranking_and_not_just_the_same_winner():
    """Determinism is the whole order, not the top row: the alternatives are shown to the user."""
    assert _rank(True).ranking == _rank(None).ranking


def test_the_electronics_snap_grid_is_not_offered_for_a_step_file_at_all():
    """Not merely ranked second. It cannot read the file, so offering it as an alternative is a
    dead end the user would walk into at the submission."""
    for coolant in (True, None, False):
        assert SNAPGRID not in _rank(coolant).ranking, coolant


# #
# the mechanism: the engine's own declaration, fired by the format
# #
def test_the_filter_is_what_drops_it_and_the_filter_is_the_format():
    """Executed, not read: able_engines with and without the format on the same case."""
    blind = mc.able_engines(MULTI_ENGINES, CASE["purpose"], CASE["input_kind"], CASE["form"])
    named = mc.able_engines(MULTI_ENGINES, CASE["purpose"], CASE["input_kind"], CASE["form"],
                            source_format="step")
    assert SNAPGRID in blind, "the format was already being passed; this test measures nothing"
    assert SNAPGRID not in named
    assert [e for e in blind if e != SNAPGRID] == named, (
        "the format filter took an engine with it that reads STEP perfectly well")


def test_the_guard_only_fires_on_a_format_the_engine_does_not_read():
    """An ECXML upload is the snap grid's own case and nothing about it moves."""
    able = mc.able_engines(MULTI_ENGINES, CASE["purpose"], CASE["input_kind"], CASE["form"],
                           source_format="ecxml")
    assert SNAPGRID in able
    for coolant in (True, None):
        assert _rank(coolant, source_format="ecxml").engine == SNAPGRID, coolant


def test_an_unnamed_format_refuses_nothing():
    """A file whose format nothing could name is not a reason to drop an engine: the filter says
    what it knows, and silence is not evidence."""
    assert sorted(mc.able_engines(MULTI_ENGINES, CASE["purpose"], CASE["input_kind"],
                                  CASE["form"], source_format="")) == sorted(MULTI_ENGINES)


def test_the_format_is_read_off_the_upload_when_the_caller_does_not_state_it():
    """`rank` reads it from the staged path, so a real upload needs no caller to name it."""
    assert mc.source_format_of("/jobs/s/ColdPlateAssembly_CLEAN.step") == "step"
    assert mc.source_format_of("/jobs/s/set_top_box.ecxml") == "ecxml"


class _FetchedBack:
    """An approved source whose suffix hint is empty - what a fetched-back upload looks like.

    application/geometry_materializer.session_upload_path names the copy `intake_copy` plus
    safe_suffix(ref.suffix_hint), so an empty or unsafe hint leaves a staged path with no suffix
    at all. The original name is still on the ref.
    """

    suffix_hint = ""
    original_filename = "ColdPlateAssembly_CLEAN.step"


def test_the_staged_path_is_not_the_only_reading_of_the_format():
    """One candidate was all `rank` read, while every sibling reading here takes three.

    MEASURED 2026-11-12: source_format_of("/jobs/s/intake_copy") == '', and the same source ref's
    original name reads "step". `upload_form` (the kind of file) takes staged, suffix_hint and
    original_filename; agents/intake/executor.py takes the same three for this very fact. The
    format did not, so on a fetched-back upload with no usable suffix hint it named nothing.
    """
    ref = _FetchedBack()
    assert mc.source_format_of("/jobs/s/intake_copy") == ""
    assert mc.upload_format("/jobs/s/intake_copy", ref) == "step"
    # the staged file still speaks first, which is the order `upload_form` uses
    assert mc.upload_format("/jobs/s/set_top_box.ecxml", ref) == "ecxml"
    # ...and nothing to read is still nothing: silence is not evidence
    assert mc.upload_format("", None) == ""


def test_an_unsuffixed_staged_upload_still_refuses_the_electronics_snap_grid():
    """The whole point of the format filter, run through the real ranker on the path that lost it.

    MEASURED 2026-11-12, the same cold-plate case with the staged copy named `intake_copy`:

        source_ref absent   1. snapgrid [RECOMMENDED]  2. cfmesh_conjugate  3. snappy_multiregion
        source_ref present  1. cfmesh_conjugate        2. snappy_multiregion

    which is the identical wrong answer the measured-fact defect gave, reached by a different
    route: the guard cannot fire on a format nothing named.
    """
    blind = mc.rank(path="/jobs/s/intake_copy", engines=list(MULTI_ENGINES),
                    coolant_inside=None, **CASE)
    named = mc.rank(path="/jobs/s/intake_copy", engines=list(MULTI_ENGINES),
                    coolant_inside=None, source_ref=_FetchedBack(), **CASE)
    assert SNAPGRID in blind.ranking, (
        "the staged path alone already named the format; this test measures nothing")
    assert SNAPGRID not in named.ranking, named.ranking
    assert named.engine == CONJUGATE, named.reason


# #
# and the engine that IS for a cold plate still says where it is at home
# #
def test_the_measured_coolant_is_still_this_engines_home_turf():
    """The fix must not have been to stop claiming anything. With the coolant measured the engine
    leads ON ITS OWN CLAIM, and the reason says so."""
    rec = _rank(True)
    fit = rec.fit_of(CONJUGATE)
    assert fit is not None and fit.turf == "home", fit
    assert fit.turf_words, "the home claim carries no words for the user to read"


def test_without_the_measurement_it_leads_without_claiming_anything():
    """A part nobody has looked inside: it is still the right engine, and it says nothing it
    cannot support. The claim is the other engines' declarations, not its own."""
    fit = _rank(None).fit_of(CONJUGATE)
    assert fit is not None and fit.turf == "", fit
    assert fit.recommended


def test_a_file_whose_fluid_is_a_body_is_still_handed_to_the_assembly_mesher():
    """The other half of the measurement, unchanged: coolant measured ABSENT means the regions are
    already in the file, and this engine is outside its own turf there."""
    rec = _rank(False, engines=(CONJUGATE, MULTIREGION))
    assert rec.fit_of(CONJUGATE).turf == "outside", rec.fit_of(CONJUGATE)
    assert rec.engine != CONJUGATE


def test_one_solid_is_no_longer_a_home_claim_of_its_own():
    """The loose row: `n_solids == 1 -> home` with no coolant precondition. One solid is not the
    coolant - a plate with open-topped grooves and no lid is one solid with no enclosed coolant at
    all, and the engine's own advisory says it cannot mesh one. Asserted through the turf verdict,
    which is what the ranker reads."""
    from meshpipeline.engines.capability import FLOW_MULTI_REGION
    from meshpipeline.engines.home_turf import verdict
    from meshpipeline.engines.registry import get_spec

    spec = get_spec(CONJUGATE)
    assert not any(r.fact == "n_solids" for r in spec.home_turf), (
        "the solid-count row is back; it claims this engine's turf off something that is not the "
        "coolant")
    got = verdict(spec, FLOW_MULTI_REGION,
                  {"flow": FLOW_MULTI_REGION, "form": "cad", "input_kind": "solid-assembly",
                   "n_solids": 1})
    assert not (got.strengths + got.weak_spots + got.outside), (
        f"a part nobody measured held {[r.fact for r in got.strengths]}")
    assert "coolant_inside" in got.unmeasured
