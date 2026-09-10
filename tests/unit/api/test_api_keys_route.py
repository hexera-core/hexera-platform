# tests/unit/api/test_api_keys_route.py
# Responsibility: Verify keys are the caller's own, minted once, and unmanageable by a key.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from meshpipeline.api.v1 import api_keys
from meshpipeline.contracts.identity import Credential, Principal

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
ORG = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def _console_principal() -> Principal:
    return Principal(owner_id=OWNER, organization_id=ORG,
                     credential=Credential.signed_header)


def _key_principal() -> Principal:
    return Principal(owner_id=OWNER, organization_id=ORG, credential=Credential.api_key,
                     key_id=str(uuid.uuid4()))


@dataclass
class _Row:
    id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None


@dataclass
class _Issued:
    key_id: str
    owner_id: str
    name: str
    plan: str
    key_prefix: str
    secret: str
    presented: str
    expires_at: datetime | None


@pytest.fixture
def keys(monkeypatch):
    rows = [_Row(id=uuid.uuid4(), name="ci", key_prefix="hx_live_abc123def456",
                 created_at=NOW, last_used_at=None, revoked_at=None, expires_at=None)]
    revoked: list = []

    async def fake_list(db, *, owner_id, organization_id=""):
        return rows if owner_id == OWNER else []

    async def fake_issue(db, *, owner_id, name="", plan="", organization_id=None,
                         expires_at=None):
        return _Issued(key_id=str(uuid.uuid4()), owner_id=owner_id, name=name, plan=plan,
                       key_prefix="hx_live_newkey000000",
                       secret="s3cr3t", presented="hx_live_newkey000000_s3cr3t",
                       expires_at=None)

    async def fake_revoke(db, *, owner_id, key_id, organization_id="", now=None):
        revoked.append((owner_id, key_id))
        return key_id == rows[0].id

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(api_keys.api_key_service, "list_for_owner", fake_list)
    monkeypatch.setattr(api_keys.api_key_service, "issue", fake_issue)
    monkeypatch.setattr(api_keys.api_key_service, "revoke", fake_revoke)
    monkeypatch.setattr(api_keys, "get_db", lambda: _NullSession())
    return rows, revoked


async def test_the_list_is_the_callers_own_keys_and_carries_no_secret(keys):
    rows, _ = keys
    payload = await api_keys.list_keys(principal=_console_principal(), owner_id=OWNER)
    assert [item["key_prefix"] for item in payload["items"]] == [rows[0].key_prefix]
    serialised = repr(payload)
    assert "key_hash" not in serialised
    assert "secret" not in serialised


async def test_creating_a_key_returns_the_presented_secret_exactly_once(keys):
    payload = await api_keys.create_key(body=api_keys.CreateKeyIn(name="ci"),
                                        principal=_console_principal(), owner_id=OWNER)
    assert payload["presented"] == "hx_live_newkey000000_s3cr3t"
    # And the list that follows must not carry it.
    listed = await api_keys.list_keys(principal=_console_principal(), owner_id=OWNER)
    assert all("presented" not in item for item in listed["items"])


async def test_revoking_reports_whether_a_live_key_was_revoked(keys):
    rows, _ = keys
    assert await api_keys.revoke_key(key_id=rows[0].id, principal=_console_principal(),
                                     owner_id=OWNER) == {"revoked": True}
    assert await api_keys.revoke_key(key_id=uuid.uuid4(), principal=_console_principal(),
                                     owner_id=OWNER) == {"revoked": False}


async def test_an_api_key_credential_may_not_list_keys(keys):
    with pytest.raises(HTTPException) as caught:
        await api_keys.list_keys(principal=_key_principal(), owner_id=OWNER)
    assert caught.value.status_code == 403


async def test_an_api_key_credential_may_not_mint_a_key(keys):
    # THE ONE THAT MATTERS. Without this, a leaked key mints its own replacements and revoking
    # the original accomplishes nothing -- the key becomes permanent persistence.
    with pytest.raises(HTTPException) as caught:
        await api_keys.create_key(body=api_keys.CreateKeyIn(name="pivot"),
                                   principal=_key_principal(), owner_id=OWNER)
    assert caught.value.status_code == 403


async def test_an_api_key_credential_may_not_revoke_a_key(keys):
    # Revocation too: otherwise a leaked key revokes the owner's real keys as a denial of service.
    with pytest.raises(HTTPException) as caught:
        await api_keys.revoke_key(key_id=uuid.uuid4(), principal=_key_principal(), owner_id=OWNER)
    assert caught.value.status_code == 403


async def test_the_refusal_names_the_credential_rather_than_pretending_the_key_is_invalid(keys):
    # Deliberately NOT the undifferentiated 401 the rest of the auth surface answers with. This
    # discloses no account existence, and a caller acting on a valid key needs to be told the
    # operation wants a console session rather than left believing their key expired.
    with pytest.raises(HTTPException) as caught:
        await api_keys.create_key(body=api_keys.CreateKeyIn(name="x"),
                                   principal=_key_principal(), owner_id=OWNER)
    assert "console" in caught.value.detail.lower()
