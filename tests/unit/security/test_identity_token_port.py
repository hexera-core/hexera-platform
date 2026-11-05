# Responsibility: Verify the neutral sign-in port delegates to whatever verifier composition bound, and refuses honestly when none is.
from __future__ import annotations

import pytest

from meshpipeline.contracts import firebase_token


@pytest.fixture(autouse=True)
def _unbound():
    # The port is process-global, like every other contract binding. Restore whatever this
    # process had so a test that binds a stub cannot leak into an unrelated one.
    saved = firebase_token._verifier
    firebase_token.set_token_verifier(None)
    yield
    firebase_token.set_token_verifier(saved)


def test_the_port_hands_the_token_to_the_bound_verifier():
    seen: list = []

    def stub(raw_token, *, project_id):
        seen.append((raw_token, project_id))
        return firebase_token.VerifiedToken(uid="u", email="a@b.c", email_verified=True, name="A")

    firebase_token.set_token_verifier(stub)
    result = firebase_token.verify("raw", project_id="hexera-dev")

    assert seen == [("raw", "hexera-dev")]
    assert result.uid == "u"


def test_an_unconfigured_deployment_is_not_reported_as_a_forged_token():
    # NoTokenVerifier, never InvalidToken. api/auth.py catches InvalidToken and answers 401 with
    # the same refusal a forgery gets; if a missing binding arrived that way, our own
    # misconfiguration would be indistinguishable - in the log and to the user - from an attack,
    # and every sign-in would fail with a message saying the token was bad. It was not.
    with pytest.raises(firebase_token.NoTokenVerifier):
        firebase_token.verify("raw", project_id="hexera-dev")

    assert not issubclass(firebase_token.NoTokenVerifier, firebase_token.InvalidToken)
