"""A rejection the reviewer cannot act on ends the review with no verdict."""
import inspect

from meshpipeline.agents.reviewer import loop_policy as LP


def test_a_clean_deficit_rejection_carries_the_eligibility_reason():
    """The deficit text names axes. When there are none, it names nothing.

    evaluate_eligibility can refuse a submission that is perfectly well-formed - every required
    axis present, grounded, unique, every evidence id known. The deficit-based correction then
    reads "The listed axes need a corrected finding" with no axes listed, which the model cannot
    act on: it resubmits the same findings, is refused identically, and the review ends at the
    round limit. A mesh that passed every other gate went undelivered that way.
    """
    src = inspect.getsource(LP.ReviewLoopPolicy._submit)
    assert "correction_message" in src, (
        "the eligibility correction never reaches the reviewer - it is composed for this moment "
        "and was being discarded, leaving the deficit text to explain a rejection it did not make")
    assert "deficits.clean" in src, (
        "a clean-deficit rejection must not be explained by the deficit text, which names nothing")


def test_the_eligibility_decision_still_carries_a_correction():
    """The field exists to be delivered; a decision that refuses must say why."""
    from meshpipeline.agents.reviewer.eligibility import EligibilityDecision
    assert "correction_message" in EligibilityDecision.__dataclass_fields__
