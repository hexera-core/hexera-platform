"""Same script, same part, opposite outcome, decided by which turn the model recorded on.

MEASURED on ahmed_variant_001, four flow conversations driven with identical replies - "internal cfd,
air through it", then "you decide everything", then "go" to everything after:

  - two recorded the port roles on the turn "you decide everything" WAS the latest message, and
    submitted in five and six turns;
  - two did not, and could never submit at all. `role_problems` refuses every port role the customer
    has not confirmed, the delegation was no longer in `latest_user_message` so it could not be
    quoted, and both runs ended with "submit rejected - port roles the customer has not confirmed"
    twice over and then the turn cap. The customer was told "the system will not accept the port roles
    unless you write them yourself" four turns running.

A standing instruction was being read as a one-turn utterance.
"""
from __future__ import annotations

from meshpipeline.application.geometry_survey import said_by_customer

EARLIER = ("internal cfd, air through it", "you decide everything")


def test_the_latest_message_is_still_the_ordinary_proof():
    assert said_by_customer("o4 is the inlet", "o4 is the inlet, rest are outlets")


def test_a_quote_from_nowhere_is_still_refused():
    assert not said_by_customer("o4 is the inlet", "go", EARLIER)


def test_an_empty_quote_is_never_proof():
    """`"" in anything` is True, so this must not be decided by containment."""
    assert not said_by_customer("", "you decide everything", EARLIER)


def test_a_delegation_two_turns_back_is_still_quotable():
    assert said_by_customer("you decide everything", "go", EARLIER), (
        "the customer said it, in this conversation, about this part - and it says do not ask again")


def test_a_non_deferral_from_an_earlier_message_is_not():
    """The narrowness is the whole safety of it: only a deferral survives its turn."""
    earlier = ("internal cfd, air through it", "no, not that one", "the part is the fluid itself")
    assert not said_by_customer("the part is the fluid itself", "go", earlier)
    assert not said_by_customer("no, not that one", "go", earlier)
    assert not said_by_customer("internal cfd, air through it", "go", earlier)


def test_a_deferral_that_was_never_said_is_not_accepted_either():
    assert not said_by_customer("you decide everything", "go", ("internal cfd, air through it",))


def test_nothing_earlier_at_all_behaves_exactly_as_before():
    assert not said_by_customer("you decide everything", "go")
    assert not said_by_customer("you decide everything", "go", ())
