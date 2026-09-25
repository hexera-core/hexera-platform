"""A customer's "no" was read as permission to record our own proposal.

`accepts_a_proposal` has three branches. The first, `affirms`, refuses any message carrying a denial -
its own docstring says so: "'no, not that one' accepts nothing". The third read the customer's EARLIER
messages for a standing delegation and never consulted the latest one at all, so a refusal fell
straight through it.

MEASURED on the committed tree, earlier = ["internal cfd, air, you decide the rest"]:

    accepts_a_proposal("no, swap them")            -> True
    accepts_a_proposal("actually o3 is the inlet") -> True
    accepts_a_proposal("no")                       -> True

What that permits is `_the_proposal_recorded` writing the PLATFORM'S port roles as the customer's
answer on the turn they rejected them, which puts a boundary condition on a mouth they had just said
was wrong. A guard that the path beside it does not consult is this codebase's signature defect; here
it decides where the inlet goes.

A delegation does not expire - that is the measured argument `said_by_customer` makes and it stands -
but it is taken back the moment they say no.
"""
from __future__ import annotations

import pytest

from meshpipeline.application.geometry_survey import accepts_a_proposal

DELEGATED = ["internal cfd, air through it", "you decide the rest"]


@pytest.mark.parametrize("latest", ["no", "no, swap them", "nope", "not that one", "no thanks"])
def test_a_refusal_takes_the_delegation_back(latest):
    assert accepts_a_proposal(latest, DELEGATED) is False, (
        "their refusal was recorded as permission to write our own roles")


@pytest.mark.parametrize("latest", ["go", "yes", "sure", "ok", "you decide", "your call"])
def test_an_acceptance_or_a_fresh_deferral_still_accepts(latest):
    assert accepts_a_proposal(latest, DELEGATED) is True


def test_the_delegation_still_survives_a_turn_that_says_nothing_either_way():
    """The measured reason this branch exists: "everything else you decide" three turns ago stands."""
    assert accepts_a_proposal("go", DELEGATED) is True
    assert accepts_a_proposal("go", []) is True


def test_a_refusal_with_no_delegation_behind_it_accepts_nothing_either():
    assert accepts_a_proposal("no", []) is False


def test_the_denial_reader_is_the_one_affirms_uses():
    """A second opinion about what "no" means is a second thing to keep in step, and the first
    divergence would be silent and in a customer's mesh."""
    from meshpipeline.agents.intake.engine_selection import _DENY, affirms
    for word in sorted(_DENY):
        assert affirms(word) is False, f"{word!r} is a denial to affirms"
        assert accepts_a_proposal(word, DELEGATED) is False, (
            f"{word!r} is a denial to affirms but not to accepts_a_proposal")
