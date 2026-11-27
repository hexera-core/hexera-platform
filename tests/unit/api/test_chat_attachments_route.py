# Responsibility: Verify the attachment routes carry the application's answers: the row, the plain line with its status, 404 for another tenant, and the served file's safety headers (NOTE-16).
from __future__ import annotations

import uuid
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from meshpipeline.api.v1.chat import router as chat_router
from meshpipeline.application import chat_attachments as ca

SID = uuid.UUID("cdcd1111-2222-4222-b222-cdcdcdcdcdcd")
_app = FastAPI()
_app.include_router(chat_router, prefix="/api/v1/chat")


def _client():
    return AsyncClient(transport=ASGITransport(app=_app), base_url="http://test")


async def _post(accept):
    with patch.object(ca, "accept", accept), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository"):
        async with _client() as c:
            return await c.post("/api/v1/chat/attachments", data={"session_id": str(SID)},
                                files={"file": ("bore.png", b"\x89PNG\r\n\x1a\n", "image/png")})


async def test_a_kept_file_answers_its_row():
    async def accept(session_id, owner_id, *, name, data, **kw):
        assert name == "bore.png" and data.startswith(b"\x89PNG")
        return {"id": "0123456789abcdef", "kind": "image", "name": name, "mime": "image/png", "bytes": 9}
    resp = await _post(accept)
    assert resp.status_code == 200 and resp.json()["id"] == "0123456789abcdef"


async def test_a_refused_file_answers_its_line_and_status():
    for line, status in ((ca.TOO_BIG_IMAGE, 413), (ca.NOT_ACCEPTED, 415), (ca.SCANNED_PDF, 422),
                         (ca.RUN_STARTED, 409)):
        async def accept(*a, _l=line, _s=status, **kw):
            raise ca.Refused(_l, _s)
        resp = await _post(accept)
        assert resp.status_code == status and resp.json()["detail"] == line


async def test_another_tenants_session_is_not_found():
    async def accept(*a, **kw):
        raise LookupError("session not found")
    assert (await _post(accept)).status_code == 404


async def test_the_file_is_served_with_its_safety_headers():
    async def opened(*a, **kw):
        return b"<html>", {"mime": "text/html", "name": "x.html"}
    with patch.object(ca, "open_for_owner", opened), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository"):
        async with _client() as c:
            resp = await c.get(f"/api/v1/chat/attachments/{SID}/0123456789abcdef")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["x-content-type-options"] == "nosniff" and "sandbox" in resp.headers["content-security-policy"]


async def test_an_unknown_file_is_not_found():
    async def opened(*a, **kw):
        return None
    with patch.object(ca, "open_for_owner", opened), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository"):
        async with _client() as c:
            resp = await c.get(f"/api/v1/chat/attachments/{SID}/0123456789abcdef")
    assert resp.status_code == 404
