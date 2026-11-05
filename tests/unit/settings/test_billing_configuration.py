# Responsibility: Verify billing is only "enabled" when it can complete a purchase end to end.
from __future__ import annotations

import pytest

import meshpipeline.settings.billing as billcfg


@pytest.fixture
def cfg(monkeypatch):
    def _set(api_key: str, webhook_secret: str):
        monkeypatch.setattr(billcfg, "STRIPE_API_KEY", api_key)
        monkeypatch.setattr(billcfg, "STRIPE_WEBHOOK_SECRET", webhook_secret)
    return _set


def test_both_credentials_enable_billing(cfg):
    cfg("rk_test_x", "whsec_y")
    assert billcfg.enabled() is True


def test_no_credentials_is_the_ordinary_disabled_state(cfg):
    # Every developer checkout and every test run looks like this. It is not a misconfiguration.
    cfg("", "")
    assert billcfg.enabled() is False


def test_an_api_key_without_a_webhook_secret_does_not_enable_billing(cfg):
    # THE DANGEROUS HALF-CONFIGURED STATE. With a key and no signing secret, checkout succeeds and
    # the customer is charged - then every fulfilment webhook fails verification at the door. The
    # subscription is never recorded, the plan never moves, the allowance is never granted: a paying
    # customer sitting on the free tier, with no error anywhere naming the cause.
    cfg("rk_test_x", "")
    assert billcfg.enabled() is False


def test_a_webhook_secret_without_an_api_key_does_not_enable_billing(cfg):
    # The mirror case: nothing can be sold, so nothing should claim to be sellable.
    cfg("", "whsec_y")
    assert billcfg.enabled() is False


@pytest.mark.parametrize("blank", ["   ", "\t", "\n"])
def test_whitespace_is_not_a_credential(cfg, blank):
    # An operator who set the variable to an empty string in a YAML block gets the disabled state,
    # not a client constructed around a key made of spaces.
    cfg("rk_test_x", blank)
    assert billcfg.enabled() is False
