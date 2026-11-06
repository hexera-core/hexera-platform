# Responsibility: Verify the signup-grant claim normalises addresses the way a credit farmer varies them.
from __future__ import annotations

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import signup_claims


def test_plus_tags_are_one_mailbox_on_every_domain():
    assert (signup_claims.normalized_mailbox("ada+bots@example.com")
            == signup_claims.normalized_mailbox("ada@example.com"))


def test_gmail_dots_and_googlemail_are_one_mailbox():
    assert signup_claims.normalized_mailbox("A.D.A@GoogleMail.com") == "ada@gmail.com"


def test_dots_elsewhere_are_distinct_mailboxes():
    # Most providers DO distinguish dots; collapsing them would deny a real second person.
    assert (signup_claims.normalized_mailbox("a.da@example.com")
            != signup_claims.normalized_mailbox("ada@example.com"))


def test_the_digest_never_contains_the_address():
    digest = signup_claims.claim_digest("ada@example.com")
    assert "ada" not in digest and len(digest) == 64


def test_withheld_domains_match_subdomains_but_not_lookalikes(monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_WITHHELD_DOMAINS", "mailinator.com, yopmail.com")
    assert signup_claims.grant_withheld_domain("x@mailinator.com")
    assert signup_claims.grant_withheld_domain("x@eu.mailinator.com")
    assert not signup_claims.grant_withheld_domain("x@notmailinator.com")
    assert not signup_claims.grant_withheld_domain("x@example.com")


def test_an_address_without_a_domain_is_withheld():
    assert signup_claims.grant_withheld_domain("no-at-sign")
