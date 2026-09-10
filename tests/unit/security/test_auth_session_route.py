# Responsibility: Verify the console's sign-in endpoint refuses everything it should and discloses nothing.
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import auth as auth_route
from meshpipeline.application.account_service import Account, SignupDisabled
from meshpipeline.contracts.firebase_token import InvalidToken, VerifiedToken

pytestmark = pytest.mark.asyncio

_ACCOUNT = Account(user_id="user-1", owner_id="person@example.com",
                   organization_id="org-1", email_verified=True, name="A Person",
                   provisioned=False)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "console-key")
    monkeypatch.setattr(polcfg, "FIREBASE_PROJECT_ID", "hexera-dev")

    def fake_verify(raw, *, project_id):
        # Mirrors the real verify(): an unconfigured project refuses before anything else, so
        # this stub cannot let a test pass that the genuine implementation would fail.
        if not project_id:
            raise InvalidToken("no Identity Platform project is configured")
        if raw == "good":
            return VerifiedToken(uid="uid-1", email="person@example.com",
                                 email_verified=True, name="A Person")
        raise InvalidToken("no")

    async def fake_resolve(db, token, now=None):
        return _ACCOUNT

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(auth_route.firebase_token, "verify", fake_verify)
    monkeypatch.setattr(auth_route.account_service, "resolve_or_provision", fake_resolve)
    monkeypatch.setattr(auth_route, "get_db", lambda: _NullSession())

    application = FastAPI()
    application.include_router(auth_route.router)
    return application


async def _post(app, body, headers=None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # `headers if headers is not None else ...`, NOT `headers or ...`: an explicitly empty
        # dict ({}) is falsy, so `or` would silently substitute the default api-key header and
        # defeat every test that means to send none.
        sent = headers if headers is not None else {"x-api-key": "console-key"}
        return await client.post("/auth/session", json=body, headers=sent)


async def test_a_good_token_returns_the_identity_the_console_needs(app):
    response = await _post(app, {"id_token": "good"})
    assert response.status_code == 200
    assert response.json() == {"user_id": "user-1", "owner_id": "person@example.com",
                               "organization_id": "org-1", "email_verified": True,
                               "name": "A Person"}


async def test_a_caller_without_the_api_key_is_refused(app):
    response = await _post(app, {"id_token": "good"}, headers={})
    assert response.status_code == 401


async def test_a_caller_with_the_wrong_api_key_is_refused(app):
    response = await _post(app, {"id_token": "good"}, headers={"x-api-key": "not-it"})
    assert response.status_code == 401


async def test_a_bad_token_is_refused_without_saying_why(app):
    response = await _post(app, {"id_token": "forged"})
    assert response.status_code == 401
    # One refusal for every cause, exactly as api_key_service.authenticate does.
    assert response.json()["detail"] == auth_route.REFUSAL


async def test_a_missing_token_is_refused_the_same_way(app):
    response = await _post(app, {})
    assert response.status_code == 401
    assert response.json()["detail"] == auth_route.REFUSAL


async def test_a_closed_signup_answers_403_so_the_console_can_explain_it(app, monkeypatch):
    # Distinct from 401 on purpose: this is the ONE refusal a user can do something about, and
    # "this deployment is not open" is not a secret. It leaks no account existence - it is the
    # same answer for every unknown token.
    async def refuse(db, token, now=None):
        raise SignupDisabled("closed")

    monkeypatch.setattr(auth_route.account_service, "resolve_or_provision", refuse)
    response = await _post(app, {"id_token": "good"})
    assert response.status_code == 403


async def test_the_token_never_reaches_the_logs(app, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    await _post(app, {"id_token": "good"})
    await _post(app, {"id_token": "forged"})
    assert "good" not in caplog.text
    assert "forged" not in caplog.text


async def test_no_project_configured_refuses_every_token(app, monkeypatch):
    monkeypatch.setattr(polcfg, "FIREBASE_PROJECT_ID", "")
    response = await _post(app, {"id_token": "good"})
    assert response.status_code == 401


async def test_a_refused_link_is_indistinguishable_from_a_forged_token(app, monkeypatch):
    # A token that names an address which already has an account it has not proven it owns is
    # refused - but it must be refused with the SAME answer a forged token gets. Anything that
    # differs (the status, the body, a distinguishing word) turns this endpoint into an oracle
    # for which addresses hold accounts here, which is precisely what the refusal exists to deny.
    from meshpipeline.application.account_service import LinkRefused

    async def refuse(db, token, now=None):
        raise LinkRefused("linking an existing account requires a verified address")

    monkeypatch.setattr(auth_route.account_service, "resolve_or_provision", refuse)
    refused = await _post(app, {"id_token": "good"})
    forged = await _post(app, {"id_token": "forged"})

    assert refused.status_code == forged.status_code == 401
    assert refused.json() == forged.json()
    assert refused.json()["detail"] == auth_route.REFUSAL


async def test_the_refused_link_does_not_name_the_address_in_the_response(app, monkeypatch):
    from meshpipeline.application.account_service import LinkRefused

    async def refuse(db, token, now=None):
        raise LinkRefused("this address is already linked to another account")

    monkeypatch.setattr(auth_route.account_service, "resolve_or_provision", refuse)
    response = await _post(app, {"id_token": "good"})
    assert "person@example.com" not in response.text
    assert "verif" not in response.text.lower(), "the refusal disclosed why it refused"


async def test_token_verification_does_not_run_on_the_event_loop(app, monkeypatch):
    # `firebase_token.verify` is synchronous: a cold certificate cache makes a blocking HTTP call
    # to Google and every call does a blocking RSA verification. On the loop that stalls every
    # other request in the process, which presents as a general API outage rather than a slow
    # sign-in. Spec section 4 step 1 requires a threadpool; this asserts the call actually leaves
    # the loop's thread rather than merely being awaited.
    import asyncio
    import threading

    loop_thread = threading.get_ident()
    seen: dict = {}

    def fake_verify(raw, *, project_id):
        seen["thread"] = threading.get_ident()
        seen["loop_running"] = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen["loop_running"] = False
        return VerifiedToken(uid="uid-1", email="person@example.com",
                             email_verified=True, name="A Person")

    monkeypatch.setattr(auth_route.firebase_token, "verify", fake_verify)
    response = await _post(app, {"id_token": "good"})

    assert response.status_code == 200
    assert seen["thread"] != loop_thread, (
        "the blocking token verification ran on the event loop's own thread")
    assert seen["loop_running"] is False
