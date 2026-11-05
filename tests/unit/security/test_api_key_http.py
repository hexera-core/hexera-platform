# Responsibility: Verify a bearer key and a signed header arrive at the same principal over real HTTP, and that neither weakens the other.
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import security as auth
from meshpipeline.contracts import api_key
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.contracts.rate_limit import set_rate_limit_store
from meshpipeline.settings import plans

_KEY = api_key.mint()
_OWNER = "tenant-a"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/owner")
    async def _owner(owner_id: str = Depends(auth.owner_dep)):
        return {"owner_id": owner_id}

    @app.get("/both")
    async def _both(owner_id: str = Depends(auth.owner_dep), plan: str = Depends(auth.plan_dep)):
        return {"owner_id": owner_id, "plan": plan}

    return app


class _NoDatabase:
    """Any use is a failure: a header-authenticated request must not open a session."""
    async def __aenter__(self):
        raise AssertionError("the header credential path opened a database session")
    async def __aexit__(self, *a):
        return False


class _FakeDB:
    async def __aenter__(self):
        return AsyncMock()
    async def __aexit__(self, *a):
        return False


def _authenticating(principal: Principal | None, calls: list | None = None):
    async def _authenticate(_db, presented, **_kw):
        if calls is not None:
            calls.append(presented)
        return principal
    return patch("meshpipeline.application.api_key_service.authenticate", new=_authenticate)


