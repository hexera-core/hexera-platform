# Responsibility: keep our machinery out of the one screen a customer approves spend on.
# Boundaries: the render boundary in the intake executor; what the cards SAY is authored in the agent package.
"""Fifteen of thirty-six runs printed our source code at the customer.

MEASURED on the 36-run batch of 2026-09-27, by grepping the replies the customers actually got:

    a .py source path      9 of 36 runs
    a file:line reference  9 of 36 runs
    a dev-plan item        8 of 36 runs
    another job's stats    1 of 36 runs
    an internal constant   1 of 36 runs
    at least one of them  15 of 36 runs

`agent_words_for_confirmation` sits twenty lines below a docstring that says of the geometry step's
failure reason: "naming it here would spend the customer's attention on our internals at the one
moment they are deciding whether to spend money." That judgement was right. The risk cards then went
out unfiltered directly underneath it, carrying file names, line ranges, dev-plan numbers, parameter
keys and one sentence quoting other customers' failure rates.

EVERY FIXTURE BELOW IS VERBATIM from a real reply, not invented. That matters: a filter tested against
strings its author imagined is a filter tested against its author's imagination.
"""
from __future__ import annotations

from meshpipeline.agents.intake.executor import agent_words_for_confirmation, customer_safe

#: Verbatim from bend_elbow_003_p3, turn 4.
LEAK_SOURCE_PATHS = (
    "The classifier's CARVE LEAKED repair asks for a larger domain_margin (judge.py:42-48); "
    "render_internal_case reads no domain_margin and pads by max(2 base_cell, 0.03 maxext) "
    "(snappy_runner.py:619), so on this path cells_across_diameter is the key that changes the "
    "surface that seals")

#: Verbatim from cht_block_round_channel_p3, turn 4. This one quotes OTHER PEOPLE'S JOBS.
LEAK_OTHER_CUSTOMERS = (
    "The only cell gate in the platform is CELL_HARD_LIMIT 8,000,000 (settings/inventory.py:384 "
    "calls it the only cell-count gate) and the customer's cap has no reader anywhere, so this "
    "number is the agent's to hold: 36 of 131 measured deliveries in the last run were over their "
    "stated cap and every one of them passed every gate.")

#: Verbatim, in 7 of the 36 runs.
LEAK_DEV_PLAN = "Use this seed as locationInMesh (the step-8 hook, dev plan 8.7); until then it is advisory."

#: What a good card looks like: a fact about THEIR part, and what it costs them. Verbatim.
GOOD_CARD = (
    "The outlet run at o5 is 1.0 D, short of the 5 D needed to settle, so the pressure drop and the "
    "flow split reported at o5 are read off a face the flow has not settled on.")


def test_a_source_path_never_reaches_them():
    said = customer_safe(LEAK_SOURCE_PATHS)
    for internal in ("judge.py", "snappy_runner.py", ":42-48", ":619"):
        assert internal not in said, said


def test_another_customers_statistics_never_reach_them():
    said = customer_safe(LEAK_OTHER_CUSTOMERS)
    assert "131" not in said and "measured deliveries" not in said, said
    assert "inventory.py" not in said, said


def test_a_dev_plan_item_never_reaches_them():
    assert customer_safe(LEAK_DEV_PLAN) == "", customer_safe(LEAK_DEV_PLAN)


def test_a_card_that_is_only_mechanism_becomes_nothing_rather_than_an_empty_bullet():
    """The caller prints "  - " + text, so a half-filtered card must not leave a dangling bullet."""
    assert customer_safe("See judge.py:42 for the detail.") == ""
    assert customer_safe("") == ""
    assert customer_safe(None) == ""


def test_what_the_customer_needs_to_know_survives_untouched():
    """The whole point. A filter that also eats the finding is worse than the leak."""
    assert customer_safe(GOOD_CARD) == GOOD_CARD


