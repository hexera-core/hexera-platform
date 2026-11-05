# Responsibility: Verify the authentication header is enforced end to end when authentication is enabled.
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1.chat import router as chat_router

_SESSION_ID = uuid.UUID("aaaa1111-1111-4111-9111-111111111111")


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1/chat")
    return app


class _FakeDBCtx:
    async def __aenter__(self):
        return AsyncMock()
    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_get_db():
    return _FakeDBCtx()


async def _call(app: FastAPI, headers: dict) -> int:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/api/v1/chat/history/{_SESSION_ID}", headers=headers)
    return resp.status_code


async def test_auth_disabled_lets_unauthenticated_caller_through():
    app = _build_app()
    with patch.object(polcfg, "MESH_API_KEY", ""), \
         patch("meshpipeline.api.v1.chat.get_db", new=_fake_get_db), \
         patch("meshpipeline.persistence.repositories.session_repository.SessionRepository.get_for_owner",
               new=AsyncMock(return_value=None)):
        status = await _call(app, headers={})
    assert status == 404, f"expected 404 (auth passed → handler ran), got {status}"


async def test_auth_enabled_rejects_missing_header():
    app = _build_app()
    with patch.object(polcfg, "MESH_API_KEY", "secret"):
        status = await _call(app, headers={})
    assert status == 401, f"expected 401 (auth rejected), got {status}"


async def test_auth_enabled_rejects_wrong_header():
    app = _build_app()
    with patch.object(polcfg, "MESH_API_KEY", "secret"):
        status = await _call(app, headers={"X-API-Key": "wrong"})
    assert status == 401, f"expected 401 (auth rejected), got {status}"


async def test_auth_enabled_accepts_correct_header():
    app = _build_app()
    with patch.object(polcfg, "MESH_API_KEY", "secret"), \
         patch("meshpipeline.api.v1.chat.get_db", new=_fake_get_db), \
         patch("meshpipeline.persistence.repositories.session_repository.SessionRepository.get_for_owner",
               new=AsyncMock(return_value=None)):
        status = await _call(app, headers={"X-API-Key": "secret"})
    assert status == 404, f"expected 404 (auth passed → handler ran), got {status}"
