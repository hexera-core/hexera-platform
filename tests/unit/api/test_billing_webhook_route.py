# Responsibility: Verify the one unauthenticated mutating route refuses everything it cannot prove.
#
# THIS FILE IS NAMED IN _UNAUTHENTICATED_ROUTE_ALLOWED. api/v1/stripe_webhook.py:receive is the only
# mutating route besides sign-in that carries no `owner_dep`, and the reason recorded there is that
# the signature authenticates it instead. That claim is only worth as much as these tests.
from __future__ import annotations

import contextlib

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from meshpipeline.api.v1 import stripe_webhook
from meshpipeline.contracts import billing as billing_contract
from meshpipeline.contracts.billing import BillingEvent, EventVerificationFailed


class _Gateway:

    def __init__(self, *, event: BillingEvent | None = None, fail: bool = False):
        self.event = event
        self.fail = fail
        self.seen: list[tuple[bytes, str]] = []

    def verify_event(self, *, payload: bytes, signature: str) -> BillingEvent:
        self.seen.append((payload, signature))
        if self.fail:
            raise EventVerificationFailed("signature mismatch")
        assert self.event is not None
        return self.event


@pytest.fixture
def client(monkeypatch):
    # THE SESSION IS STUBBED, not connected. Every assertion in this file is about the ROUTE - what
    # it proves before it acts, and what status it answers - and none of them is about persistence.
    # A real session would make these tests need a database to prove a signature check.
    @contextlib.asynccontextmanager
    async def _no_db():
        yield object()

    monkeypatch.setattr(stripe_webhook, "get_db", _no_db)
    app = FastAPI()
    app.include_router(stripe_webhook.router, prefix="/api/v1/webhooks/stripe")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_gateway():
    # Every test binds its own; the accessor is process-wide state, so it is cleared afterwards to
    # stop one test's gateway leaking into another's.
    yield
    billing_contract.set_billing_gateway(None)


def test_an_unconfigured_deployment_refuses_the_route_outright(client):
    # FAIL CLOSED. With no gateway there is no signing secret, so there is no way to tell the
    # provider from anyone who found the URL - and every handler behind this endpoint writes to the
    # credit ledger. A 503 here is the difference between "billing is off" and "billing is an
    # unauthenticated grant of credits".
    billing_contract.set_billing_gateway(None)
    res = client.post("/api/v1/webhooks/stripe", content=b"{}")
    assert res.status_code == 503


def test_an_unverified_body_is_rejected_and_never_applied(client, monkeypatch):
    # THE CLAIM THE ALLOWLIST ENTRY RESTS ON: an unsigned or forged body does not reach a handler.
    gateway = _Gateway(fail=True)
    billing_contract.set_billing_gateway(gateway)

    applied: list = []

    async def _never(db, event):
        applied.append(event)
        return True

    monkeypatch.setattr(stripe_webhook.billing_service, "handle_event", _never)

    res = client.post("/api/v1/webhooks/stripe", content=b'{"id":"evt_forged"}',
                      headers={"Stripe-Signature": "t=1,v1=nonsense"})
    # 400, NOT 500: a body that cannot verify must never be retried. A 5xx would ask the provider to
    # redeliver the same forged or stale request on a backoff, indefinitely.
    assert res.status_code == 400
    assert applied == []


def test_the_raw_bytes_are_what_get_verified(client):
    # The signature is an HMAC over the body EXACTLY as sent. If anything parsed the JSON and
    # re-serialised it before verification, a valid signature would start failing - so this pins
    # that the gateway receives the original bytes, spacing and all.
    body = b'{"id":"evt_1",  "spaced": true}'
    gateway = _Gateway(event=BillingEvent(id="evt_1", type="customer.created", payload={}))
    billing_contract.set_billing_gateway(gateway)

    client.post("/api/v1/webhooks/stripe", content=body,
                headers={"Stripe-Signature": "t=1,v1=abc"})
    assert gateway.seen == [(body, "t=1,v1=abc")]