def test_the_customer_facing_half_of_a_mixed_card_survives():
    mixed = ("The seed point sits outside the fluid volume, so the mesher would carve the wrong side. "
             "Use this seed as locationInMesh (the step-8 hook, dev plan 8.7); until then it is advisory.")
    said = customer_safe(mixed)
    assert said.startswith("The seed point sits outside the fluid volume")
    assert "dev plan" not in said and "locationInMesh" not in said


def test_it_runs_on_the_real_path_and_not_only_in_this_test():
    """The filter is worth nothing if the confirmation does not call it."""
    step = {"status": "planned",
            "plan": {"summary_for_user": "An elbow, air in at o1 and out at o4.",
                     "risks": [{"severity": "high", "effect": "changes_bc",
                                "consequence": GOOD_CARD,
                                "recommendation": LEAK_DEV_PLAN}]}}
    said = agent_words_for_confirmation(step)
    assert "o5 are read off a face the flow has not settled on" in said
    assert "dev plan" not in said and "locationInMesh" not in said


def test_a_risk_whose_whole_recommendation_was_mechanism_still_shows_its_finding():
    step = {"status": "planned",
            "plan": {"summary_for_user": "",
                     "risks": [{"severity": "high", "effect": "changes_bc",
                                "consequence": GOOD_CARD, "recommendation": "See judge.py:42."}]}}
    said = agent_words_for_confirmation(step)
    assert "flow has not settled on" in said
    assert "judge.py" not in said
    assert "-> " not in said, "an empty recommendation must not leave a dangling arrow"


def test_the_seed_card_goes_whole_rather_than_leaving_its_mechanism_behind():
    """MEASURED LIVE on 2026-09-28, on the owner's own run on a hydrogen manifold. The seed_point card is
    three sentences. The filter caught the first, because it names locationInMesh, and printed the other two
    onto the approval screen:

        Ray parity was not run here (no mesh file on disk); the handover's seed_check runs it. The
        platform's own hollow-wall fallback walks the inlet-to-outlet chord, which on a bend is the air
        between the legs (21 seedfix reruns in the export).

    Which is this filter's own docstring warning about orphans, arriving as two of them. Nothing in that card
    was ever the customer's to read: its own recommendation calls it a builder hook, advisory until a later
    step. So it goes whole rather than losing its head.
    """
    card = ("The carve seed (snappy's locationInMesh) is 0.25 bores inside the inlet o6 along its inward "
            "normal. Ray parity was not run here (no mesh file on disk); the handover's seed_check runs it. "
            "The platform's own hollow-wall fallback walks the inlet-to-outlet chord, which on a bend is the "
            "air between the legs (21 seedfix reruns in the export).")
    assert customer_safe(card) == "", "every sentence of this card is mechanism"

    # the variant a run WITH a mesh file on disk produces, which would otherwise survive as an orphan
    tested = ("The carve seed (snappy's locationInMesh) is 0.25 bores inside the inlet o6 along its inward "
              "normal. By ray parity it lies inside the carved cavity.")
    assert customer_safe(tested) == ""


def test_the_corpus_is_not_quoted_at_the_person_paying_for_this_job():
    """`in the export` sits beside `measured deliveries` and `in the last run were` for one reason: all three
    quote OTHER people's jobs at the customer deciding whether to spend money on theirs."""
    assert customer_safe("Twenty-one seedfix reruns in the export say otherwise.") == ""


def test_a_sentence_of_real_physics_still_survives_the_filter():
    """The direction that matters. A leak filter that eats the customer's own findings is worse than the leak,
    so this is the text the same live run DID deserve, and it comes through untouched."""
    real = ("If o5 is an outlet, the flow leaving through it has had 1.0 diameters since a change in "
            "cross-section, short of the 5 diameters it takes to settle. A pressure outlet holds one static "
            "pressure across the whole face and treats the profile there as settled and parallel.")
    assert customer_safe(real) == real
