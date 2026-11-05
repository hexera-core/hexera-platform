# Responsibility: Verify the export and erasure routes refuse an API key, demand typed confirmation, and map refusals.
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshpipeline.api.security import principal_dep
from meshpipeline.api.v1 import account
from meshpipeline.contracts.identity import Credential, Principal

OWNER = "ada@example.com"
ORG = "7b0f2a8e-2d5e-4a59-9d55-1c1d3f0c6c11"


@asynccontextmanager
async def _null_db():
    yield None


def _client(monkeypatch, credential=Credential.signed_header, calls=None):
    calls = calls if calls is not None else []

    async def export_account(db, *, owner_id, organization_id):
        calls.append(("export", owner_id, organization_id))
        return {"format": "hexera-account-export/v1"}

    async def request_erasure(db, *, owner_id, organization_id):
        calls.append(("erase", owner_id, organization_id))
        if organization_id == "refuse":
            raise account.privacy_service.ErasureRefused("Cancel your subscription first.")
        return {"status": "requested", "requested_at": "now"}

    monkeypatch.setattr(account.privacy_service, "export_account", export_account)
    monkeypatch.setattr(account.privacy_service, "request_erasure", request_erasure)
    monkeypatch.setattr(account, "get_db", _null_db)
    app = FastAPI()
    app.include_router(account.router, prefix="/api/v1/account")
    org = "refuse" if credential == "refuse" else ORG
    cred = Credential.signed_header if credential == "refuse" else credential
    app.dependency_overrides[principal_dep] = lambda: Principal(
        owner_id=OWNER, organization_id=org, credential=cred)
    return TestClient(app), calls


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/v1/account/export", None),
    ("post", "/api/v1/account/erasure", {"confirm_email": OWNER}),
])
def test_an_api_key_can_neither_export_nor_erase(monkeypatch, method, path, body):
    # A leaked key must not be enough to take a tenant's data wholesale or to destroy it.
    client, calls = _client(monkeypatch, credential=Credential.api_key)
    response = getattr(client, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 403
    assert calls == []


def test_the_export_is_an_uncached_attachment(monkeypatch):
    client, calls = _client(monkeypatch)
    response = client.get("/api/v1/account/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("export", OWNER, ORG)]


@pytest.mark.parametrize("typed", ["", "someone@example.com", "ada@example.co"])
def test_erasure_requires_the_account_address_typed(monkeypatch, typed):
    client, calls = _client(monkeypatch)
    response = client.post("/api/v1/account/erasure", json={"confirm_email": typed})
    assert response.status_code == 400
    assert calls == []


def test_a_confirmed_erasure_is_accepted(monkeypatch):
    client, calls = _client(monkeypatch)
    response = client.post("/api/v1/account/erasure", json={"confirm_email": " ADA@example.com "})
    assert response.status_code == 202
    assert calls == [("erase", OWNER, ORG)]


def test_a_refused_erasure_is_a_409_with_the_reason(monkeypatch):
    client, _ = _client(monkeypatch, credential="refuse")
    response = client.post("/api/v1/account/erasure", json={"confirm_email": OWNER})
    assert response.status_code == 409
    assert response.json()["detail"] == "Cancel your subscription first."
