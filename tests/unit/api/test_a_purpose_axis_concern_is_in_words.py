# The reviewer had the words and the API said it did not.
#
# MEASURED on job cba7ac69 (2026-11-25), the 279,281,786-cell Corintis cold plate delivered with
# concerns: the review ran SEVEN axes - four the engine owns and three the conjugate-heat-transfer
# purpose owns - and failed `near_interface_resolution`, which is one of the purpose's. The job
# status returned
#
#     reviewer_findings: ["The reviewer found a problem it could not put in words here"]
#
# while the same page, from the terminal record, showed the real sentence. `_failed_concerns` was
# looking failed axes up in `get_spec(engine).review_rubric`, which holds the engine's four only,
# so the three purpose-owned axes could never be found and its no-declared-concern fallback fired
# on a sentence that was declared all along.
from meshpipeline.api.v1.simulation import _failed_concerns
from meshpipeline.engines.quality_criteria import compose_review_rubric

ENGINE = "cfmesh_conjugate"
PURPOSE = "conjugate_heat_transfer"
#: What the API said instead of the axis's own words.
NO_WORDS = "The reviewer found a problem it could not put in words here"
#: The axis job cba7ac69 actually failed. The purpose owns it, not the engine.
FAILED_AXIS = "near_interface_resolution"


def _review(*failed):
    return {"axis_findings": [{"axis_key": k, "passed": False} for k in failed]
                             + [{"axis_key": "interface_continuity", "passed": True}]}


def test_the_axis_this_job_failed_is_the_purposes_and_not_the_engines():
    # If this ever stops being true the test below is measuring nothing.
    from meshpipeline.engines.registry import get_spec

    assert FAILED_AXIS not in {a.name for a in get_spec(ENGINE).review_rubric}
    assert FAILED_AXIS in {a.name for a in compose_review_rubric(ENGINE, PURPOSE)}


def test_a_failed_purpose_axis_says_what_it_means():
    got = _failed_concerns(_review(FAILED_AXIS), ENGINE, PURPOSE)
    assert got == ["The mesh is too coarse where the regions meet to resolve transfer across them"]


def test_the_sentence_is_the_axis_own_declared_concern():
    axis = next(a for a in compose_review_rubric(ENGINE, PURPOSE) if a.name == FAILED_AXIS)
    assert _failed_concerns(_review(FAILED_AXIS), ENGINE, PURPOSE) == [axis.concern]


def test_no_axis_a_conjugate_review_can_fail_is_left_without_words():
    # The whole rubric, one at a time: seven axes, seven sentences.
    names = [a.name for a in compose_review_rubric(ENGINE, PURPOSE)]
    assert len(names) == 7, names
    for name in names:
        got = _failed_concerns(_review(name), ENGINE, PURPOSE)
        assert got and got[0] != NO_WORDS, f"{name} has no words"


def test_an_engine_owned_axis_is_unchanged():
    got = _failed_concerns(_review("coolant_capture"), ENGINE, PURPOSE)
    assert got == ["The meshed coolant is not the shape of the part's passages"]


def test_a_run_with_no_recorded_purpose_still_reads_the_engines_own_axes():
    # An older row carries no purpose. Its engine axes must still resolve - only the purpose's
    # cannot, which is the behaviour that was there before and is not a regression.
    assert _failed_concerns(_review("coolant_capture"), ENGINE, "") == [
        "The meshed coolant is not the shape of the part's passages"]
    assert _failed_concerns(_review(FAILED_AXIS), ENGINE, "") == [NO_WORDS]


def test_an_unknown_purpose_does_not_break_the_lookup():
    assert _failed_concerns(_review("coolant_capture"), ENGINE, "no_such_purpose") == [
        "The meshed coolant is not the shape of the part's passages"]


def test_a_review_with_nothing_failed_says_nothing():
    assert _failed_concerns({"axis_findings": [
        {"axis_key": FAILED_AXIS, "passed": True}]}, ENGINE, PURPOSE) == []
    assert _failed_concerns({}, ENGINE, PURPOSE) == []
