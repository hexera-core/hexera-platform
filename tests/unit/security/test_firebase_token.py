# Responsibility: Verify that only a genuine, current, correctly-addressed Identity Platform token is accepted.
from __future__ import annotations

import time

import pytest

from meshpipeline.adapters.firebase_token import identity_platform

_PROJECT = "hexera-dev"


class _StubJWT:
    """Stands in for google.auth.jwt.decode, which is the one thing here that needs a key."""

    def __init__(self, claims=None, error=None):
        self.claims = claims
        self.error = error
        self.calls: list = []

    def __call__(self, token, certs=None, audience=None):
        self.calls.append({"token": token, "certs": certs, "audience": audience})
        if self.error is not None:
            raise self.error
        return dict(self.claims)


def _claims(**overrides):
    now = int(time.time())
    base = {
        "iss": f"https://securetoken.google.com/{_PROJECT}",
        "aud": _PROJECT,
        "sub": "firebase-uid-1",
        "user_id": "firebase-uid-1",
        "email": "Person@Example.com",
        "email_verified": True,
        "name": "A Person",
        "iat": now - 10,
        "exp": now + 3600,
    }
    base.update(overrides)
    return base


def _verify(monkeypatch, claims=None, error=None):
    stub = _StubJWT(claims=claims if claims is not None else _claims(), error=error)
    monkeypatch.setattr(identity_platform, "_decode", stub)
    return stub


def test_a_genuine_token_yields_the_identity_it_asserts(monkeypatch):
    _verify(monkeypatch)
    result = identity_platform.verify("raw", project_id=_PROJECT,
                                   certs_provider=lambda: {"kid": "cert"})
    assert result.uid == "firebase-uid-1"
    assert result.email_verified is True
    assert result.name == "A Person"


def test_the_email_is_normalised_the_way_owner_id_is(monkeypatch):
    # owner_id is the lowercased email everywhere in this schema. A token asserting mixed case
    # must not produce a second, differently-spelled tenant.
    _verify(monkeypatch)
    result = identity_platform.verify("raw", project_id=_PROJECT,
                                   certs_provider=lambda: {"kid": "cert"})
    assert result.email == "person@example.com"


def test_a_token_addressed_to_another_project_is_refused(monkeypatch):
    _verify(monkeypatch, claims=_claims(iss="https://securetoken.google.com/someone-else"))
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_the_audience_is_checked_by_the_decoder_not_by_us(monkeypatch):
    # google.auth.jwt.decode enforces `aud`; passing it is how that happens. If this argument
    # ever stopped being passed, any project's token would verify here.
    stub = _verify(monkeypatch)
    identity_platform.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})
    assert stub.calls[0]["audience"] == _PROJECT


def test_a_token_with_no_subject_is_refused(monkeypatch):
    _verify(monkeypatch, claims=_claims(sub="", user_id=""))
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_a_token_with_no_email_is_refused(monkeypatch):
    # owner_id IS the email. A token without one cannot name a tenant.
    _verify(monkeypatch, claims=_claims(email=""))
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_a_forged_or_expired_token_is_refused_as_one_kind_of_refusal(monkeypatch):
    _verify(monkeypatch, error=ValueError("Token expired"))
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify("raw", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})


def test_an_empty_token_never_reaches_the_decoder(monkeypatch):
    stub = _verify(monkeypatch)
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify("", project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})
    assert stub.calls == []


def test_a_none_token_is_refused_like_an_empty_one(monkeypatch):
    # The signature is typed `str`, and `(raw_token or "").strip()` already tolerates `None` -
    # but on a security boundary that guarantee belongs in a test, not left implicit in the code.
    stub = _verify(monkeypatch)
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify(None, project_id=_PROJECT, certs_provider=lambda: {"kid": "cert"})
    assert stub.calls == []


def test_a_cert_provider_failure_is_not_mistaken_for_a_forged_token(monkeypatch):
    # A cert-endpoint outage, or a bug inside google_certs(), is an infrastructure/programming
    # fault - not evidence of a forged token - and must surface as itself, not be relabelled
    # InvalidToken. Collapsing the two would hide, forever, whether sign-ins are failing because
    # Google's certs are unreachable or because someone is sending us forged tokens.
    def broken_provider():
        raise RuntimeError("cert endpoint unreachable")

    with pytest.raises(RuntimeError, match="cert endpoint unreachable"):
        identity_platform.verify("raw", project_id=_PROJECT, certs_provider=broken_provider)


def test_no_configured_project_refuses_rather_than_accepting_anything(monkeypatch):
    stub = _verify(monkeypatch)
    with pytest.raises(identity_platform.InvalidToken):
        identity_platform.verify("raw", project_id="", certs_provider=lambda: {"kid": "cert"})
    assert stub.calls == []


def test_the_certificates_are_fetched_once_and_reused_within_the_ttl(monkeypatch):
    fetches: list = []

    def fake_get(url, timeout=None):
        fetches.append(url)

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"kid-1": "-----BEGIN CERTIFICATE-----"}

        return _Response()

    identity_platform.reset_cert_cache()
    monkeypatch.setattr(identity_platform.httpx, "get", fake_get)
    monkeypatch.setattr(identity_platform, "_now_monotonic", lambda: 1000.0)
    assert identity_platform.google_certs() == {"kid-1": "-----BEGIN CERTIFICATE-----"}
    assert identity_platform.google_certs() == {"kid-1": "-----BEGIN CERTIFICATE-----"}
    assert len(fetches) == 1, "the certificates were re-fetched inside their own TTL"


def test_the_certificates_are_refetched_once_the_ttl_lapses(monkeypatch):
    fetches: list = []
    clock = {"t": 1000.0}

    def fake_get(url, timeout=None):
        fetches.append(url)

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"kid-1": "cert"}

        return _Response()

    identity_platform.reset_cert_cache()
    monkeypatch.setattr(identity_platform.httpx, "get", fake_get)
    monkeypatch.setattr(identity_platform, "_now_monotonic", lambda: clock["t"])
    identity_platform.google_certs()
    clock["t"] = 1000.0 + identity_platform.CERT_TTL_SECONDS + 1
    identity_platform.google_certs()
    assert len(fetches) == 2


def test_the_module_does_not_import_requests():
    # requests is transitive-only in this project (constraints.txt, not runtime.txt). Importing
    # it directly makes the runtime depend on whatever google-cloud-storage happens to pull.
    import pathlib
    src = pathlib.Path(identity_platform.__file__).read_text()
    assert "import requests" not in src