def test_an_ignored_event_is_acknowledged_rather_than_retried(client):
    # An event type this deployment does not act on is a FULLY HANDLED outcome. Answering anything
    # but 2xx would make the provider retry it on a backoff and eventually disable the endpoint.
    billing_contract.set_billing_gateway(
        _Gateway(event=BillingEvent(id="evt_1", type="customer.created", payload={})))
    res = client.post("/api/v1/webhooks/stripe", content=b"{}",
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code == 200
    assert res.json() == {"received": True, "applied": False}


def test_a_handler_failure_asks_for_a_retry(client, monkeypatch):
    # THE ONE FAILURE THAT SHOULD BE RETRIED: the event was genuinely the provider's and the effect
    # did not land. The idempotency row is written in the same transaction as the effect, so the
    # rolled-back attempt leaves no claim behind and the retry applies cleanly.
    billing_contract.set_billing_gateway(
        _Gateway(event=BillingEvent(id="evt_1", type="invoice.paid", payload={})))

    async def _boom(db, event):
        raise RuntimeError("the database was unreachable")

    monkeypatch.setattr(stripe_webhook.billing_service, "handle_event", _boom)

    res = client.post("/api/v1/webhooks/stripe", content=b"{}",
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code == 500


def test_the_route_declares_no_owner_dependency(client):
    # The allowlist entry says this route cannot carry `owner_dep` because the caller is the payment
    # provider. If somebody later adds one, the entry becomes a stale exemption that nothing reads -
    # this fails first and says so.
    import inspect

    from meshpipeline.api.security import owner_dep
    defaults = inspect.signature(stripe_webhook.receive).parameters.values()
    assert not any(getattr(p.default, "dependency", None) is owner_dep for p in defaults)


def test_a_failure_to_apply_never_leaks_the_body_into_the_response(client, monkeypatch):
    # An unverified body is attacker-controlled. Neither it nor the provider's own error text may be
    # echoed back to whoever posted it.
    billing_contract.set_billing_gateway(_Gateway(fail=True))
    res = client.post("/api/v1/webhooks/stripe", content=b'{"secret":"do-not-echo"}',
                      headers={"Stripe-Signature": "t=1,v1=nonsense"})
    assert "do-not-echo" not in res.text
    assert res.json()["detail"] == "signature verification failed"


def test_the_service_is_reached_with_the_verified_event(client, monkeypatch):
    # The handler must receive the CONTRACT's event, not the raw body: everything downstream reads
    # `.type` and `.payload`, and both exist only after verification has produced them.
    event = BillingEvent(id="evt_9", type="invoice.paid", payload={"customer": "cus_1"})
    billing_contract.set_billing_gateway(_Gateway(event=event))

    seen: list = []

    async def _capture(db, incoming):
        seen.append(incoming)
        return True

    monkeypatch.setattr(stripe_webhook.billing_service, "handle_event", _capture)
    res = client.post("/api/v1/webhooks/stripe", content=b"{}",
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code == 200
    assert seen == [event]


def test_an_http_exception_from_the_handler_is_not_swallowed_into_a_retry(client, monkeypatch):
    # A handler that raises HTTPException is caught by the blanket `except Exception` and re-raised
    # as a 500. That is deliberate - it still asks for a retry - but it must not silently become a
    # 200, which would tell the provider the event was applied when it was not.
    billing_contract.set_billing_gateway(
        _Gateway(event=BillingEvent(id="evt_1", type="invoice.paid", payload={})))

    async def _refuse(db, event):
        raise HTTPException(status_code=409, detail="conflict")

    monkeypatch.setattr(stripe_webhook.billing_service, "handle_event", _refuse)
    res = client.post("/api/v1/webhooks/stripe", content=b"{}",
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code >= 400


# THE BODY CAP, which is the availability control that replaced the rate limiter

def test_an_oversized_declared_body_is_refused_before_it_is_read(client):
    # This route is public and exempt from the limiter, so the body is the one thing an anonymous
    # caller controls before authentication. A declared length past the cap is refused for the cost
    # of parsing an integer.
    body = b"x" * (stripe_webhook.MAX_WEBHOOK_BODY_BYTES + 1)
    billing_contract.set_billing_gateway(_Gateway(fail=True))
    res = client.post("/api/v1/webhooks/stripe", content=body,
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code == 413


def test_an_oversized_body_that_lies_about_its_length_is_still_refused(client):
    # A caller can understate or omit content-length. The streamed read is what makes the cap real
    # rather than advisory - the refusal has to land at the limit, not after the whole body is in
    # memory, because holding it there is the exhaustion this guards against.
    gateway = _Gateway(fail=True)
    billing_contract.set_billing_gateway(gateway)

    def _chunks():
        for _ in range(8):
            yield b"x" * 65536

    res = client.post("/api/v1/webhooks/stripe", content=_chunks(),
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code == 413
    # AND IT NEVER REACHED VERIFICATION: the point is to stop before doing work, not to do the work
    # and then complain about it.
    assert gateway.seen == []


def test_a_body_within_the_cap_still_reaches_verification_byte_for_byte(client):
    # The cap must not truncate or reshape an ordinary event - the signature is an HMAC over these
    # exact bytes, so a cap that trimmed them would break every valid delivery.
    body = b'{"id":"evt_1","padding":"' + b"y" * 1024 + b'"}'
    gateway = _Gateway(event=BillingEvent(id="evt_1", type="customer.created", payload={}))
    billing_contract.set_billing_gateway(gateway)
    res = client.post("/api/v1/webhooks/stripe", content=body,
                      headers={"Stripe-Signature": "t=1,v1=abc"})
    assert res.status_code == 200
    assert gateway.seen == [(body, "t=1,v1=abc")]