async def _get(app: FastAPI, path: str = "/owner", **headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(path, headers=headers)


# the bearer credential

async def test_a_live_key_resolves_to_the_owner_the_key_names():
    principal = Principal(owner_id=_OWNER, plan="scale", credential=Credential.api_key,
                          key_id=str(uuid.uuid4()))
    with _authenticating(principal), patch.object(auth, "get_db", new=_FakeDB), \
         patch.object(polcfg, "MESH_API_KEY", "shared-secret"):
        resp = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
    assert resp.status_code == 200
    assert resp.json()["owner_id"] == _OWNER


async def test_a_live_key_needs_no_shared_product_key_and_no_signature():
    # The whole point of a per-key credential: it carries its own owner, so the shared
    # MESH_API_KEY and the X-User-Sig HMAC are not additionally required.
    principal = Principal(owner_id=_OWNER, credential=Credential.api_key)
    with _authenticating(principal), patch.object(auth, "get_db", new=_FakeDB), \
         patch.object(polcfg, "MESH_API_KEY", "shared-secret"), \
         patch.object(polcfg, "USER_TOKEN_SECRET", "signing-secret"):
        resp = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
    assert resp.status_code == 200


async def test_a_rejected_key_is_401():
    with _authenticating(None), patch.object(auth, "get_db", new=_FakeDB):
        resp = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
    assert resp.status_code == 401
    assert "key" in resp.json()["detail"].lower()


async def test_a_bearer_credential_that_is_not_a_key_is_401_rather_than_ignored():
    # Falling through to the header path would let a caller present a bad bearer token and still
    # be served as a self-asserted dev identity.
    with patch.object(auth, "get_db", new=_NoDatabase), patch.object(polcfg, "MESH_API_KEY", ""):
        resp = await _get(_app(), Authorization="Bearer not-a-hexera-key")
    assert resp.status_code == 401


async def test_a_key_presented_in_the_wrong_scheme_is_not_accepted():
    with patch.object(auth, "get_db", new=_NoDatabase), patch.object(polcfg, "MESH_API_KEY", "k"):
        resp = await _get(_app(), Authorization=f"Basic {_KEY.presented}")
    assert resp.status_code == 401


async def test_the_scheme_is_matched_case_insensitively():
    principal = Principal(owner_id=_OWNER, credential=Credential.api_key)
    with _authenticating(principal), patch.object(auth, "get_db", new=_FakeDB):
        resp = await _get(_app(), Authorization=f"bearer {_KEY.presented}")
    assert resp.status_code == 200


# the header credential, unchanged

async def test_the_signed_header_path_still_authenticates_and_touches_no_database():
    with patch.object(auth, "get_db", new=_NoDatabase), \
         patch.object(polcfg, "MESH_API_KEY", "shared-secret"), \
         patch.object(polcfg, "USER_TOKEN_SECRET", "signing-secret"):
        resp = await _get(_app(), **{"X-API-Key": "shared-secret", "X-User-Id": "alice",
                                     "X-User-Sig": auth.expected_user_sig("alice")})
    assert resp.status_code == 200
    assert resp.json()["owner_id"] == "alice"


async def test_a_forged_signature_is_still_refused():
    with patch.object(auth, "get_db", new=_NoDatabase), \
         patch.object(polcfg, "MESH_API_KEY", "shared-secret"), \
         patch.object(polcfg, "USER_TOKEN_SECRET", "signing-secret"):
        resp = await _get(_app(), **{"X-API-Key": "shared-secret", "X-User-Id": "victim",
                                     "X-User-Sig": auth.expected_user_sig("alice")})
    assert resp.status_code == 401


# one identity concept, resolved once

async def test_both_credential_types_resolve_through_the_one_seam():
    # resolve_principal is the single place a credential becomes a tenant. When organisations
    # arrive, this is the one function that widens - so both paths must already come through it.
    from_key = Principal(owner_id=_OWNER, credential=Credential.api_key)
    with _authenticating(from_key), patch.object(auth, "get_db", new=_FakeDB):
        by_key = await auth.resolve_principal(f"Bearer {_KEY.presented}", None, None, None)
    with patch.object(polcfg, "MESH_API_KEY", ""), patch.object(polcfg, "USER_TOKEN_SECRET", ""):
        by_header = await auth.resolve_principal(None, None, "alice", None)

    assert type(by_key) is type(by_header) is Principal
    assert by_key.owner_id == _OWNER and by_header.owner_id == "alice"
    assert by_key.credential != by_header.credential, "the two credentials are not distinguished"


async def test_the_header_path_returns_a_principal_not_a_bare_string():
    with patch.object(polcfg, "MESH_API_KEY", ""), patch.object(polcfg, "USER_TOKEN_SECRET", ""):
        principal = await auth.resolve_principal(None, None, "alice", None)
    assert isinstance(principal, Principal)
    assert principal.owner_id == "alice"
    assert principal.credential == Credential.self_asserted
    assert principal.plan == ""


async def test_a_signed_header_identity_is_marked_as_such():
    with patch.object(polcfg, "MESH_API_KEY", "k"), patch.object(polcfg, "USER_TOKEN_SECRET", "s"):
        principal = await auth.resolve_principal("", "k", "alice", auth.expected_user_sig("alice"))
    assert principal.credential == Credential.signed_header


async def test_the_credential_is_resolved_once_however_many_dependencies_need_it():
    # Two resolutions would mean two database reads, two last_used_at writes and two rate-limit
    # units consumed for one request.
    calls: list[str] = []
    principal = Principal(owner_id=_OWNER, plan="scale", credential=Credential.api_key)
    with _authenticating(principal, calls), patch.object(auth, "get_db", new=_FakeDB):
        resp = await _get(_app(), "/both", Authorization=f"Bearer {_KEY.presented}")
    assert resp.status_code == 200
    assert resp.json() == {"owner_id": _OWNER, "plan": "scale"}
    assert len(calls) == 1, f"the key was verified {len(calls)} times for one request"


# the plan's request rate

class _CountingStore:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr_window(self, identity: str, window: int, ttl_seconds: int) -> int:
        key = f"{identity}:{window}"
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


class _BrokenStore:
    async def incr_window(self, identity: str, window: int, ttl_seconds: int) -> int:
        raise RuntimeError("redis is down")


async def test_a_key_is_held_to_its_plan_s_request_rate():
    principal = Principal(owner_id=_OWNER, plan="trial", credential=Credential.api_key)
    store = _CountingStore()
    set_rate_limit_store(store)
    try:
        with _authenticating(principal), patch.object(auth, "get_db", new=_FakeDB), \
             patch.dict(plans.PLANS, {"trial": plans.PlanOverride(rate_limit_per_minute=1)},
                        clear=True):
            first = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
            second = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
    finally:
        set_rate_limit_store(None)
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers.get("retry-after")


async def test_an_unavailable_limiter_does_not_refuse_an_authenticated_caller():
    principal = Principal(owner_id=_OWNER, plan="trial", credential=Credential.api_key)
    set_rate_limit_store(_BrokenStore())
    try:
        with _authenticating(principal), patch.object(auth, "get_db", new=_FakeDB), \
             patch.dict(plans.PLANS, {"trial": plans.PlanOverride(rate_limit_per_minute=1)},
                        clear=True):
            first = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
            second = await _get(_app(), Authorization=f"Bearer {_KEY.presented}")
    finally:
        set_rate_limit_store(None)
    assert (first.status_code, second.status_code) == (200, 200)


async def test_a_header_identity_is_not_counted_against_a_plan_window():
    # The transport middleware already limits it; counting it here would halve its allowance.
    store = _CountingStore()
    set_rate_limit_store(store)
    try:
        with patch.object(auth, "get_db", new=_NoDatabase), patch.object(polcfg, "MESH_API_KEY", ""):
            resp = await _get(_app(), **{"X-User-Id": "alice"})
    finally:
        set_rate_limit_store(None)
    assert resp.status_code == 200
    assert store.counts == {}
