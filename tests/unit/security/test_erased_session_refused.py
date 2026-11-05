# Responsibility: Verify a signed console session dies with the account it names.
from __future__ import annotations

import pytest
from fastapi import HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import security

pytestmark = pytest.mark.asyncio


class _Row:
    pass


@pytest.fixture
def signed(monkeypatch):
    state = {"org": "", "user": None}

    async def org_for(owner_id):
        return state["org"]

    async def get_by_email(self, db, email):
        return state["user"]

    class _NullDb:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "secret")
    monkeypatch.setattr(security, "_organization_for", org_for)
    monkeypatch.setattr(security, "get_db", lambda: _NullDb())
    monkeypatch.setattr(security, "verify_identity", lambda *a: "ada@example.com")
    from meshpipeline.persistence.repositories.user_repository import UserRepository
    monkeypatch.setattr(UserRepository, "get_by_email", get_by_email)
    return state


async def test_an_erased_accounts_signed_session_is_refused(signed):
    # THE HOLE THIS CLOSES: erasure deletes the users row, but the console cookie's HMAC stays
    # valid; without this check the request resolved org-less and spend_gate's users-row test
    # (which the erasure also removed) admitted it to run uncharged jobs.
    with pytest.raises(HTTPException) as exc:
        await security.resolve_principal(None, None, "ada@example.com", "sig")
    assert exc.value.status_code == 401


async def test_an_unverified_account_awaiting_provisioning_still_signs_in(signed):
    # No organisation yet is the NORMAL state between sign-up and the verification click; the
    # users row is what tells it apart from an erased account.
    signed["user"] = _Row()
    principal = await security.resolve_principal(None, None, "ada@example.com", "sig")
    assert principal.organization_id == ""


async def test_a_resolved_organisation_skips_the_existence_read(signed):
    signed["org"] = "7b0f2a8e-2d5e-4a59-9d55-1c1d3f0c6c11"
    signed["user"] = None   # would refuse if it were consulted
    principal = await security.resolve_principal(None, None, "ada@example.com", "sig")
    assert principal.organization_id == signed["org"]


async def test_self_asserted_dev_identity_is_untouched(signed, monkeypatch):
    # No secret means no existence check: with user=None this would 401 if it were applied.
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "")
    principal = await security.resolve_principal(None, None, "ada@example.com", None)
    assert principal.owner_id == "ada@example.com"
